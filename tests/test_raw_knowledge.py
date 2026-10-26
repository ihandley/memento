import json
import subprocess
from pathlib import Path

import pytest
from memento.extractors.base import ExtractionBlocked, ExtractionError, ExtractionPendingRetry
from memento.extractors.raw_knowledge import RawKnowledgeExtractor


@pytest.fixture(autouse=True)
def operator_id_env(monkeypatch):
    monkeypatch.setenv("MEMENTO_OPERATOR_ID", "test-operator")
    for var in ("MEMENTO_PROJECT_ID", "MEMENTO_PROJECT_ROOT"):
        monkeypatch.delenv(var, raising=False)


def cursor_response(payload: dict) -> subprocess.CompletedProcess[str]:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(payload),
        "session_id": "cursor-session",
    }
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(envelope), stderr="")


def write_document(path: Path, text: str) -> None:
    path.write_text(text)


def test_raw_knowledge_extractor_passes_timeout_to_cursor_call(tmp_path):
    document = tmp_path / "doc.md"
    write_document(document, "# Doc\n\nBody.\n")
    observed: list[int] = []

    def runner(command, **kwargs):
        observed.append(kwargs["timeout"])
        return cursor_response({"records": []})

    RawKnowledgeExtractor(runner=runner).extract(document)
    RawKnowledgeExtractor(runner=runner, timeout_seconds=300).extract(document)

    assert observed == [120, 300]


def test_raw_knowledge_extractor_returns_validated_structured_records(tmp_path):
    document = tmp_path / "device-identity-decision.md"
    write_document(
        document,
        "The team decided to leave device identity as-is.\n\n"
        "Bridging Horus IDs into UCS config was declined as papering over the real problem.",
    )
    observed: dict = {}

    def runner(command, **kwargs):
        observed["command"] = command
        observed["input"] = kwargs["input"]
        return cursor_response(
            {
                "records": [
                    {
                        "question": "Why was device identity left as-is?",
                        "summary": "Bridging was declined as papering over a transitional problem.",
                        "resolution": "Replace the legacy APIs instead of bridging identity.",
                        "systems": ["LVCore"],
                        "code_references": [],
                        "temporal_scope": "durable",
                        "evidence_location": "para-2",
                    }
                ]
            }
        )

    records = RawKnowledgeExtractor(runner=runner).extract(document)

    assert len(records) == 1
    assert records[0].resolution == "Replace the legacy APIs instead of bridging identity."
    assert records[0].source == str(document.resolve())
    assert records[0].source_session_id == document.stem
    assert records[0].source_type == "raw_knowledge_source"
    assert records[0].operator_id == "test-operator"
    assert records[0].prompt_version == 1
    assert records[0].evidence_location.identifier == "para-2"
    assert records[0].evidence_location.preserved_text.startswith("Bridging Horus IDs")
    assert "document_data" in observed["input"]
    assert "transcript_data" not in observed["input"]


def test_raw_knowledge_extractor_discards_hallucinated_evidence_location(tmp_path):
    document = tmp_path / "note.md"
    write_document(document, "Only one paragraph here.")

    def runner(command, **kwargs):
        return cursor_response(
            {
                "records": [
                    {
                        "question": "What does the note say?",
                        "summary": "Only one paragraph here.",
                        "evidence_location": "para-99",
                    }
                ]
            }
        )

    records = RawKnowledgeExtractor(runner=runner).extract(document)

    assert records[0].evidence_location is None


def test_raw_knowledge_extractor_blocks_oversized_documents(tmp_path):
    document = tmp_path / "huge.md"
    write_document(document, "x" * 100)

    def runner(command, **kwargs):
        raise AssertionError("Cursor must not be invoked for a blocked document")

    with pytest.raises(ExtractionBlocked):
        RawKnowledgeExtractor(runner=runner, max_sanitized_chars=10).extract(document)


def test_raw_knowledge_extractor_reports_pending_retry_on_cursor_timeout(tmp_path):
    document = tmp_path / "note.md"
    write_document(document, "Some content.")

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=1)

    with pytest.raises(ExtractionPendingRetry):
        RawKnowledgeExtractor(runner=runner).extract(document)


def test_raw_knowledge_extractor_fails_after_bounded_retries_on_malformed_output(tmp_path):
    document = tmp_path / "note.md"
    write_document(document, "Some content.")
    attempts = []

    def runner(command, **kwargs):
        attempts.append(1)
        return subprocess.CompletedProcess([], 0, stdout="not json", stderr="")

    with pytest.raises(ExtractionError):
        RawKnowledgeExtractor(runner=runner, max_output_retries=1).extract(document)
    assert len(attempts) == 2


def test_raw_knowledge_extractor_requires_operator_id(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMENTO_OPERATOR_ID", raising=False)
    with pytest.raises(ValueError):
        RawKnowledgeExtractor(runner=lambda *a, **k: None)
