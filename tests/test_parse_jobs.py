import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parse_jobs import parse_emails


def test_glassdoor_current_alert_card_splits_company_rating_title_and_location():
    job_url = (
        "https://www.glassdoor.it/partner/jobListing.htm"
        "?jobListingId=1010206875020&utm_source=jobalert"
    )
    body = f"""
    <html><body>
      <table><tbody><tr><td>
        <a href="{job_url}">
          <span>Align Technology</span>
          <span>3.5 ★</span>
          <span>Machine Learning Scientist</span>
          <span>Zürich</span>
          <span>Candidatura semplice</span>
          <span>3 gg</span>
        </a>
      </td></tr></tbody></table>
    </body></html>
    """

    jobs = parse_emails([{
        "from": "Glassdoor <noreply@glassdoor.com>",
        "date": "Sat, 25 Jul 2026",
        "body": body,
    }])

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Machine Learning Scientist"
    assert jobs[0]["company"] == "Align Technology"
    assert jobs[0]["location"] == "Zürich"
    assert jobs[0]["dedup_key"] == "glassdoor:1010206875020"
    assert jobs[0]["url"].startswith(
        "https://www.glassdoor.it/partner/jobListing.htm?jobListingId=1010206875020"
    )


def test_linkedin_current_alert_card_uses_compact_title_and_middle_dot_fields():
    job_url = "https://www.linkedin.com/jobs/view/4434637079?trk=email_job_alert"
    body = f"""
    <html><body>
      <table><tbody><tr>
        <td><a href="{job_url}"><img src="logo.png"></a></td>
        <td>
          <a href="{job_url}">
            <span>Digital Pathologist</span>
            <span>Roche · Basel, Basel, Switzerland</span>
            <span>Actively recruiting</span>
          </a>
          <a href="{job_url}">Digital Pathologist</a>
        </td>
      </tr></tbody></table>
    </body></html>
    """

    jobs = parse_emails([{
        "from": "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        "date": "Sat, 25 Jul 2026",
        "body": body,
    }])

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Digital Pathologist"
    assert jobs[0]["company"] == "Roche"
    assert jobs[0]["location"] == "Basel, Basel, Switzerland"
    assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/4434637079"


def test_indeed_opaque_engage_links_become_redacted_deduplicated_leads():
    opaque_url = (
        "https://engage.indeed.com/f/a/"
        "recipient-specific-token~~/campaign-token~/opaque-destination"
    )
    body = f"""
    <html><body>
      <table><tr><td>
        <a href="{opaque_url}">
          <span>AI Research Engineer</span>
          <span>Acme Robotics - Zürich, Switzerland</span>
          <span>Build Python and PyTorch research systems.</span>
          <span>2 days ago</span>
        </a>
        <a href="{opaque_url}">View job</a>
      </td></tr></table>
      <a href="{opaque_url.replace('opaque-destination', 'footer-token')}">
        Manage job alerts
      </a>
    </body></html>
    """

    jobs = parse_emails([{
        "from": "Indeed Job Alerts <campaign@engage.indeed.com>",
        "date": "Sat, 25 Jul 2026",
        "body": body,
    }])

    assert len(jobs) == 1
    assert jobs[0]["title"] == "AI Research Engineer"
    assert jobs[0]["source"] == "Indeed"
    assert jobs[0]["dedup_key"].startswith("indeed-engage:")
    assert "recipient-specific-token" not in jobs[0]["dedup_key"]
    assert jobs[0]["url"] == "https://www.indeed.com/"
    assert jobs[0]["company"] == "Acme Robotics"
    assert jobs[0]["location"] == "Zürich, Switzerland"


def test_indeed_card_skips_standalone_rating_before_location():
    opaque_url = (
        "https://engage.indeed.com/f/a/"
        "recipient-specific-token~~/campaign-token~/opaque-destination"
    )
    body = f"""
    <html><body>
      <a href="{opaque_url}">
        <span>AIML Researcher - Foundation Model, Post-Training</span>
        <span>Apple</span>
        <span>4.1</span>
        <span>- Zürich, ZH</span>
        <span>Explore novel training strategies and model steering.</span>
        <span>2 days ago</span>
      </a>
    </body></html>
    """

    jobs = parse_emails([{
        "from": "Indeed Job Alerts <campaign@engage.indeed.com>",
        "date": "Sat, 25 Jul 2026",
        "body": body,
    }])

    assert len(jobs) == 1
    assert jobs[0]["company"] == "Apple"
    assert jobs[0]["location"] == "Zürich, ZH"


def test_indeed_card_cleans_location_separator_without_rating():
    opaque_url = (
        "https://engage.indeed.com/f/a/"
        "recipient-specific-token~~/campaign-token~/opaque-destination"
    )
    body = f"""
    <html><body>
      <a href="{opaque_url}">
        <span>AI Engineer</span>
        <span>Zuger Kantonalbank</span>
        <span>- Baar, ZG</span>
        <span>Build production AI systems.</span>
        <span>Just posted</span>
      </a>
    </body></html>
    """

    jobs = parse_emails([{
        "from": "Indeed Job Alerts <campaign@engage.indeed.com>",
        "date": "Sat, 25 Jul 2026",
        "body": body,
    }])

    assert len(jobs) == 1
    assert jobs[0]["company"] == "Zuger Kantonalbank"
    assert jobs[0]["location"] == "Baar, ZG"


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
