import json

from memento.evaluation import _eligible, judge_outcomes, sample_prompts, summarize


def _write_session(root, name, entries):
    project = root / "-proj"
    project.mkdir(exist_ok=True)
    (project / f"{name}.jsonl").write_text("\n".join(json.dumps(entry) for entry in entries))


def _user(text, **extra):
    return {"type": "user", "message": {"content": text}, "sessionId": "s1", "timestamp": "2026-09-01T10:00:00Z", **extra}


def test_sample_prompts_keeps_only_human_typed_prompts_and_redacts(tmp_path):
    _write_session(
        tmp_path,
        "s1",
        [
            _user("why does the hook time out?"),
            _user("why does the hook time out?"),
            _user("<system-reminder>injected</system-reminder>"),
            _user("[SYSTEM NOTIFICATION - NOT USER INPUT] task done"),
            _user("sidechain", isSidechain=True),
            _user("meta", isMeta=True),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}, "sessionId": "s1"},
            {"type": "assistant", "message": {"content": "hi"}},
            _user("key is AKIAIOSFODNN7EXAMPLE ok"),
        ],
    )
    prompts = sample_prompts(tmp_path, n=10, seed=1)
    texts = {prompt["text"] for prompt in prompts}
    assert "why does the hook time out?" in texts
    assert len(prompts) == 2
    assert not any("AKIAIOSFODNN7EXAMPLE" in text for text in texts)


def test_sample_prompts_is_deterministic_for_a_seed(tmp_path):
    _write_session(tmp_path, "s1", [_user(f"question number {index}") for index in range(30)])
    assert sample_prompts(tmp_path, n=5, seed=7) == sample_prompts(tmp_path, n=5, seed=7)


def test_eligible_excludes_same_session_and_future_records():
    prompt = {"session_id": "s1", "timestamp": "2026-09-10T10:00:00Z"}
    assert not _eligible({"source_session_id": "s1", "timestamp": "2026-09-01T00:00:00Z"}, prompt)
    assert not _eligible({"source_session_id": "s2", "timestamp": "2026-09-11T00:00:00Z"}, prompt)
    assert _eligible({"source_session_id": "s2", "timestamp": "2026-09-10T00:00:00Z"}, prompt)
    assert _eligible({"source_session_id": "s2", "timestamp": ""}, prompt)


def _outcome(prompt_id, records):
    return {"prompt": {"id": prompt_id, "text": "t " + prompt_id}, "returned": records}


def _returned(record_id, distance=0.6, reason="vector", tokens=100):
    return {"id": record_id, "vector_distance": distance, "qualification_reason": reason, "tokens": tokens, "text": "x"}


def test_judge_outcomes_only_judges_unseen_pairs(tmp_path):
    calls = []

    def judge(prompt_text, records):
        calls.append([record["id"] for record in records])
        return {record["id"]: {"score": 2, "reason": "r"} for record in records}

    cache = tmp_path / "judgments.json"
    first = [_outcome("p1", [_returned("a")])]
    judge_outcomes(first, judge, cache, workers=1)
    second = [_outcome("p1", [_returned("a"), _returned("b")])]
    judge_outcomes(second, judge, cache, workers=1)

    assert calls == [["a"], ["b"]]
    assert [record["judgment"]["score"] for record in second[0]["returned"]] == [2, 2]


def test_summarize_separates_useful_noise_and_wasted_tokens():
    useful = _outcome("p1", [_returned("a"), _returned("b")])
    noise = _outcome("p2", [_returned("c", distance=0.9, reason="exact_phrase")])
    empty = _outcome("p3", [])
    for record, score in zip(useful["returned"] + noise["returned"], (2, 0, 0)):
        record["judgment"] = {"score": score, "reason": ""}

    summary = summarize([useful, noise, empty])

    assert summary["injected_rate"] == 0.667
    assert summary["useful_rate"] == 0.333
    assert summary["noise_injection_rate"] == 0.5
    assert summary["tokens_wasted_share"] == 0.667
    assert summary["by_qualification_reason"]["exact_phrase"]["directly_useful"] == 0.0
