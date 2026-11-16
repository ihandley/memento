import json
from datetime import datetime, timedelta, timezone

import pytest

import memento.hook as hook
from memento.artifacts import set_active_hash, write_artifact
from memento.jsonio import append_json_line
from memento.report import INJECTIONS_LOG_NAME, digest, render_digest

from conftest import make_record

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def _activate(tmp_path, *, session="old-session", question="Token refresh", timestamp="2026-10-03T12:00:00Z"):
    record = make_record(question=question, source_session_id=session, timestamp=timestamp)
    write_artifact(
        tmp_path, source_type="claude_session", source_id=session, source_uri="file:///s.jsonl",
        hash_value="sha256:abc", extractor="t", extractor_model="t", prompt_version=1, episode_records=[record],
    )
    set_active_hash(tmp_path, source_type="claude_session", source_id=session, hash_value="sha256:abc")
    from memento.artifacts import load_active_episode_records

    return [r for r in load_active_episode_records(tmp_path) if r["source_session_id"] == session][-1]


def _inject(tmp_path, record_ids, *, session="current", days_ago=1):
    append_json_line(
        tmp_path / INJECTIONS_LOG_NAME,
        {"logged_at": (NOW - timedelta(days=days_ago)).isoformat(), "session_id": session, "record_ids": record_ids},
    )


def test_digest_lists_title_date_source_session_and_count(tmp_path):
    record = _activate(tmp_path)
    _inject(tmp_path, [record["id"]], session="s1")
    _inject(tmp_path, [record["id"]], session="s2", days_ago=2)
    entries = digest(tmp_path, now=NOW)
    assert entries == [
        {"record_id": record["id"], "title": "Token refresh", "original_date": "2026-10-03",
         "source_session_id": "old-session", "injections": 2}
    ]
    rendered = render_digest(entries)
    assert "Token refresh" in rendered and "2026-10-03" in rendered and "old-session" in rendered and "2" in rendered


def test_digest_excludes_same_session_injections(tmp_path):
    record = _activate(tmp_path, session="current")
    _inject(tmp_path, [record["id"]], session="current")
    assert digest(tmp_path, now=NOW) == []


def test_digest_excludes_injections_older_than_seven_days(tmp_path):
    record = _activate(tmp_path)
    _inject(tmp_path, [record["id"]], days_ago=8)
    assert digest(tmp_path, now=NOW) == []


def test_digest_empty_is_clean(tmp_path):
    assert digest(tmp_path, now=NOW) == []
    assert "No" in render_digest([])


def test_digest_skips_records_that_no_longer_exist(tmp_path):
    _inject(tmp_path, ["missing"])
    assert digest(tmp_path, now=NOW) == []


def _hook_record(identifier):
    return dict(id=identifier, text="short", question=identifier, source="src", source_type="claude_session",
                source_id="sid", source_hash="h", timestamp="2026-10-03T00:00:00Z", source_session_id="old")


def test_hook_logs_injected_ids_and_session_but_never_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "search", lambda *a: ([_hook_record("r1"), _hook_record("r2")], {}))
    hook.handle_user_prompt(
        {"hook_event_name": "UserPromptSubmit", "prompt": "private prompt sentinel", "session_id": "cur"},
        tmp_path / "db", tmp_path, embedder=object(),
    )
    entry = json.loads((tmp_path / INJECTIONS_LOG_NAME).read_text())
    assert entry["session_id"] == "cur" and entry["record_ids"] == ["r1", "r2"]
    for written in tmp_path.glob("*.jsonl"):
        assert "private prompt sentinel" not in written.read_text()


@pytest.mark.parametrize("event_extra, results", [({"session_id": "cur"}, []), ({}, [_hook_record("r1")])])
def test_hook_logs_nothing_without_injection_or_session(tmp_path, monkeypatch, event_extra, results):
    monkeypatch.setattr(hook, "search", lambda *a: (results, {}))
    hook.handle_user_prompt(
        {"hook_event_name": "UserPromptSubmit", "prompt": "p", **event_extra}, tmp_path / "db", tmp_path, embedder=object(),
    )
    assert not (tmp_path / INJECTIONS_LOG_NAME).exists()


def test_hook_survives_injection_log_write_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "search", lambda *a: ([_hook_record("r1")], {}))
    monkeypatch.setattr(hook, "append_json_line", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    response = hook.handle_user_prompt(
        {"hook_event_name": "UserPromptSubmit", "prompt": "p", "session_id": "cur"}, tmp_path / "db", tmp_path,
        embedder=object(),
    )
    assert "additionalContext" in response["hookSpecificOutput"]
