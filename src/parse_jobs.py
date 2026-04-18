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


def _parse_linkedin_email(body: str) -> list[dict]:
    text = _strip_html(body)
    jobs = []
    # LinkedIn alert format: job title followed by company and location
    blocks = re.split(r'\n{2,}', text)
    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(lines) < 2:
            continue
        urls = _extract_urls(block)
        job_url = next((u for u in urls if 'linkedin.com/jobs' in u), None)
        if not job_url:
            continue
        jobs.append({
            "title": lines[0],
            "company": lines[1] if len(lines) > 1 else "",
            "location": lines[2] if len(lines) > 2 else "",
            "url": _clean_url(job_url),
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
