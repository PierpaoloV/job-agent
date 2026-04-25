"""Fetch job alert emails from Gmail."""
import base64
import os
import pathlib

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = pathlib.Path(__file__).parent.parent / "token.json"
CREDS_PATH = pathlib.Path(__file__).parent.parent / "credentials.json"

SENDER_FILTERS = [
    "jobalerts-noreply@linkedin.com",
    "alert@indeed.com",
    "noreply@glassdoor.com",
    "jobs-listings@linkedin.com",
    "noreply@linkedin.com",
    "welcometothejungle.com",
    "wttj.co",
    "eurotechjobs.com",
]


def _get_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise RuntimeError(_format_refresh_failure(exc)) from exc
            TOKEN_PATH.write_text(creds.to_json())
        else:
            raise RuntimeError("token.json missing or invalid — run auth_gmail.py first")
    return build("gmail", "v1", credentials=creds)


def _format_refresh_failure(exc: Exception) -> str:
    error_text = str(exc)
    normalized = error_text.lower()

    if "invalid_grant" not in normalized and "expired or revoked" not in normalized:
        return f"Gmail OAuth refresh failed: {error_text}"

    recovery = "Re-run `python auth_gmail.py` locally to generate a new `token.json`."
    if os.environ.get("GITHUB_ACTIONS") == "true":
        recovery += " Then update the `GMAIL_TOKEN_JSON` GitHub Actions secret with the new file contents."

    return (
        "Gmail OAuth refresh token was expired or revoked. "
        f"{recovery} "
        "If this repeats every few days, publish the Google OAuth consent screen to Production instead of Testing."
    )


def _decode_body(part):
    """Recursively find and decode the email body, preferring text/html."""
    html = _find_mime(part, "text/html")
    if html:
        return html
    plain = _find_mime(part, "text/plain")
    if plain:
        return plain
    return ""


def _find_mime(part, mime_type: str) -> str:
    if part.get("mimeType") == mime_type:
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for sub in part.get("parts", []):
        result = _find_mime(sub, mime_type)
        if result:
            return result
    return ""


def fetch_job_emails(days_back: int = 2) -> list[dict]:
    service = _get_service()
    sender_query = " OR ".join(f"from:{s}" for s in SENDER_FILTERS)
    query = f"({sender_query}) newer_than:{days_back}d"

    results = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = results.get("messages", [])

    emails = []
    for msg in messages:
        full = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        body = _decode_body(full["payload"])
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "body": body,
        })

    print(f"Fetched {len(emails)} job alert emails (last {days_back}d)")
    return emails
