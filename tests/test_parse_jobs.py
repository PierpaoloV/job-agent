import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parse_jobs import parse_emails


def test_wttj_direct_and_wrapped_links():
    body = """
    <html><body>
      <a href="https://www.welcometothejungle.com/en/companies/acme/jobs/ml-engineer_milan?utm_source=email">
        Machine Learning Engineer
      </a>
      <div>Acme AI</div><div>Milan, Italy</div>
      <a href="https://tracking.example/click?url=https%3A%2F%2Fwww.welcometothejungle.com%2Fen%2Fcompanies%2Fbeta%2Fjobs%2Fcomputer-vision-scientist_remote%3Futm_campaign%3Dalert">
        Computer Vision Scientist
      </a>
      <div>Beta Health</div><div>Remote</div>
    </body></html>
    """
    jobs = parse_emails([{
        "from": "Welcome to the Jungle <alerts@welcometothejungle.com>",
        "date": "Thu, 23 Apr 2026",
        "body": body,
    }])

    assert [job["title"] for job in jobs] == [
        "Machine Learning Engineer",
        "Computer Vision Scientist",
    ]
    assert all(job["source"] == "Welcome to the Jungle" for job in jobs)
    assert jobs[0]["dedup_key"] == "wttj:/en/companies/acme/jobs/ml-engineer_milan"
    assert jobs[1]["url"] == "https://www.welcometothejungle.com/en/companies/beta/jobs/computer-vision-scientist_remote"


def test_wttj_tracking_links_group_to_one_job():
    first = "http://t.welcometothejungle.com/ls/click?upn=u001.same-destination-3DABCD_csTrack"
    body = f"""
    <html><body>
      <a href="{first}">Kingfisher</a>
      <a href="{first.replace('ABCD', 'WXYZ')}">Machine Learning Engineer</a>
      <a href="{first.replace('ABCD', 'IJKL')}">CDI - London</a>
      <a href="http://t.welcometothejungle.com/ls/click?upn=u001.footer-3DZZZZ_csTrack">
        Voir toutes les offres
      </a>
    </body></html>
    """
    jobs = parse_emails([{
        "from": "Welcome to the Jungle <alerts@welcometothejungle.com>",
        "date": "Thu, 23 Apr 2026",
        "body": body,
    }])

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Machine Learning Engineer"
    assert jobs[0]["company"] == "Kingfisher"
    assert jobs[0]["location"] == "CDI - London"
    assert jobs[0]["dedup_key"] == "wttj-tracking:u001.same-destination-3D"


def test_eurotechjobs_fallback_extracts_context_fields():
    body = """
    <html><body>
      <div>
        <a href="https://www.eurotechjobs.com/job_display/123/Machine_Learning_Engineer">
          Machine Learning Engineer
        </a>
        <p>Acme Robotics - Milan, Italy - Remote - Python PyTorch - EUR 65000</p>
      </div>
    </body></html>
    """
    jobs = parse_emails([{
        "from": "EuroTechJobs <alerts@eurotechjobs.com>",
        "date": "Thu, 23 Apr 2026",
        "body": body,
    }])

    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "EuroTechJobs"
    assert job["company"] == "Acme Robotics"
    assert job["location"] == "Milan, Italy"
    assert job["salary"] == "EUR 65000"
    assert job["remote_policy"] == "remote"
    assert job["required_skills"] == ["python", "pytorch", "machine learning"]
    assert "Acme Robotics" in job["raw_email_context"]


def test_unknown_sender_fallback_skips_footer_links():
    body = """
    <html><body>
      <div>
        <a href="https://example.com/jobs/42?utm_source=email">Computer Vision Scientist</a>
        <span>Beta Health - Remote - Python computer vision</span>
      </div>
      <a href="https://example.com/unsubscribe">unsubscribe</a>
    </body></html>
    """
    jobs = parse_emails([{
        "from": "Unknown Jobs <jobs@example.com>",
        "date": "Thu, 23 Apr 2026",
        "body": body,
    }])

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Computer Vision Scientist"
    assert jobs[0]["company"] == "Beta Health"
    assert "unsubscribe" not in jobs[0]["raw_email_context"].lower()
