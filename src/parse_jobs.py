"""Parse raw email bodies into structured job listings."""
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from bs4 import BeautifulSoup
from datetime import datetime


# Query params that identify tracking / email-session state; safe to drop
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "trk", "refId", "trackingId", "lipi", "alertAction", "savedSearchId",
    "savedSearchAuthToken", "savedSearchExpireTime", "jrtk", "cs", "t",
    "pos", "guid", "ao", "uido", "src", "ctt", "cb", "gdir", "vt", "s",
    "from", "advn", "sjdu", "tk", "mo", "sk", "eid",
}


def _strip_tracking(href: str) -> str:
    """Remove known tracking query params; preserve path and essential params."""
    try:
        parts = urlparse(href)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k not in _TRACKING_PARAMS]
        return urlunparse(parts._replace(query=urlencode(query)))
    except Exception:
        return href


def _canonicalize_linkedin(href: str) -> tuple[str, str] | None:
    m = re.search(r'/jobs/view/(\d+)', href)
    if not m:
        return None
    url = f"https://www.linkedin.com/jobs/view/{m.group(1)}"
    return url, url  # dedup_key, display_url


def _canonicalize_indeed(href: str) -> tuple[str, str] | None:
    m = re.search(r'[?&]jk=([a-zA-Z0-9]+)', href)
    if not m:
        return None
    url = f"https://www.indeed.com/viewjob?jk={m.group(1)}"
    return url, url


def _canonicalize_glassdoor(href: str) -> tuple[str, str] | None:
    # Glassdoor IDs jobs via jl=<id> or jobListingId=<id>. Dedup on the ID,
    # but preserve the slug in the display URL — the bare ID URL triggers
    # Cloudflare bot protection.
    m = re.search(r'[?&](?:jl|jobListingId)=(\d+)', href)
    if not m:
        return None
    dedup_key = f"glassdoor:{m.group(1)}"
    display_url = _strip_tracking(href)
    return dedup_key, display_url


def _extract_jobs(body: str, url_predicate, canonicalize, source: str) -> list[dict]:
    """Group <a> tags by canonical ID; pick anchor with longest text as title;
    walk up ancestors for company/location."""
    soup = BeautifulSoup(body, "html.parser")

    key_to_entries: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not url_predicate(href):
            continue
        result = canonicalize(href)
        if not result:
            continue
        dedup_key, display_url = result
        key_to_entries.setdefault(dedup_key, []).append((a, display_url))

    jobs = []
    for dedup_key, entries in key_to_entries.items():
        best_a, display_url = max(entries, key=lambda e: len(e[0].get_text(strip=True)))
        title = best_a.get_text(strip=True)
        if not title:
            continue

        company, location = "", ""
        ancestor = best_a
        for _ in range(4):
            if ancestor.parent is None:
                break
            ancestor = ancestor.parent
        if ancestor:
            segments = [s for s in ancestor.stripped_strings if s != title and len(s) > 1]
            if segments:
                company = segments[0]
            if len(segments) > 1:
                location = segments[1]

        jobs.append({
            "title": title,
            "company": company,
            "location": location,
            "url": display_url,
            "dedup_key": dedup_key,
            "source": source,
        })

    return jobs


def _parse_linkedin_email(body: str) -> list[dict]:
    return _extract_jobs(
        body,
        url_predicate=lambda h: "/jobs/view/" in h,
        canonicalize=_canonicalize_linkedin,
        source="LinkedIn",
    )


def _parse_indeed_email(body: str) -> list[dict]:
    return _extract_jobs(
        body,
        url_predicate=lambda h: "indeed." in h and ("/viewjob" in h or "/rc/clk" in h or "/job/" in h),
        canonicalize=_canonicalize_indeed,
        source="Indeed",
    )


def _parse_glassdoor_email(body: str) -> list[dict]:
    return _extract_jobs(
        body,
        url_predicate=lambda h: "glassdoor." in h and ("/job-listing" in h or "/partner/" in h or "/job/" in h),
        canonicalize=_canonicalize_glassdoor,
        source="Glassdoor",
    )


def parse_emails(emails: list[dict]) -> list[dict]:
    all_jobs = []
    for email in emails:
        sender = email.get("from", "").lower()
        body = email.get("body", "")
        if "linkedin" in sender:
            jobs = _parse_linkedin_email(body)
        elif "indeed" in sender:
            jobs = _parse_indeed_email(body)
        elif "glassdoor" in sender:
            jobs = _parse_glassdoor_email(body)
        else:
            jobs = []

        for job in jobs:
            job["email_date"] = email.get("date", "")
            job["posted_at"] = datetime.now().isoformat()

        all_jobs.extend(jobs)

    print(f"Parsed {len(all_jobs)} job listings from {len(emails)} emails")
    return all_jobs
