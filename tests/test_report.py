from datetime import datetime, timedelta, timezone

from memento.artifacts import set_active_hash, write_artifact
from memento.jsonio import append_json_line, atomic_write_json
from memento.overlay import overlay_path
from memento.report import (
    generate_report,
    never_retrieved,
    retrieval_usage,
    sync_yield,
    top_topics,
    verification_ratio,
)

from conftest import make_record

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def _activate_record(tmp_path, *, source_id="session-1", systems=()):
    record = make_record(systems=list(systems))
    write_artifact(
        tmp_path,
        source_type="claude_session",
        source_id=source_id,
        source_uri="file:///session.jsonl",
        hash_value="sha256:abc",
        extractor="test",
        extractor_model="test",
        prompt_version=1,
        episode_records=[record],
    )
    set_active_hash(tmp_path, source_type="claude_session", source_id=source_id, hash_value="sha256:abc")
    from memento.artifacts import load_active_episode_records

    return load_active_episode_records(tmp_path)[-1]


def test_retrieval_usage_computes_hit_rate_and_windows(tmp_path):
    from memento.hook import METRICS_LOG_NAME

    metrics_path = tmp_path / METRICS_LOG_NAME
    append_json_line(metrics_path, {"outcome": "ok", "result_count": 2, "logged_at": (NOW - timedelta(days=1)).isoformat()})
    append_json_line(metrics_path, {"outcome": "ok", "result_count": 0, "logged_at": (NOW - timedelta(days=1)).isoformat()})
    append_json_line(metrics_path, {"outcome": "timeout", "result_count": 0, "logged_at": (NOW - timedelta(days=40)).isoformat()})
    append_json_line(metrics_path, {"outcome": "skipped_empty", "result_count": 0, "logged_at": (NOW - timedelta(days=1)).isoformat()})

    usage = retrieval_usage(tmp_path, now=NOW)

    assert usage["lifetime"]["fired"] == 3
    assert usage["lifetime"]["hits"] == 1
    assert usage["lifetime"]["timeouts"] == 1
    assert usage["30d"]["fired"] == 2
    assert usage["30d"]["hits"] == 1
    assert usage["30d"]["timeouts"] == 0


def test_retrieval_usage_empty_window_has_no_hit_rate(tmp_path):
    usage = retrieval_usage(tmp_path, now=NOW)
    assert usage["lifetime"] == {"fired": 0, "hits": 0, "hit_rate": None, "timeouts": 0, "errors": 0}


def test_top_topics_joins_returned_ids_to_record_systems(tmp_path):
    from memento.retrieval import TRACE_LOG_NAME

    record = _activate_record(tmp_path, systems=["lvcore"])
    append_json_line(
        tmp_path / TRACE_LOG_NAME,
        {"project_id": "proj-a", "returned_ids": [record["id"]], "logged_at": (NOW - timedelta(days=1)).isoformat()},
    )
    append_json_line(
        tmp_path / TRACE_LOG_NAME,
        {"project_id": "proj-a", "returned_ids": [], "logged_at": (NOW - timedelta(days=1)).isoformat()},
    )

    topics = top_topics(tmp_path, now=NOW)

    assert topics["30d"]["by_project"] == [("proj-a", 1)]
    assert topics["30d"]["by_topic"] == [("lvcore", 1)]


def test_sync_yield_aggregates_by_label_and_window(tmp_path):
    sync_log = tmp_path / "sync_history.jsonl"
    append_json_line(sync_log, {"label": "claude_sessions", "activated": 5, "blocked": 1, "failed": 0, "pending_retry": 0, "logged_at": (NOW - timedelta(days=1)).isoformat()})
    append_json_line(sync_log, {"label": "confluence:proj-a", "activated": 2, "blocked": 0, "failed": 0, "pending_retry": 0, "logged_at": (NOW - timedelta(days=40)).isoformat()})

    result = sync_yield(tmp_path, now=NOW)

    assert result["lifetime"]["activated"] == 7
    assert result["30d"]["activated"] == 5
    assert result["30d"]["activated_by_source"] == {"claude_sessions": 5}


def test_verification_ratio_counts_status_and_recent_reviews(tmp_path):
    _activate_record(tmp_path, source_id="s1")
    _activate_record(tmp_path, source_id="s2")
    overlay = {
        "sha256:abc:0": {"verification_status": "verified", "updated_at": (NOW - timedelta(days=1)).isoformat()},
    }
    atomic_write_json(overlay_path(tmp_path), overlay)

    ratio = verification_ratio(tmp_path, now=NOW)

    assert ratio["verified"] == 1
    assert ratio["unreviewed"] == 1
    assert ratio["reviewed_last_30d"] == 1
    assert ratio["reviewed_last_7d"] == 1


def test_never_retrieved_excludes_recent_and_cited_records(tmp_path):
    from memento.retrieval import TRACE_LOG_NAME

    old_record = _activate_record(tmp_path, source_id="old")
    # Artifacts stamp extracted_at from the wall clock, so the query clock has to
    # sit after that stamp. The September fixture date is now older than the
    # 14-day window relative to a record written today.
    query_now = datetime.now(timezone.utc) + timedelta(days=30)
    stale = never_retrieved(tmp_path, min_age_days=14, now=query_now)

    assert stale == [{
        "id": old_record["id"],
        "question": old_record["question"],
        "source": old_record["source"],
        "extracted_at": old_record["extracted_at"],
    }]

    append_json_line(tmp_path / TRACE_LOG_NAME, {"returned_ids": [old_record["id"]], "logged_at": NOW.isoformat()})
    assert never_retrieved(tmp_path, min_age_days=14, now=query_now) == []


def test_generate_report_has_every_section(tmp_path):
    report = generate_report(tmp_path, now=NOW)
    assert set(report) == {
        "generated_at", "retrieval_usage", "top_topics", "nightly_sync_yield", "verification", "never_retrieved",
    }
