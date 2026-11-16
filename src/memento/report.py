from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .artifacts import find_record, load_active_episode_records
from .hook import INJECTIONS_LOG_NAME, METRICS_LOG_NAME
from .jsonio import read_json
from .overlay import overlay_path
from .retrieval import TRACE_LOG_NAME

SYNC_LOG_NAME = "sync_history.jsonl"

WINDOWS: dict[str, timedelta | None] = {"lifetime": None, "30d": timedelta(days=30), "7d": timedelta(days=7)}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _since(entries: list[dict], timestamp_field: str, cutoff: datetime | None) -> list[dict]:
    if cutoff is None:
        return entries
    kept = []
    for entry in entries:
        parsed = _parse_timestamp(entry.get(timestamp_field))
        if parsed is not None and parsed >= cutoff:
            kept.append(entry)
    return kept


def retrieval_usage(artifacts_root: Path, *, now: datetime | None = None) -> dict:
    """Hook fire count, hit rate, and failure breakdown from hook_metrics.jsonl,
    never the query text itself (matching the hook's no-prompt-logging posture)."""

    now = now or datetime.now(timezone.utc)
    metrics = _read_jsonl(artifacts_root / METRICS_LOG_NAME)
    windows = {}
    for label, delta in WINDOWS.items():
        cutoff = now - delta if delta else None
        subset = _since(metrics, "logged_at", cutoff)
        fired = [m for m in subset if m.get("outcome") != "skipped_empty"]
        hits = [m for m in fired if m.get("outcome") == "ok" and m.get("result_count", 0) > 0]
        windows[label] = {
            "fired": len(fired),
            "hits": len(hits),
            "hit_rate": round(len(hits) / len(fired), 3) if fired else None,
            "timeouts": sum(1 for m in fired if m.get("outcome") == "timeout"),
            "errors": sum(1 for m in fired if m.get("outcome") == "error"),
        }
    return windows


def _record_topic(artifacts_root: Path, record_id: str, cache: dict[str, str]) -> str:
    # First `systems` entry only, since markdown/RAW knowledge-base imports
    # fold their knowledge_base_id in there (see markdown_kb.py) and it's
    # the closest thing to a topic tag any record carries.
    if record_id not in cache:
        record = find_record(artifacts_root, record_id)
        systems = (record or {}).get("systems") or []
        cache[record_id] = systems[0] if systems else "untagged"
    return cache[record_id]


def top_topics(artifacts_root: Path, *, limit: int = 10, now: datetime | None = None) -> dict:
    """What retrieval hits actually surface, from retrieval_traces.jsonl,
    never the query text (see retrieval.search's persisted_trace)."""

    now = now or datetime.now(timezone.utc)
    traces = _read_jsonl(artifacts_root / TRACE_LOG_NAME)
    topic_cache: dict[str, str] = {}
    result = {}
    for label, delta in WINDOWS.items():
        cutoff = now - delta if delta else None
        subset = _since(traces, "logged_at", cutoff)
        by_project: Counter[str] = Counter()
        by_topic: Counter[str] = Counter()
        for trace in subset:
            returned_ids = trace.get("returned_ids") or []
            if not returned_ids:
                continue
            if trace.get("project_id"):
                by_project[trace["project_id"]] += 1
            for record_id in returned_ids:
                by_topic[_record_topic(artifacts_root, record_id, topic_cache)] += 1
        result[label] = {"by_project": by_project.most_common(limit), "by_topic": by_topic.most_common(limit)}
    return result


