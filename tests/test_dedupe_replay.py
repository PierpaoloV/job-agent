from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dedupe


def glassdoor_job():
    return {
        "dedup_key": "glassdoor:1010206875020",
        "url": "https://glassdoor.example/1010206875020",
        "title": "Machine Learning Scientist",
        "company": "Align Technology",
        "source": "Glassdoor",
    }


def test_named_source_replay_is_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setattr(dedupe, "DB_PATH", tmp_path / "seen.sqlite")
    job = glassdoor_job()
    dedupe.mark_seen([job])
    assert dedupe.filter_new([job]) == []

    replayed = dedupe.filter_new(
        [job], replay_sources=("glassdoor",), replay_id="glassdoor-parser-v2"
    )
    assert replayed == [job]
    dedupe.mark_seen(
        replayed,
        replay_sources=("glassdoor",),
        replay_id="glassdoor-parser-v2",
    )

    assert dedupe.filter_new(
        [job], replay_sources=("glassdoor",), replay_id="glassdoor-parser-v2"
    ) == []
    assert dedupe.filter_new(
        [job], replay_sources=("glassdoor",), replay_id="glassdoor-parser-v3"
    ) == [job]


def test_replay_sources_require_a_named_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(dedupe, "DB_PATH", tmp_path / "seen.sqlite")

    try:
        dedupe.filter_new([glassdoor_job()], replay_sources=("glassdoor",))
    except ValueError as error:
        assert "replay_id" in str(error)
    else:
        raise AssertionError("unnamed replay was accepted")
