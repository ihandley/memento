from __future__ import annotations

import hashlib
import json
import random
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from . import retrieval
from .cursor_client import run_cursor_json
from .hook import _estimate_tokens, _format_record
from .jsonio import atomic_write_json
from .retrieval import RetrievalConfig, RetrievalScope, search
from .sanitize import redact_secrets

Judge = Callable[[str, list[dict]], dict[str, dict]]

SYSTEM_PREFIXES = ("<", "[SYSTEM NOTIFICATION", "Caveat:", "[Request interrupted")
DISTANCE_BUCKETS = ((0.0, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 0.85), (0.85, 99.0))
LENGTH_BUCKETS = (("1-3 words", 1, 3), ("4-10 words", 4, 10), ("11+ words", 11, 10**6))

JUDGE_INSTRUCTIONS = """You are grading a retrieval system. A coding assistant receives ONLY the user prompt below, plus the retrieved records. The prompt is standalone: no conversation history is available to it.

For each record, rate how much it helps the assistant answer or act on the prompt:
2 = directly useful: contains facts or a resolution the assistant needs
1 = related to the same topic but not needed to answer
0 = unrelated, or only shares a project or keyword name

Judge strictly. Sharing a project name alone is 0. Return only JSON:
{"ratings": [{"id": "<record id>", "score": 0, "reason": "<8 words>"}]}
"""


