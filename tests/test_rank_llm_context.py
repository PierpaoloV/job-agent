import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=object))

from rank_llm import _load_context


def _preferences_file(tmp_path):
    path = tmp_path / "preferences.yaml"
    path.write_text(
        """
profile:
  name: Alex Example
portfolio:
  geography:
    primary: [Zurich, Basel]
    lower_priority: [USA (in-person)]
    default_location_order: [London, Dublin]
  compensation:
    hard_salary_floor: null
    unpublished_is_eligible: true
""",
        encoding="utf-8",
    )
    return path


def test_load_context_prefers_resume_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGENT_RESUME_MD", "Private resume from secret")
    monkeypatch.setenv(
        "JOB_AGENT_PREFERENCES_PATH", str(_preferences_file(tmp_path))
    )

    prefs, resume = _load_context()

    assert prefs["profile"]["name"] == "Alex Example"
    assert resume == "Private resume from secret"


def test_preferences_use_portfolio_without_salary_floor_or_us_exclusion(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JOB_AGENT_RESUME_MD", "Professional resume")
    monkeypatch.setenv(
        "JOB_AGENT_PREFERENCES_PATH", str(_preferences_file(tmp_path))
    )

    prefs, _ = _load_context()

    assert prefs["portfolio"]["compensation"]["hard_salary_floor"] is None
    assert prefs["portfolio"]["compensation"]["unpublished_is_eligible"] is True
    assert "USA (in-person)" in prefs["locations"]["acceptable"]
    assert prefs["locations"]["rejected"] == []
    assert prefs["portfolio"]["geography"]["default_location_order"][:2] == [
        "London",
        "Dublin",
    ]
