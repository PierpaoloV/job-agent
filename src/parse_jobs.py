"""Parse raw email bodies into structured job listings."""
import hashlib
import re
from urllib.parse import unquote, urlparse, parse_qsl, urlencode, urlunparse
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

_NON_JOB_LINK_TEXT = (
    "view all",
    "see all",
    "voir toutes",
    "manage",
    "gerer",
    "gérer",
    "unsubscribe",
    "ne plus recevoir",
    "privacy",
    "confidentialit",
    "terms",
    "preferences",
    "alert",
    "avis",
    "view jobs",
    "since yesterday",
    "for last 7 days",
    "sign in",
    "accedi",
    "edit",
    "modifica",
    "create your indeed resume",
    "crea il tuo indeed cv",
    "annullare la tua iscrizione",
    "help center",
    "centro di supporto",
    "termini",
)

_JOB_HINTS = (
    "engineer",
    "scientist",
    "developer",
    "researcher",
    "architect",
    "manager",
    "lead",
    "specialist",
    "analyst",
    "consultant",
    "ml",
    "ai",
    "data",
    "software",
    "machine learning",
    "computer vision",
)


def _strip_tracking(href: str) -> str:
    """Remove known tracking query params; preserve path and essential params."""
    try:
        parts = urlparse(href)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k not in _TRACKING_PARAMS]
        return urlunparse(parts._replace(query=urlencode(query)))
    except Exception:
        return href


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, limit: int) -> str:
    text = _clean_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def _is_noise_segment(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _NON_JOB_LINK_TEXT)


def _anchor_context(anchor) -> tuple[str, list[str]]:
    ancestor = anchor
    for _ in range(4):
        if ancestor.parent is None:
            break
        ancestor = ancestor.parent

    text = _clean_text(ancestor.get_text(" ", strip=True)) if ancestor else ""
    segments = []
    if ancestor:
        seen = set()
        for segment in ancestor.stripped_strings:
            cleaned = _clean_text(segment)
            key = cleaned.lower()
            if cleaned and key not in seen and not _is_noise_segment(cleaned):
                seen.add(key)
                segments.append(cleaned)
    if segments:
        text = _clean_text(" ".join(segments))
    return text, segments


def _infer_job_fields(title: str, segments: list[str], context: str) -> dict:
    lower_context = context.lower()
    company, location = "", ""
    candidate_segments = [s for s in segments if s != title and len(s) > 1 and not _is_noise_segment(s)]

    if candidate_segments:
        split_parts = [
            _clean_text(part)
            for part in re.split(r"\s+(?:[-–|•·])\s+", candidate_segments[0])
            if _clean_text(part)
        ]
        if len(split_parts) >= 2:
            company = split_parts[0]
            location = split_parts[1]
        else:
            company = candidate_segments[0]

    for segment in candidate_segments:
        if segment != company:
            location = location or segment
            break

    salary_match = re.search(
        r"(?:€|eur|usd|\$|£)\s?[0-9][0-9,.\skK]*(?:\s?[-–]\s?(?:€|eur|usd|\$|£)?\s?[0-9][0-9,.\skK]*)?",
        context,
        re.IGNORECASE,
    )
    remote_policy = ""
    for label in ("remote", "hybrid", "on-site", "onsite", "relocation"):
        if label in lower_context:
            remote_policy = label
            break

    seniority = ""
    for label in ("intern", "internship", "stage", "junior", "senior", "lead", "principal", "staff", "mid-level"):
        if label in lower_context:
            seniority = label
            break

    skills = []
    for skill in ("python", "pytorch", "tensorflow", "machine learning", "deep learning", "computer vision", "llm", "mlops", "docker", "kubernetes", "aws", "gcp", "azure", "sql"):
        if skill in lower_context:
            skills.append(skill)

    return {
        "company": company,
        "location": location,
        "snippet": _truncate(context, 260),
        "raw_email_context": _truncate(context, 1000),
        "salary": salary_match.group(0) if salary_match else "",
        "remote_policy": remote_policy,
        "seniority": seniority,
        "required_skills": skills,
    }


