import os
import sys
import random
import string
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# All requests use HTTPS certificate verification by default.
# Never set verify=False for authentication or file uploads.

TIMEOUT = (10, 30)
MAX_PDF_SIZE = 20 * 1024 * 1024  # 20 MB safety limit

COOKIES = os.environ.get("NAUKRI_COOKIES", "")
FILE_ID = os.environ.get("GOOGLE_DRIVE_FILE_ID", "")
FORM_KEY = os.environ.get("NAUKRI_FORM_KEY", "")

HEADERS = {
    "accept": "application/json",
    "appid": "105",
    "clientid": "d3skt0p",
    "systemid": "jobseeker",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
}

def parse_cookies(cookie_string):
    """Parse a browser cookie string without logging its contents."""
    cookies = {}

    for pair in cookie_string.split(";"):
        if "=" not in pair:
            continue

        key, value = pair.strip().split("=", 1)
        key = key.strip()

        if key:
            cookies[key] = value.strip()

    return cookies


def request_checked(response):
    """Raise a safe error without exposing response contents."""
    if not response.ok:
        raise RuntimeError(
            f"Request failed with HTTP {response.status_code}"
        )
    return response


def download_pdf(file_id):
    """Download and validate the resume PDF."""
    if not file_id or any(c.isspace() for c in file_id):
        raise ValueError("Google Drive file ID is missing or invalid.")

    url = "https://drive.google.com/uc"

    with requests.get(
        url,
        params={"export": "download", "id": file_id},
        timeout=TIMEOUT,
        stream=True,
    ) as response:
        request_checked(response)

        content = bytearray()

        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue

            content.extend(chunk)

            if len(content) > MAX_PDF_SIZE:
                raise ValueError("Resume exceeds the 20 MB safety limit.")

    pdf = bytes(content)

    if not pdf.startswith(b"%PDF-"):
        raise ValueError(
            "Downloaded file is not a valid PDF. "
            "Check the Google Drive link and file permissions."
        )

    return pdf


def refresh_token(session, refresh_token_value):
    """Refresh the Naukri access token."""
    response = session.post(
        "https://www.naukri.com/central-login-services/v1/login/token/refresh",
        headers={
            **HEADERS,
            "content-type": "application/json",
        },
        json={"refreshToken": refresh_token_value},
        timeout=TIMEOUT,
    )

    if response.status_code != 200:
        return None

    return session.cookies.get("nauk_at")


def get_profile_id(session, token):
    """Get the authenticated user's profile ID."""
    response = session.get(
        "https://www.naukri.com/cloudgateway-mynaukri/"
        "resman-aggregator-services/v0/users/self/dashboard",
        headers={
            **HEADERS,
            "authorization": f"Bearer {token}",
        },
        timeout=TIMEOUT,
    )

    request_checked(response)

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Naukri returned an invalid dashboard response.")

    profile_id = (
        data.get("dashBoard", {}).get("profileId")
        or data.get("profileId")
    )

    if not profile_id:
        raise RuntimeError(
            "Profile ID was not returned. Check Naukri authentication."
        )

    return str(profile_id)


def upload_file(pdf_bytes, filename, form_key):
    """Upload the PDF to Naukri's file service."""
    file_key = "U" + "".join(
        random.choices(string.ascii_letters + string.digits, k=13)
    )

    response = requests.post(
        "https://filevalidation.naukri.com/file",
        headers={
            "appid": "105",
            "systemid": "fileupload",
        },
        files={
            "file": (filename, BytesIO(pdf_bytes), "application/pdf")
        },
        data={
            "formKey": form_key,
            "fileName": filename,
            "fileKey": file_key,
        },
        timeout=TIMEOUT,
    )

    request_checked(response)
    return file_key


def attach_to_profile(session, token, profile_id, form_key, file_key):
    """Attach the uploaded PDF to the user's profile."""
    response = session.post(
        "https://www.naukri.com/cloudgateway-mynaukri/"
        f"resman-aggregator-services/v0/users/self/profiles/"
        f"{profile_id}/advResume",
        headers={
            **HEADERS,
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "x-http-method-override": "PUT",
        },
        json={
            "textCV": {
                "formKey": form_key,
                "fileKey": file_key,
                "textCvContent": None,
            }
        },
        timeout=TIMEOUT,
    )

    request_checked(response)


def main():
    if not all([COOKIES, FILE_ID, FORM_KEY]):
        raise ValueError(
            "One or more required GitHub Secrets are missing: "
            "NAUKRI_COOKIES, GOOGLE_DRIVE_FILE_ID, NAUKRI_FORM_KEY."
        )

    cookie_values = parse_cookies(COOKIES)

    if not cookie_values.get("nauk_at") and not cookie_values.get("nauk_rt"):
        raise ValueError(
            "Cookies do not contain a Naukri access or refresh token."
        )

    with requests.Session() as session:
        session.headers.update(HEADERS)

        for key, value in cookie_values.items():
            session.cookies.set(key, value, domain=".naukri.com")

        token = cookie_values.get("nauk_at")

        if cookie_values.get("nauk_rt"):
            try:
                new_token = refresh_token(
                    session, cookie_values["nauk_rt"]
                )
                if new_token:
                    token = new_token
                    print("[OK] Access token refreshed.")
                else:
                    print("[WARN] Token refresh failed.")
            except requests.RequestException:
                print("[WARN] Token refresh request failed.")

        if not token:
            raise RuntimeError(
                "No usable access token. Refresh the Naukri cookies."
            )

        print("[INFO] Authenticating with Naukri...")

        profile_id = get_profile_id(session, token)
        print("[OK] Profile retrieved.")

        pdf = download_pdf(FILE_ID)
        print(f"[OK] Resume downloaded ({len(pdf):,} bytes).")

        date_suffix = datetime.now(
            ZoneInfo("Asia/Kolkata")
        ).strftime("%d_%b_%Y").lower()

        filename = f"resume_{date_suffix}.pdf"

        file_key = upload_file(pdf, filename, FORM_KEY)
        print("[OK] PDF uploaded to Naukri's file service.")

        attach_to_profile(
            session, token, profile_id, FORM_KEY, file_key
        )

        print("[OK] Resume update request completed.")


if __name__ == "__main__":
    try:
        main()
    except requests.Timeout:
        print("[ERROR] Request timed out. Try again later.")
        sys.exit(1)
    except requests.RequestException as exc:
        # Do not print request headers, cookies, or response bodies.
        status = (
            exc.response.status_code
            if getattr(exc, "response", None) is not None
            else None
        )
        if status:
            print(f"[ERROR] Network request failed (HTTP {status}).")
        else:
            print("[ERROR] Network request failed.")
        sys.exit(1)
    except (ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
