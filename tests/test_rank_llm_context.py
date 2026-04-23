import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=object))

from rank_llm import _load_context


def test_load_context_prefers_resume_secret(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_RESUME_MD", "Private resume from secret")

    prefs, resume = _load_context()

    assert prefs["profile"]["name"] == "Pierpaolo Vendittelli"
    assert resume == "Private resume from secret"