def _infer_indeed_card_fields(title: str, segments: list[str], context: str) -> dict:
    """Infer Indeed fields without mistaking its standalone rating for location."""
    fields = _infer_job_fields(title, segments, context)
    candidates = [
        segment
        for segment in segments
        if segment != title
        and len(segment) > 1
        and not _is_noise_segment(segment)
    ]
    if not candidates:
        return fields

    combined = [
        _clean_text(part)
        for part in re.split(r"\s+(?:[-–|•·])\s+", candidates[0])
        if _clean_text(part)
    ]
    if len(combined) >= 2:
        fields["company"] = combined[0]
        fields["location"] = combined[1]
        return fields

    fields["company"] = candidates[0]
    remaining = candidates[1:]
    if remaining and re.fullmatch(
        r"\d(?:[.,]\d)?(?:\s*[★☆])?", remaining[0]
    ):
        remaining = remaining[1:]
    if remaining:
        fields["location"] = re.sub(
            r"^\s*[-–|•·]\s*", "", remaining[0]
        ).strip()
    else:
        fields["location"] = ""
    return fields


def _find_embedded_url(href: str, domain: str) -> str | None:
    """Return an embedded destination URL from a tracked email link."""
    try:
        parts = urlparse(href)
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in {"url", "u", "target", "redirect", "redirect_url", "destination"}:
                decoded_value = unquote(value)
                if domain in decoded_value:
                    return decoded_value
    except Exception:
        pass

    decoded_href = unquote(href)
    m = re.search(r'https?://(?:[^/"\'>\s]+\.)?' + re.escape(domain) + r'[^"\'>\s]*', decoded_href)
    if m:
        return m.group(0)

    return None


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


def _canonicalize_wttj(href: str) -> tuple[str, str] | None:
    target = _find_embedded_url(href, "welcometothejungle.com") or href
    display_url = _strip_tracking(target)

    try:
        parts = urlparse(display_url)
    except Exception:
        return None

    if "welcometothejungle.com" not in parts.netloc.lower() or "/jobs/" not in parts.path:
        return None

    path = parts.path.rstrip("/")
    display_url = urlunparse(parts._replace(scheme="https", netloc=parts.netloc.lower(), path=path, query="", fragment=""))
    dedup_key = f"wttj:{path}"
    return dedup_key, display_url


def _canonicalize_wttj_tracking(href: str) -> tuple[str, str] | None:
    try:
        parts = urlparse(href)
    except Exception:
        return None

    if not parts.netloc.lower().endswith("welcometothejungle.com") or parts.path != "/ls/click":
        return None

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    upn = query.get("upn", "")
    if not upn:
        return None

    destination_token = upn.split("_cs", 1)[0]
    m = re.match(r"(.+(?:-3D-3D|-3D))[A-Za-z0-9_-]{4}$", destination_token)
    if m:
        destination_token = m.group(1)

    display_url = urlunparse(parts._replace(fragment=""))
    return f"wttj-tracking:{destination_token}", display_url


def _is_wttj_job_url(href: str) -> bool:
    decoded_href = unquote(href)
    if "welcometothejungle.com" in decoded_href and "/jobs/" in decoded_href:
        return True
    try:
        parts = urlparse(href)
    except Exception:
        return False
    return parts.netloc.lower().endswith("welcometothejungle.com") and parts.path == "/ls/click"


def _canonicalize_wttj_any(href: str) -> tuple[str, str] | None:
    return _canonicalize_wttj(href) or _canonicalize_wttj_tracking(href)


def _extract_jobs(
    body: str,
    url_predicate,
    canonicalize,
    source: str,
    *,
    prefer_compact_title: bool = False,
) -> list[dict]:
    """Group anchors by canonical ID, select a title anchor, then walk up
    ancestors for company and location context."""
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
        text_entries = [
            entry for entry in entries
            if _clean_text(entry[0].get_text(" ", strip=True))
        ]
        if not text_entries:
            continue
        choose = min if prefer_compact_title else max
        best_a, display_url = choose(
            text_entries, key=lambda e: len(_clean_text(e[0].get_text(" ", strip=True)))
        )
        title = _clean_text(best_a.get_text(" ", strip=True))
        if not title:
            continue

        context, segments = _anchor_context(best_a)
        fields = _infer_job_fields(title, segments, context)

        jobs.append({
            "title": title,
            "company": fields["company"],
            "location": fields["location"],
            "url": display_url,
            "dedup_key": dedup_key,
            "source": source,
            "snippet": fields["snippet"],
            "raw_email_context": fields["raw_email_context"],
            "salary": fields["salary"],
            "remote_policy": fields["remote_policy"],
            "seniority": fields["seniority"],
            "required_skills": fields["required_skills"],
        })

    return jobs


