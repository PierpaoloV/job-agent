from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discovery_verification import verify_shortlist
from opportunity_domain import OfficialVacancyData, Runtime
from opportunity_workflow import HostedFetchBlocked
from workflow import ShortlistArtifact


class FixedClock:
    def now(self):
        return datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


class OfficialSource:
    def __init__(self, *, blocked=False):
        self.blocked = blocked
        self.calls = []

    def retrieve(self, lead, runtime):
        self.calls.append((lead, runtime))
        if self.blocked:
            raise HostedFetchBlocked("Glassdoor blocked")
        return OfficialVacancyData(
            official_job_id="align-ml-scientist",
            canonical_url="https://jobs.aligntech.com/postings/official-id",
            company="Align Technology",
            role="Machine Learning Scientist",
            team="Advanced Technology Development",
            location="Zürich",
            modality="Onsite",
            seniority="",
            compensation="",
            requirements=("Python", "PyTorch", "Computer vision"),
            ownership="unknown",
            sponsorship="not stated",
            description="Build computer vision and machine learning systems.",
            published_at="2026-07-24",
        )


def artifact() -> ShortlistArtifact:
    return ShortlistArtifact.from_dict({
        "version": "job-agent.shortlist.v1",
        "created_at": "2026-07-25T11:00:00+00:00",
        "opportunities": [{
            "stable_id": "glassdoor:1010206875020",
            "discovered_at": "2026-07-25T11:00:00+00:00",
            "source_confidence": "supported",
            "local_score": 0.8,
            "screening_reasons": ["research fit"],
            "shortlisted": True,
            "screening_outcome": "shortlisted",
            "job": {
                "dedup_key": "glassdoor:1010206875020",
                "url": "https://www.glassdoor.it/partner/jobListing.htm?jobListingId=1010206875020",
                "source": "Glassdoor",
                "title": "Machine Learning Scientist",
                "company": "Align Technology",
                "location": "Zürich",
                "snippet": "Align Technology Machine Learning Scientist Zürich",
            },
        }],
    })


def test_shortlisted_lead_is_replaced_by_exact_official_vacancy_before_grading():
    source = OfficialSource()

    verified = verify_shortlist(artifact(), source=source, clock=FixedClock())

    job = verified.opportunities[0].job
    assert source.calls[0][1] == Runtime.HOSTED
    assert job["verification_status"] == "verified"
    assert job["official_url"] == "https://jobs.aligntech.com/postings/official-id"
    assert job["official_description"].startswith("Build computer vision")
    assert job["requirements"] == ["Python", "PyTorch", "Computer vision"]
    assert job["official_vacancy_version"].startswith("sha256:")
    assert verified.opportunities[0].shortlisted is True


def test_hosted_block_keeps_shortlisted_lead_for_local_recovery():
    waiting = verify_shortlist(
        artifact(), source=OfficialSource(blocked=True), clock=FixedClock()
    )

    job = waiting.opportunities[0].job
    assert job["verification_status"] == "needs_local_fetch"
    assert job["official_description"] == ""
    assert waiting.opportunities[0].shortlisted is True
