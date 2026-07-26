from pathlib import Path
import json
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opportunity_domain import Runtime
from opportunity_sources import OpportunityLead
from search_official_source import SearchOfficialSource, is_official_company_url


class Response:
    def __init__(self, text, *, url, status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code


class Http:
    def __init__(self, official):
        self.official = official
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "duckduckgo.com" in url:
            official = quote(
                "https://jobs.aligntech.com/postings/current",
                safe="",
            )
            return Response(
                f'<a class="result__a" '
                f'href="//duckduckgo.com/l/?uddg={official}">Role</a>',
                url=url,
            )
        if "search.brave.com" in url:
            return Response("", url=url)
        return Response(
            self.official,
            url="https://jobs.aligntech.com/postings/current",
        )


def lead():
    return OpportunityLead(
        stable_id="glassdoor:42",
        source="Glassdoor",
        source_confidence="supported",
        canonical_url="https://glassdoor.example/42",
        title="Machine Learning Scientist",
        company="Align Technology",
        location="Zürich",
        modality="",
        snippet="",
        email_received_at=None,
        discovered_at="2026-07-25T00:00:00+00:00",
        published_at=None,
    )


def test_search_resolves_official_json_ld_and_prefers_explicit_work_city():
    posting = {
        "@type": "JobPosting",
        "identifier": {"value": "current"},
        "url": "https://jobs.aligntech.com/postings/current",
        "title": "Machine Learning Scientist",
        "hiringOrganization": {"name": "Align Technology"},
        "jobLocation": {
            "address": {
                "addressLocality": "Rotkreuz",
                "addressCountry": "CH",
            }
        },
        "datePosted": "2026-07-22",
        "description": """
          <p>This position is full-time based in Zurich. Work on applied
          machine learning research with a product team and clinicians.</p>
          <h3>Skills, Knowledge &amp; Expertise</h3>
          <ul><li>Python and PyTorch</li><li>Computer vision</li></ul>
        """,
    }
    body = (
        '<script type="application/ld+json">'
        + json.dumps(posting)
        + "</script>"
    )

    vacancy = SearchOfficialSource(http=Http(body)).retrieve(
        lead(), Runtime.HOSTED
    )

    assert vacancy.official_job_id == "current"
    assert vacancy.canonical_url.endswith("/postings/current")
    assert vacancy.location == "Zürich"
    assert vacancy.requirements == ("Python and PyTorch", "Computer vision")
    assert "based in Zurich" in vacancy.description


def test_aggregator_jobposting_cannot_impersonate_the_employer():
    posting = {
        "@type": "JobPosting",
        "title": "Machine Learning Scientist",
        "hiringOrganization": {"name": "Align Technology"},
        "description": "Machine learning role " * 20,
    }
    body = (
        '<script type="application/ld+json">'
        + json.dumps(posting)
        + "</script>"
    )
    http = Http(body)
    http.get = lambda url, **kwargs: (
        Response(
            '<a class="result__a" href="https://swissaijob.ch/jobs/align">'
            "Role</a>",
            url=url,
        )
        if "duckduckgo.com" in url
        else Response(body, url="https://swissaijob.ch/jobs/align")
    )

    try:
        SearchOfficialSource(http=http).retrieve(lead(), Runtime.HOSTED)
    except Exception as error:
        assert type(error).__name__ == "OfficialVacancyUnavailable"
    else:
        raise AssertionError("aggregator was accepted as the official employer")


def test_brave_fallback_recovers_when_duckduckgo_is_rate_limited():
    posting = {
        "@type": "JobPosting",
        "identifier": {"value": "current"},
        "url": "https://jobs.aligntech.com/postings/current",
        "title": "Machine Learning Scientist",
        "hiringOrganization": {
            "name": "Align Technology",
            "url": "https://aligntech.com",
        },
        "description": "Official machine learning vacancy. " * 10,
    }
    official = (
        '<script type="application/ld+json">'
        + json.dumps(posting)
        + "</script>"
    )

    class FallbackHttp:
        def get(self, url, **kwargs):
            if "duckduckgo.com" in url:
                return Response("", url=url, status_code=202)
            if "search.brave.com" in url:
                return Response(
                    '{"url":"https://jobs.aligntech.com/postings/current"}',
                    url=url,
                )
            return Response(
                official,
                url="https://jobs.aligntech.com/postings/current",
            )

    vacancy = SearchOfficialSource(http=FallbackHttp()).retrieve(
        lead(), Runtime.HOSTED
    )

    assert vacancy.official_job_id == "current"


def test_company_scoped_gem_url_is_an_official_ats_boundary():
    assert is_official_company_url(
        "https://jobs.gem.com/rivia/am9icG9zdDpX6tPeu4scKBFrmPoeoZ57",
        "Rivia",
    )


def test_gem_url_cannot_impersonate_a_different_employer():
    assert not is_official_company_url(
        "https://jobs.gem.com/other-company/am9icG9zdDpX6tPeu4scKBFrmPoeoZ57",
        "Rivia",
    )


def test_company_scoped_speedinvest_job_board_is_an_official_boundary():
    assert is_official_company_url(
        "https://careers.speedinvest.com/companies/rivia/jobs/67935149-senior-ai-engineer",
        "Rivia",
    )


def test_speedinvest_job_board_cannot_impersonate_a_portfolio_company():
    assert not is_official_company_url(
        "https://careers.speedinvest.com/companies/other-company/jobs/67935149-senior-ai-engineer",
        "Rivia",
    )