def _canonicalize_generic(href: str) -> tuple[str, str] | None:
    try:
        parts = urlparse(href)
    except Exception:
        return None

    if not parts.scheme.startswith("http") or not parts.netloc:
        return None

    display_url = _strip_tracking(href)
    parts = urlparse(display_url)
    path = parts.path.rstrip("/") or parts.path
    display_url = urlunparse(parts._replace(path=path, fragment=""))
    dedup_key = urlunparse(parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), path=path, query="", fragment=""))
    return dedup_key, display_url


def _looks_like_generic_job_link(href: str, text: str) -> bool:
    lower_href = unquote(href).lower()
    lower_text = text.lower()
    if any(marker in lower_href for marker in ("unsubscribe", "privacy", "preferences", "manage-alert", "terms")):
        return False
    if any(marker in lower_text for marker in _NON_JOB_LINK_TEXT):
        return False
    return (
        "/job" in lower_href
        or "/jobs" in lower_href
        or "/vacancy" in lower_href
        or "/careers" in lower_href
        or any(hint in lower_text for hint in _JOB_HINTS)
    )


def _parse_fallback_email(body: str, source: str) -> list[dict]:
    soup = BeautifulSoup(body, "html.parser")
    key_to_entries: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        text = _clean_text(a.get_text(" ", strip=True))
        if not text or not _looks_like_generic_job_link(a["href"], text):
            continue
        result = _canonicalize_generic(a["href"])
        if not result:
            continue
        dedup_key, display_url = result
        key_to_entries.setdefault(dedup_key, []).append((a, display_url))

    jobs = []
    for dedup_key, entries in key_to_entries.items():
        best_a, display_url = max(entries, key=lambda e: len(e[0].get_text(strip=True)))
        title = _clean_text(best_a.get_text(" ", strip=True))
        if len(title) < 4 or any(marker in title.lower() for marker in _NON_JOB_LINK_TEXT):
            continue
        context, segments = _anchor_context(best_a)
        fields = _infer_job_fields(title, segments, context)
        jobs.append({
            "title": title,
            "company": fields["company"],
            "location": fields["location"],
            "url": display_url,
            "dedup_key": dedup_key,
            "source": source,
            "snippet": fields["snippet"],
            "raw_email_context": fields["raw_email_context"],
            "salary": fields["salary"],
            "remote_policy": fields["remote_policy"],
            "seniority": fields["seniority"],
            "required_skills": fields["required_skills"],
        })
    return jobs


def _parse_linkedin_email(body: str) -> list[dict]:
    return _extract_jobs(
        body,
        url_predicate=lambda h: "/jobs/view/" in h,
        canonicalize=_canonicalize_linkedin,
        source="LinkedIn",
        prefer_compact_title=True,
    )


def _parse_indeed_email(body: str) -> list[dict]:
    direct = _extract_jobs(
        body,
        url_predicate=lambda h: "indeed." in h and ("/viewjob" in h or "/rc/clk" in h or "/job/" in h),
        canonicalize=_canonicalize_indeed,
        source="Indeed",
    )
    opaque = _parse_indeed_engage_email(body)
    known = {job["dedup_key"] for job in direct}
    return direct + [
        job for job in opaque if job["dedup_key"] not in known
    ]


def _parse_indeed_engage_email(body: str) -> list[dict]:
    """Recover cards whose destination is hidden behind an opaque email link."""
    soup = BeautifulSoup(body, "html.parser")
    grouped: dict[str, list] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        try:
            parts = urlparse(href)
        except Exception:
            continue
        if (
            parts.hostname != "engage.indeed.com"
            or not parts.path.startswith("/f/a/")
        ):
            continue
        digest = hashlib.sha256(href.encode("utf-8")).hexdigest()
        grouped.setdefault(f"indeed-engage:{digest}", []).append(anchor)

    jobs = []
    for dedup_key, anchors in grouped.items():
        text_anchors = [
            anchor
            for anchor in anchors
            if _clean_text(anchor.get_text(" ", strip=True))
        ]
        if not text_anchors:
            continue
        best = max(
            text_anchors,
            key=lambda anchor: len(
                _clean_text(anchor.get_text(" ", strip=True))
            ),
        )
        card_segments = [
            _clean_text(value)
            for value in best.stripped_strings
            if _clean_text(value)
        ]
        if not card_segments:
            continue
        title = card_segments[0]
        if (
            len(title) < 4
            or _is_noise_segment(title)
            or title.casefold()
            in {
                "sign in",
                "accedi",
                "view job",
                "visualizza offerta",
                "apply now",
                "candidati ora",
                "download the app",
            }
        ):
            continue
        context = _clean_text(" ".join(card_segments))
        fields = _infer_indeed_card_fields(title, card_segments, context)
        jobs.append(
            {
                "title": title,
                "company": fields["company"],
                "location": fields["location"],
                # Never persist a recipient-specific tracking link. The
                # official resolver replaces this source landing page before
                # a role can be graded or shown as actionable.
                "url": "https://www.indeed.com/",
                "dedup_key": dedup_key,
                "source": "Indeed",
                "snippet": fields["snippet"],
                "raw_email_context": fields["raw_email_context"],
                "salary": fields["salary"],
                "remote_policy": fields["remote_policy"],
                "seniority": fields["seniority"],
                "required_skills": fields["required_skills"],
            }
        )
    return jobs