def sync_yield(artifacts_root: Path, *, now: datetime | None = None) -> dict:
    """New sources/records nightly-sync.sh picked up, from sync_history.jsonl
    (one entry per run_step call; see nightly-sync.sh)."""

    now = now or datetime.now(timezone.utc)
    runs = _read_jsonl(artifacts_root / SYNC_LOG_NAME)
    result = {}
    for label, delta in WINDOWS.items():
        cutoff = now - delta if delta else None
        subset = _since(runs, "logged_at", cutoff)
        totals: Counter[str] = Counter()
        activated_by_label: Counter[str] = Counter()
        for run in subset:
            activated_by_label[run.get("label", "unknown")] += run.get("activated", 0)
            for field in ("activated", "blocked", "failed", "pending_retry"):
                totals[field] += run.get(field, 0)
        result[label] = {"runs": len(subset), **totals, "activated_by_source": dict(activated_by_label)}
    return result


def verification_ratio(artifacts_root: Path, *, now: datetime | None = None) -> dict:
    """Verified/rejected ratio across the corpus, from overlay.json. verify
    and reject are terminal transitions from unreviewed (see
    overlay._ALLOWED_TRANSITIONS), so a record's updated_at approximates when
    it became verified or rejected."""

    now = now or datetime.now(timezone.utc)
    path = overlay_path(artifacts_root)
    overlay = read_json(path) if path.exists() else {}
    by_status = Counter(state.get("verification_status", "unreviewed") for state in overlay.values())
    record_count = len(load_active_episode_records(artifacts_root))
    unreviewed = max(record_count - sum(by_status.values()), 0) + by_status.get("unreviewed", 0)
    reviewed_recent = {}
    for label, delta in WINDOWS.items():
        if delta is None:
            continue
        cutoff = now - delta
        reviewed_recent[label] = sum(
            1
            for state in overlay.values()
            if state.get("verification_status") in {"verified", "rejected"}
            and (parsed := _parse_timestamp(state.get("updated_at"))) is not None
            and parsed >= cutoff
        )
    return {
        "verified": by_status.get("verified", 0),
        "rejected": by_status.get("rejected", 0),
        "superseded": by_status.get("superseded", 0),
        "unreviewed": unreviewed,
        "reviewed_last_30d": reviewed_recent.get("30d", 0),
        "reviewed_last_7d": reviewed_recent.get("7d", 0),
    }


def never_retrieved(artifacts_root: Path, *, min_age_days: int = 14, now: datetime | None = None) -> list[dict]:
    """Active records old enough to have had a chance to be useful, but that
    never appeared in any retrieval_traces.jsonl `returned_ids` list. Dead
    weight: the nightly sync keeps scooping content nothing ever cites."""

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=min_age_days)
    ever_returned: set[str] = set()
    for trace in _read_jsonl(artifacts_root / TRACE_LOG_NAME):
        ever_returned.update(trace.get("returned_ids") or [])
    stale = []
    for record in load_active_episode_records(artifacts_root):
        extracted_at = _parse_timestamp(record.get("extracted_at"))
        if extracted_at is None or extracted_at >= cutoff:
            continue
        if record["id"] in ever_returned:
            continue
        stale.append({
            "id": record["id"],
            "question": record["question"],
            "source": record["source"],
            "extracted_at": record.get("extracted_at"),
        })
    return stale


def digest(artifacts_root: Path, *, days: int = 7, now: datetime | None = None) -> list[dict]:
    """Records injected in the last `days` days that came from a different
    session than the one they were injected into ("you already solved this").
    Built from hook_injections.jsonl (ids and session ids only, never prompt
    text) joined to the records themselves."""

    now = now or datetime.now(timezone.utc)
    injections = _since(_read_jsonl(artifacts_root / INJECTIONS_LOG_NAME), "logged_at", now - timedelta(days=days))
    per_record: Counter[str] = Counter()
    records: dict[str, dict | None] = {}
    for injection in injections:
        injected_at = _parse_timestamp(injection.get("logged_at"))
        for record_id in set(injection.get("record_ids") or []):
            if record_id not in records:
                records[record_id] = find_record(artifacts_root, record_id)
            record = records[record_id]
            if record is None or record.get("source_session_id") == injection.get("session_id"):
                continue
            # Different AND earlier: a record dated after its injection can't
            # be something already solved.
            recorded_at = _parse_timestamp(record.get("timestamp"))
            if recorded_at is not None and injected_at is not None and recorded_at > injected_at:
                continue
            per_record[record_id] += 1
    entries = [
        {
            "record_id": record_id,
            "title": " ".join(str(records[record_id].get("question") or "").split()) or "untitled",
            "original_date": (records[record_id].get("timestamp") or "")[:10] or "undated",
            "source_session_id": records[record_id].get("source_session_id") or "unknown",
            "injections": count,
        }
        for record_id, count in per_record.items()
    ]
    return sorted(entries, key=lambda entry: (-entry["injections"], entry["original_date"], entry["record_id"]))


