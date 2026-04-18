"""Parse raw email bodies into structured job listings."""
import re
from bs4 import BeautifulSoup
from datetime import datetime


def _clean_url(url: str) -> str:
    url = re.sub(r'[?&](utm_[^&]+|trk=[^&]+|refId=[^&]+|trackingId=[^&]+)', '', url)
    url = url.rstrip('&?')
    return url


def _canonicalize_linkedin_url(href: str) -> str:
    m = re.search(r'/jobs/view/(\d+)', href)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}"
    return _clean_url(href)


def _canonicalize_indeed_url(href: str) -> str:
    m = re.search(r'[?&]jk=([a-zA-Z0-9]+)', href)
    if m:
        return f"https://www.indeed.com/viewjob?jk={m.group(1)}"
    return _clean_url(href)


def _canonicalize_glassdoor_url(href: str) -> str:
    m = re.search(r'[?&](?:jl|jobListingId)=(\d+)', href)
    if m:
        return f"https://www.glassdoor.com/job-listing/?jl={m.group(1)}"
    return _clean_url(href)


def _extract_jobs(body: str, url_predicate, canonicalize, source: str) -> list[dict]:
    """Generic parser: group <a> tags by canonical URL, pick anchor with
    longest text as the title, walk up ancestors for company/location."""
    soup = BeautifulSoup(body, "html.parser")

    url_to_anchors: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not url_predicate(href):
            continue
        url = canonicalize(href)
        url_to_anchors.setdefault(url, []).append(a)

    jobs = []
    for url, anchors in url_to_anchors.items():
        best = max(anchors, key=lambda a: len(a.get_text(strip=True)))
        title = best.get_text(strip=True)
        if not title:
            continue

        company, location = "", ""
        ancestor = best
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
            "url": url,
            "source": source,
        })

    return jobs


def _parse_linkedin_email(body: str) -> list[dict]:
    return _extract_jobs(
        body,
        url_predicate=lambda h: "/jobs/view/" in h,
        canonicalize=_canonicalize_linkedin_url,
        source="LinkedIn",
    )


def _parse_indeed_email(body: str) -> list[dict]:
    return _extract_jobs(
        body,
        url_predicate=lambda h: "indeed." in h and ("/viewjob" in h or "/rc/clk" in h or "/job/" in h),
        canonicalize=_canonicalize_indeed_url,
        source="Indeed",
    )


def _parse_glassdoor_email(body: str) -> list[dict]:
    return _extract_jobs(
        body,
        url_predicate=lambda h: "glassdoor." in h and ("/job-listing/" in h or "/partner/" in h or "/job/" in h),
        canonicalize=_canonicalize_glassdoor_url,
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