def _parse_glassdoor_email(body: str) -> list[dict]:
    soup = BeautifulSoup(body, "html.parser")
    jobs = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not (
            "glassdoor." in href
            and (
                "/job-listing" in href
                or "/partner/" in href
                or "/job/" in href
            )
        ):
            continue
        canonical = _canonicalize_glassdoor(href)
        if canonical is None:
            continue
        dedup_key, display_url = canonical
        if dedup_key in seen:
            continue
        segments = [_clean_text(value) for value in anchor.stripped_strings]
        company, title, location = _glassdoor_card_fields(segments)
        if not title:
            continue
        seen.add(dedup_key)
        context = _clean_text(" ".join(segments))
        fields = _infer_job_fields(title, segments, context)
        jobs.append({
            "title": title,
            "company": company or fields["company"],
            "location": location or fields["location"],
            "url": display_url,
            "dedup_key": dedup_key,
            "source": "Glassdoor",
            "snippet": fields["snippet"],
            "raw_email_context": fields["raw_email_context"],
            "salary": fields["salary"],
            "remote_policy": fields["remote_policy"],
            "seniority": fields["seniority"],
            "required_skills": fields["required_skills"],
        })
    return jobs


def _glassdoor_card_fields(segments: list[str]) -> tuple[str, str, str]:
    values = [
        value
        for value in segments
        if value and not _is_noise_segment(value)
    ]
    if not values:
        return "", "", ""
    if len(values) == 1:
        return "", values[0], ""
    company = values[0]
    candidates = [
        (index, value)
        for index, value in enumerate(values[1:], start=1)
        if not re.fullmatch(r"\d(?:[.,]\d)?\s*★", value)
        and not re.fullmatch(
            r"(?:\d+\s*)?(?:gg|giorni?|ore?|h|d|days?|hours?)",
            value,
            re.IGNORECASE,
        )
        and "candidatura" not in value.casefold()
        and "easy apply" not in value.casefold()
    ]
    if not candidates:
        return company, "", ""
    title_index, title = next(
        (
            item
            for item in candidates
            if any(hint in item[1].casefold() for hint in _JOB_HINTS)
        ),
        candidates[0],
    )
    location = next(
        (value for index, value in candidates if index > title_index),
        "",
    )
    return company, title, location


def _parse_wttj_email(body: str) -> list[dict]:
    jobs = _extract_jobs(
        body,
        url_predicate=_is_wttj_job_url,
        canonicalize=_canonicalize_wttj_any,
        source="Welcome to the Jungle",
    )
    non_job_titles = (
        "voir toutes les offres",
        "gerer mes alertes",
        "gérer mes alertes",
        "donner mon avis",
        "ne plus recevoir",
        "laissez votre avis",
        "politique de confidentialite",
        "politique de confidentialité",
    )
    return [
        job for job in jobs
        if not any(job["title"].lower().startswith(title) for title in non_job_titles)
    ]


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
        elif "welcometothejungle" in sender or "wttj" in sender:
            jobs = _parse_wttj_email(body)
        elif "eurotechjobs" in sender:
            jobs = _parse_fallback_email(body, source="EuroTechJobs")
        else:
            jobs = _parse_fallback_email(body, source=email.get("from", "Unknown"))

        for job in jobs:
            job["email_date"] = email.get("date", "")
            job["posted_at"] = datetime.now().isoformat()

        all_jobs.extend(jobs)

    print(f"Parsed {len(all_jobs)} job listings from {len(emails)} emails")
    return all_jobs
