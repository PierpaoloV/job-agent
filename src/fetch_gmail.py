"""Fetch job alert emails from Gmail."""
import base64
from collections import Counter
import os
import re
from urllib.parse import parse_qsl, urlparse

from bs4 import BeautifulSoup

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from gmail_auth import (
    SCOPES,
    credential_candidates,
    resolve_token_input_path,
    verify_dedicated_mailbox,
)

SENDER_FILTERS = [
    "jobalerts-noreply@linkedin.com",
    # Indeed uses multiple regional and campaign-specific local parts. Gmail's
    # from: operator accepts a domain fragment, while the parser still admits
    # only Indeed vacancy URLs.
    "indeed.com",
    "alert@indeed.com",
    "donotreply@jobalert.indeed.com",
    "noreply@glassdoor.com",
    "jobs-listings@linkedin.com",
    "noreply@linkedin.com",
    "welcometothejungle.com",
    "wttj.co",
    "eurotechjobs.com",
]


def _get_service():
    creds = None
    token_path = resolve_token_input_path()
    if token_path and token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise RuntimeError(_format_refresh_failure(exc)) from exc
            if token_path is not None:
                token_path.write_text(creds.to_json())
        else:
            searched_credentials = ", ".join(str(path) for path in credential_candidates())
            raise RuntimeError(
                "Gmail token missing or invalid — run `python auth_gmail.py` first. "
                f"Credential search paths: {searched_credentials}"
            )
    service = build("gmail", "v1", credentials=creds)
    verify_dedicated_mailbox(service, creds)
    return service


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

    source_counts = _email_source_counts(emails)
    source_summary = ", ".join(
        f"{source}={source_counts[source]}"
        for source in ("Indeed", "Glassdoor", "LinkedIn", "Other")
    )
    print(
        f"Fetched {len(emails)} job alert emails (last {days_back}d); "
        f"sources: {source_summary}"
    )
    indeed_shapes = _indeed_link_shape_counts(emails)
    if source_counts["Indeed"]:
        rendered_shapes = ", ".join(
            f"{shape} x{count}"
            for shape, count in indeed_shapes.most_common(12)
        )
        print(
            "Indeed link shapes (values redacted): "
            + (rendered_shapes or "none")
        )
    return emails


def _email_source_counts(emails: list[dict]) -> dict[str, int]:
    counts = {"Indeed": 0, "Glassdoor": 0, "LinkedIn": 0, "Other": 0}
    for email in emails:
        sender = str(email.get("from", "")).casefold()
        if "indeed" in sender:
            source = "Indeed"
        elif "glassdoor" in sender:
            source = "Glassdoor"
        elif "linkedin" in sender:
            source = "LinkedIn"
        else:
            source = "Other"
        counts[source] += 1
    return counts


def _indeed_link_shape_counts(emails: list[dict]) -> Counter[str]:
    """Summarize URL structure without logging query values or opaque IDs."""
    shapes: Counter[str] = Counter()
    for email in emails:
        if "indeed" not in str(email.get("from", "")).casefold():
            continue
        soup = BeautifulSoup(str(email.get("body", "")), "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            parts = urlparse(href)
            host = (parts.hostname or "relative").casefold()
            if "indeed" not in host:
                continue
            path = "/".join(
                _redact_path_segment(segment)
                for segment in parts.path.split("/")
                if segment
            )
            keys = sorted(
                {
                    key.casefold()
                    for key, _ in parse_qsl(
                        parts.query, keep_blank_values=True
                    )
                }
            )
            shapes[f"{host}/{path}?{','.join(keys)}"] += 1
    return shapes


def _redact_path_segment(value: str) -> str:
    if (
        len(value) >= 16
        or "~" in value
        or re.fullmatch(r"[0-9a-fA-F-]{12,}", value)
    ):
        return "{id}"
    return value