def sample_prompts(projects_root: Path, *, n: int, seed: int, min_chars: int = 1) -> list[dict]:
    """Human-typed prompts from Claude session transcripts, secrets redacted.
    The hook never stores prompt text, so past transcripts are the only
    source of what the hook really saw."""

    seen: set[str] = set()
    pool: list[dict] = []
    for path in sorted(projects_root.glob("*/*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
                continue
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, str):
                continue
            text = redact_secrets(content.strip())
            if len(text) < min_chars or text.startswith(SYSTEM_PREFIXES):
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            pool.append(
                {
                    "id": digest[:16],
                    "session_id": entry.get("sessionId", ""),
                    "timestamp": entry.get("timestamp", ""),
                    "text": text,
                }
            )
    random.Random(seed).shuffle(pool)
    return sorted(pool[:n], key=lambda prompt: prompt["id"])


def _eligible(candidate: dict, prompt: dict) -> bool:
    """Simulate retrieval at prompt time: a record extracted from the prompt's
    own session, or about a later date, couldn't have existed yet."""

    if candidate.get("source_session_id") == prompt["session_id"]:
        return False
    record_day = (candidate.get("timestamp") or "")[:10]
    prompt_day = prompt["timestamp"][:10]
    return not (record_day and prompt_day and record_day > prompt_day)


def replay(
    prompts: list[dict],
    database: Path,
    artifacts_root: Path,
    embedder,
    *,
    config: RetrievalConfig | None = None,
    max_results: int = 3,
    scope: RetrievalScope | None = None,
) -> list[dict]:
    """Run each prompt through the live search path. Over-fetches so the
    time/session exclusions still leave a full top-k, and keeps search()
    from appending these replays to the real retrieval trace log."""

    config = replace(config or RetrievalConfig.from_env(), max_results=max(max_results * 5, 15))
    scope = scope or RetrievalScope(global_scope=True)
    outcomes = []
    with mock.patch.object(retrieval, "append_json_line", lambda *args, **kwargs: None):
        for prompt in prompts:
            results, trace = search(database, artifacts_root, prompt["text"], embedder, config, scope)
            returned = [result for result in results if _eligible(result, prompt)][:max_results]
            by_id = {entry["id"]: entry for entry in trace["candidates"]}
            outcomes.append(
                {
                    "prompt": prompt,
                    "returned": [
                        {
                            "id": result["id"],
                            "vector_distance": by_id[result["id"]].get("vector_distance"),
                            "lexical_score": by_id[result["id"]].get("lexical_score"),
                            "qualification_reason": by_id[result["id"]].get("qualification_reason"),
                            "tokens": _estimate_tokens(_format_record(1, result)),
                            "text": result["text"],
                        }
                        for result in returned
                    ],
                }
            )
    return outcomes


def cursor_judge(*, model: str, mode: str = "ask", timeout: int = 180) -> Judge:
    def judge(prompt_text: str, records: list[dict]) -> dict[str, dict]:
        payload = {
            "prompt": prompt_text,
            "records": [{"id": record["id"], "text": record["text"]} for record in records],
        }
        value = run_cursor_json(
            JUDGE_INSTRUCTIONS + "\n" + json.dumps(payload),
            executable="cursor-agent", runner=subprocess.run, workspace=Path(tempfile.gettempdir()),
            mode=mode, model=model, timeout=timeout,
        )
        return {
            str(item["id"]): {"score": int(item["score"]), "reason": str(item.get("reason", ""))}
            for item in value.get("ratings", [])
            if item.get("score") in (0, 1, 2)
        }

    return judge


def _cache_key(prompt: dict, record_id: str) -> str:
    return f"{prompt['id']}|{record_id}"


def judge_outcomes(outcomes: list[dict], judge: Judge, cache_path: Path, *, workers: int = 4) -> None:
    """Attach a `judgment` to each returned record. Cached by (prompt, record)
    so a rerun after changing the gate only pays for pairs it hasn't seen."""

    cache: dict[str, dict] = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    def todo(outcome: dict) -> list[dict]:
        return [r for r in outcome["returned"] if _cache_key(outcome["prompt"], r["id"]) not in cache]

    def run(outcome: dict) -> tuple[dict, dict[str, dict]]:
        pending = todo(outcome)
        return outcome, judge(outcome["prompt"]["text"], pending) if pending else {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for outcome, ratings in pool.map(run, [o for o in outcomes if todo(o)]):
            for record_id, rating in ratings.items():
                cache[_cache_key(outcome["prompt"], record_id)] = rating
    atomic_write_json(cache_path, cache)
    for outcome in outcomes:
        for record in outcome["returned"]:
            record["judgment"] = cache.get(_cache_key(outcome["prompt"], record["id"]))


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _precision(records: list[dict]) -> dict:
    judged = [r for r in records if r.get("judgment")]
    return {
        "judged": len(judged),
        "related_or_better": _rate(sum(1 for r in judged if r["judgment"]["score"] >= 1), len(judged)),
        "directly_useful": _rate(sum(1 for r in judged if r["judgment"]["score"] == 2), len(judged)),
    }


def summarize(outcomes: list[dict]) -> dict:
    total = len(outcomes)
    injected = [o for o in outcomes if o["returned"]]
    judged_prompts = [o for o in injected if all(r.get("judgment") for r in o["returned"])]

    def best(outcome: dict) -> int:
        return max((r["judgment"]["score"] for r in outcome["returned"]), default=0)

    useful = [o for o in judged_prompts if best(o) == 2]
    noise = [o for o in judged_prompts if best(o) == 0]
    all_returned = [r for o in outcomes for r in o["returned"]]
    tokens_total = sum(r["tokens"] for r in all_returned)
    tokens_noise = sum(r["tokens"] for r in all_returned if r.get("judgment") and r["judgment"]["score"] == 0)

    by_distance = {}
    for low, high in DISTANCE_BUCKETS:
        bucket = [r for r in all_returned if r["vector_distance"] is not None and low <= r["vector_distance"] < high]
        by_distance[f"{low}-{high if high < 99 else 'inf'}"] = {"returned": len(bucket), **_precision(bucket)}

    by_reason: dict[str, list[dict]] = defaultdict(list)
    for record in all_returned:
        by_reason[record["qualification_reason"] or "unknown"].append(record)

    by_length = {}
    for label, low, high in LENGTH_BUCKETS:
        group = [o for o in outcomes if low <= len(o["prompt"]["text"].split()) <= high]
        records = [r for o in group for r in o["returned"]]
        by_length[label] = {
            "prompts": len(group),
            "injected": _rate(sum(1 for o in group if o["returned"]), len(group)),
            **_precision(records),
        }

    return {
        "prompts": total,
        "injected_rate": _rate(len(injected), total),
        "judged_prompts": len(judged_prompts),
        "useful_rate": _rate(len(useful), total),
        "noise_injection_rate": _rate(len(noise), len(injected)),
        "records": _precision(all_returned),
        "tokens_injected_total": tokens_total,
        "tokens_injected_per_prompt": round(tokens_total / total, 1) if total else 0,
        "tokens_wasted_share": _rate(tokens_noise, tokens_total),
        "by_vector_distance": by_distance,
        "by_qualification_reason": {k: {"returned": len(v), **_precision(v)} for k, v in sorted(by_reason.items())},
        "by_prompt_length": by_length,
    }


def render_summary(summary: dict) -> str:
    def pct(value) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%"

    records = summary["records"]
    lines = [
        f"Retrieval eval: {summary['prompts']} real prompts, global scope, same-session and future records excluded",
        "",
        f"Injected anything:        {pct(summary['injected_rate'])} of prompts",
        f"Injected something useful: {pct(summary['useful_rate'])} of prompts  (>=1 record rated 'directly useful')",
        f"Pure-noise injections:    {pct(summary['noise_injection_rate'])} of injections  (every record rated unrelated)",
        f"Record precision:         {pct(records['related_or_better'])} related or better, {pct(records['directly_useful'])} directly useful ({records['judged']} judged)",
        f"Tokens injected:          {summary['tokens_injected_per_prompt']}/prompt, {pct(summary['tokens_wasted_share'])} on unrelated records",
        "",
        "Precision by best-record vector distance:",
    ]
    for label, group in summary["by_vector_distance"].items():
        lines.append(f"  {label:<10} n={group['returned']:<4} related={pct(group['related_or_better'])} useful={pct(group['directly_useful'])}")
    lines.append("Precision by qualification reason:")
    for label, group in summary["by_qualification_reason"].items():
        lines.append(f"  {label:<14} n={group['returned']:<4} related={pct(group['related_or_better'])} useful={pct(group['directly_useful'])}")
    lines.append("By prompt length:")
    for label, group in summary["by_prompt_length"].items():
        lines.append(f"  {label:<10} prompts={group['prompts']:<4} injected={pct(group['injected'])} related={pct(group['related_or_better'])} useful={pct(group['directly_useful'])}")
    return "\n".join(lines)


def spot_check_markdown(outcomes: list[dict], *, count: int, seed: int) -> str:
    pairs = [(o["prompt"], r) for o in outcomes for r in o["returned"] if r.get("judgment")]
    random.Random(seed).shuffle(pairs)
    lines = ["# Spot check: does your rating match the judge's?", ""]
    for index, (prompt, record) in enumerate(pairs[:count], start=1):
        lines += [
            f"## {index}. judge: {record['judgment']['score']} ({record['judgment']['reason']})",
            f"**Prompt:** {prompt['text'][:400]}",
            f"**Record:** {record['text'][:600]}",
            f"**Your score (0/1/2):** ",
            "",
        ]
    return "\n".join(lines)
