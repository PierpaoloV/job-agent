"""Official-vacancy resolver using public search results and JobPosting data."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from bs4 import BeautifulSoup
import requests

from opportunity_domain import OfficialVacancyData, Runtime
from opportunity_sources import OpportunityLead
from opportunity_workflow import HostedFetchBlocked, OfficialVacancyUnavailable


_SEARCH_URLS = (
    "https://html.duckduckgo.com/html/",
    "https://search.brave.com/search",
)
_EXCLUDED_HOSTS = (
    "glassdoor.",
    "indeed.",
    "linkedin.",
    "jobrapido.",
    "talent.com",
    "jooble.",
    "trabajo.",
    "ziprecruiter.",
    "swissaijob.",
)
_KNOWN_ATS_HOSTS = (
    "ashbyhq.com",
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "smartrecruiters.com",
)
_COMPANY_SCOPED_ATS_HOSTS = (
    "jobs.gem.com",
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class SearchOfficialSource:
    """Find a matching employer page, then parse its JobPosting JSON-LD."""

    def __init__(self, *, http=requests, timeout: int = 15) -> None:
        self._http = http
        self._timeout = timeout

    def retrieve(
        self, lead: OpportunityLead, runtime: Runtime
    ) -> OfficialVacancyData:
        query = " ".join(
            value
            for value in (
                f'"{lead.title}"',
                f'"{lead.company}"',
                lead.location,
                "jobs careers",
            )
            if value.strip('"')
        )
        blocked = 0
        for search_url in _SEARCH_URLS:
            query_parameters = {"q": query}
            if "brave.com" in search_url:
                query_parameters["source"] = "web"
            try:
                response = self._http.get(
                    f"{search_url}?{urlencode(query_parameters)}",
                    headers=_HEADERS,
                    timeout=self._timeout,
                )
            except requests.RequestException:
                blocked += 1
                continue
            if response.status_code in {202, 403, 429}:
                blocked += 1
                continue
            if response.status_code != 200:
                continue
            for candidate in _search_result_urls(response.text):
                if _excluded(candidate):
                    continue
                vacancy = self._candidate(candidate, lead)
                if vacancy is not None:
                    return vacancy
        if blocked == len(_SEARCH_URLS):
            raise _retrieval_error(runtime, "public search was blocked")
        raise OfficialVacancyUnavailable(
            "no matching official employer vacancy was found"
        )

    def _candidate(
        self, url: str, lead: OpportunityLead
    ) -> OfficialVacancyData | None:
        try:
            response = self._http.get(
                url,
                headers=_HEADERS,
                timeout=self._timeout,
                allow_redirects=True,
            )
        except requests.RequestException:
            return None
        if response.status_code != 200 or _excluded(response.url):
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        for posting in _job_postings(soup):
            vacancy = _to_vacancy(posting, response.url, lead)
            if vacancy is not None:
                return vacancy
        return None


def _retrieval_error(runtime: Runtime, message: str) -> RuntimeError:
    if Runtime(runtime) == Runtime.HOSTED:
        return HostedFetchBlocked(message)
    return OfficialVacancyUnavailable(message)


def _search_result_urls(body: str) -> tuple[str, ...]:
    soup = BeautifulSoup(body, "html.parser")
    urls = []
    for anchor in soup.select("a.result__a[href]"):
        href = str(anchor["href"])
        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc:
            values = parse_qs(parsed.query).get("uddg", ())
            if values:
                href = unquote(values[0])
        elif href.startswith("//duckduckgo.com/"):
            values = parse_qs(urlparse(f"https:{href}").query).get("uddg", ())
            if values:
                href = unquote(values[0])
        if href.startswith("http") and href not in urls:
            urls.append(href)
    for encoded in re.findall(r'"url":"(https?[^"]+)"', body):
        try:
            href = json.loads(f'"{encoded}"')
        except json.JSONDecodeError:
            continue
        if href.startswith("http") and href not in urls:
            urls.append(href)
    return tuple(urls[:20])


def _excluded(url: str) -> bool:
    host = urlparse(url).netloc.casefold()
    return any(marker in host for marker in _EXCLUDED_HOSTS)


def _job_postings(soup: BeautifulSoup) -> Iterable[Mapping[str, Any]]:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        yield from _walk_job_postings(value)


def _walk_job_postings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        kind = value.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "JobPosting" in kinds:
            yield value
        for child in value.values():
            yield from _walk_job_postings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_job_postings(child)


def _to_vacancy(
    posting: Mapping[str, Any],
    page_url: str,
    lead: OpportunityLead,
) -> OfficialVacancyData | None:
    role = _text(posting.get("title"))
    organization = posting.get("hiringOrganization")
    company = (
        _text(organization.get("name"))
        if isinstance(organization, Mapping)
        else ""
    )
    if not role or not _matches(role, lead.title):
        return None
    if company and lead.company and not _matches(company, lead.company):
        return None
    if not _official_origin(posting, page_url, company or lead.company):
        return None
    raw_description = posting.get("description")
    description = _plain(raw_description)
    if len(description) < 120:
        return None
    canonical_url = _text(posting.get("url")) or page_url
    identifier = posting.get("identifier")
    if isinstance(identifier, Mapping):
        official_id = _text(identifier.get("value"))
    else:
        official_id = _text(identifier)
    requirements = _requirements(posting)
    if not requirements:
        requirements = _description_requirements(raw_description)
    location = _location(posting) or lead.location
    lead_city = re.split(r"[,|]", lead.location, maxsplit=1)[0].strip()
    if lead_city and _fold(lead_city) in _fold(description):
        location = lead.location
    return OfficialVacancyData(
        official_job_id=official_id or canonical_url,
        canonical_url=canonical_url,
        company=company or lead.company,
        role=role,
        team=_text(posting.get("department")),
        location=location,
        modality=_modality(posting),
        seniority=_text(posting.get("experienceRequirements")),
        compensation=_compensation(posting.get("baseSalary")),
        requirements=requirements,
        ownership="unknown",
        sponsorship="not_stated",
        description=description,
        published_at=_text(posting.get("datePosted")) or None,
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return re.sub(r"\s+", " ", str(value)).strip()
    return ""


def _official_origin(
    posting: Mapping[str, Any],
    page_url: str,
    company: str,
) -> bool:
    page_host = urlparse(page_url).netloc.casefold().split(":", 1)[0]
    if is_official_company_url(page_url, company):
        return True
    organization = posting.get("hiringOrganization")
    if isinstance(organization, Mapping):
        for key in ("url", "sameAs"):
            values = organization.get(key)
            links = values if isinstance(values, list) else [values]
            for link in links:
                host = urlparse(_text(link)).netloc.casefold()
                if host and _registrable_domain(host) == _registrable_domain(
                    page_host
                ):
                    return True
    return False


def is_official_company_url(url: str, company: str) -> bool:
    if _excluded(url):
        return False
    parsed = urlparse(url)
    page_host = parsed.netloc.casefold().split(":", 1)[0]
    if not page_host:
        return False
    company_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", _fold(company))
        if len(token) >= 4 and token not in {"technology", "technologies"}
    }
    if page_host in _COMPANY_SCOPED_ATS_HOSTS:
        path_tokens = set(re.findall(r"[a-z0-9]+", _fold(parsed.path)))
        return bool(company_tokens & path_tokens)
    if any(marker in page_host for marker in _KNOWN_ATS_HOSTS):
        return True
    return any(token in page_host for token in company_tokens)


def _registrable_domain(host: str) -> str:
    labels = [part for part in host.split(".") if part and part != "www"]
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _plain(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        BeautifulSoup(html.unescape(_text(value)), "html.parser").get_text(" "),
    ).strip()


def _matches(left: str, right: str) -> bool:
    def tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", _fold(value))
            if len(token) > 1
            and token not in {"senior", "sr", "junior", "jr", "the", "and"}
        }

    first, second = tokens(left), tokens(right)
    if not first or not second:
        return False
    return len(first & second) / min(len(first), len(second)) >= 0.6


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore"
    ).decode("ascii").casefold()


def _location(posting: Mapping[str, Any]) -> str:
    value = posting.get("jobLocation")
    locations = value if isinstance(value, list) else [value]
    for item in locations:
        if not isinstance(item, Mapping):
            continue
        address = item.get("address")
        if not isinstance(address, Mapping):
            continue
        parts = [
            _text(address.get(key))
            for key in ("addressLocality", "addressRegion", "addressCountry")
        ]
        result = ", ".join(part for part in parts if part)
        if result:
            return result
    return ""


def _modality(posting: Mapping[str, Any]) -> str:
    value = _text(posting.get("jobLocationType"))
    return "Remote" if "telecommute" in value.casefold() else value


def _requirements(posting: Mapping[str, Any]) -> tuple[str, ...]:
    values = []
    for key in (
        "qualifications",
        "skills",
        "experienceRequirements",
        "educationRequirements",
    ):
        value = posting.get(key)
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = _plain(item)
            if text and text not in values:
                values.append(text)
    return tuple(values)


def _description_requirements(value: Any) -> tuple[str, ...]:
    soup = BeautifulSoup(html.unescape(_text(value)), "html.parser")
    heading = next(
        (
            item
            for item in soup.find_all(re.compile(r"^h[1-6]$"))
            if any(
                marker in item.get_text(" ", strip=True).casefold()
                for marker in (
                    "requirement",
                    "qualification",
                    "skills",
                    "knowledge",
                    "expertise",
                )
            )
        ),
        None,
    )
    if heading is None:
        return ()
    requirements = []
    for sibling in heading.find_next_siblings():
        if re.fullmatch(r"h[1-6]", sibling.name or ""):
            break
        for item in sibling.find_all("li"):
            text = re.sub(
                r"\s+", " ", item.get_text(" ", strip=True)
            ).strip()
            if text and text not in requirements:
                requirements.append(text)
    return tuple(requirements)


def _compensation(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, Mapping):
        return json.dumps(value, sort_keys=True)
    return _text(value)


__all__ = ["SearchOfficialSource", "is_official_company_url"]