def render_digest(entries: list[dict], *, days: int = 7) -> str:
    if not entries:
        return f"No earlier-session records were injected in the last {days} days."
    lines = [f"You already solved this: {len(entries)} record(s) injected in the last {days} days", ""]
    for entry in entries:
        lines.append(
            f"  {entry['original_date']}  {entry['title']}  "
            f"(session {entry['source_session_id']}, injected {entry['injections']}x)"
        )
    return "\n".join(lines)


def render_report(data: dict) -> str:
    lines = [f"Memento report — generated {data['generated_at']}", ""]

    lines.append("Retrieval usage")
    for label in ("lifetime", "30d", "7d"):
        usage = data["retrieval_usage"][label]
        lines.append(
            f"  {label:8} fired={usage['fired']:<5} hits={usage['hits']:<5} "
            f"hit_rate={usage['hit_rate']} timeouts={usage['timeouts']} errors={usage['errors']}"
        )
    lines.append("")

    lines.append("Top topics (30d)")
    topics_30d = data["top_topics"]["30d"]
    projects = ", ".join(f"{name} ({count})" for name, count in topics_30d["by_project"]) or "none"
    topics = ", ".join(f"{name} ({count})" for name, count in topics_30d["by_topic"]) or "none"
    lines.append(f"  by project: {projects}")
    lines.append(f"  by topic:   {topics}")
    lines.append("")

    lines.append("Nightly sync yield")
    for label in ("lifetime", "30d", "7d"):
        yield_data = data["nightly_sync_yield"][label]
        lines.append(
            f"  {label:8} runs={yield_data['runs']:<4} activated={yield_data['activated']:<5} "
            f"blocked={yield_data['blocked']} failed={yield_data['failed']} pending_retry={yield_data['pending_retry']}"
        )
    by_source = data["nightly_sync_yield"]["30d"]["activated_by_source"]
    if by_source:
        lines.append("  activated by source (30d): " + ", ".join(f"{name} ({count})" for name, count in by_source.items()))
    lines.append("")

    verification = data["verification"]
    lines.append("Verification")
    lines.append(
        f"  verified={verification['verified']} rejected={verification['rejected']} "
        f"superseded={verification['superseded']} unreviewed={verification['unreviewed']}"
    )
    lines.append(f"  reviewed last 30d={verification['reviewed_last_30d']} last 7d={verification['reviewed_last_7d']}")
    lines.append("")

    stale = data["never_retrieved"]
    lines.append(f"Never retrieved ({len(stale)} record(s))")
    for item in stale[:20]:
        lines.append(f"  - {item['id']}: {item['question']!r} (extracted {item['extracted_at']})")
    if len(stale) > 20:
        lines.append(f"  ... and {len(stale) - 20} more")

    return "\n".join(lines)


def generate_report(artifacts_root: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "retrieval_usage": retrieval_usage(artifacts_root, now=now),
        "top_topics": top_topics(artifacts_root, now=now),
        "nightly_sync_yield": sync_yield(artifacts_root, now=now),
        "verification": verification_ratio(artifacts_root, now=now),
        "never_retrieved": never_retrieved(artifacts_root, now=now),
    }
