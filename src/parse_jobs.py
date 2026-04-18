"""Parse raw email bodies into structured job listings."""
import re
from bs4 import BeautifulSoup
from datetime import datetime


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def _extract_urls(text: str) -> list[str]:
    return re.findall(r'https?://[^\s\'"<>]+', text)


def _clean_url(url: str) -> str:
    # Strip tracking params after known job ID patterns
    url = re.sub(r'[?&](utm_[^&]+|trk=[^&]+|refId=[^&]+|trackingId=[^&]+)', '', url)
    url = url.rstrip('&?')
    return url


def _canonicalize_linkedin_url(href: str) -> str:
    """Extract the numeric job ID and return the canonical public URL."""
    m = re.search(r'/jobs/view/(\d+)', href)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}"
    return _clean_url(href)


def _parse_linkedin_email(body: str) -> list[dict]:
    soup = BeautifulSoup(body, "html.parser")
    jobs = []
    seen_urls = set()

    matching = [a for a in soup.find_all("a", href=True) if "/jobs/view/" in a["href"]]
    print(f"[DEBUG] _parse_linkedin_email: {len(matching)} <a> tags match /jobs/view/")

    # Find all <a> tags linking to LinkedIn job view pages
    # (URLs in emails use /comm/jobs/view/ for tracking; normalize to canonical form)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/jobs/view/" not in href:
            continue
        url = _canonicalize_linkedin_url(href)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = a.get_text(strip=True)
        print(f"[DEBUG] url={url} title={title!r}")

        # Walk siblings/parent for company and location text
        company, location = "", ""
        parent = a.find_parent()
        if parent:
            siblings = list(parent.stripped_strings)
            # title is usually first string; company and location follow
            filtered = [s for s in siblings if s != title and len(s) > 1]
            if filtered:
                company = filtered[0]
            if len(filtered) > 1:
                location = filtered[1]

        if not title:
            continue

        jobs.append({
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "source": "LinkedIn",
        })

    return jobs


def _parse_indeed_email(body: str) -> list[dict]:
    text = _strip_html(body)
    jobs = []
    urls = _extract_urls(body)
    job_urls = [u for u in urls if 'indeed.com/viewjob' in u or 'indeed.com/rc/clk' in u]
    # Try to match title/company patterns near each URL
    for url in job_urls:
        jobs.append({
            "title": "",
            "company": "",
            "location": "",
            "url": _clean_url(url),
            "source": "Indeed",
        })
    # Enrich with text parsing
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    for i, job in enumerate(jobs):
        if i < len(lines):
            job["title"] = lines[i * 3] if i * 3 < len(lines) else ""
            job["company"] = lines[i * 3 + 1] if i * 3 + 1 < len(lines) else ""
            job["location"] = lines[i * 3 + 2] if i * 3 + 2 < len(lines) else ""
    return jobs


def _parse_glassdoor_email(body: str) -> list[dict]:
    text = _strip_html(body)
    jobs = []
    urls = _extract_urls(body)
    job_urls = [u for u in urls if 'glassdoor.com/job' in u or 'glassdoor.com/partner' in u]
    for url in job_urls:
        jobs.append({
            "title": "",
            "company": "",
            "location": "",
            "url": _clean_url(url),
            "source": "Glassdoor",
        })
    return jobs


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
