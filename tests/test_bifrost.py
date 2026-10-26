import json
from pathlib import Path

import pytest

from memento.extractors.base import EngineConfigurationError, ExtractionError, ExtractionPendingRetry
from memento.extractors.bifrost import BifrostExtractor
from memento.extractors.cursor import CursorExtractor
from memento.secrets import KeychainSecretError


@pytest.fixture(autouse=True)
def operator_id_env(monkeypatch):
    monkeypatch.setenv("SESSION_RAG_OPERATOR_ID", "test-operator")
    for var in (
        "SESSION_RAG_PROJECT_ID",
        "SESSION_RAG_PROJECT_ROOT",
        "SESSION_RAG_REPOSITORY_REVISION",
        "SESSION_RAG_WORKING_TREE_DIRTY",
    ):
        monkeypatch.delenv(var, raising=False)


def cursor_response(payload: dict):
    import subprocess

    envelope = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(payload), "session_id": "cursor-session"}
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(envelope), stderr="")


def write_transcript(path: Path) -> None:
    path.write_text('{"type":"user","message":{"content":"Why did RabbitMQ reconnect?"}}\n')


def _chat_body(payload: dict, *, fenced: bool = False) -> tuple[int, str]:
    content = json.dumps(payload)
    if fenced:
        content = f"```json\n{content}\n```"
    return 200, json.dumps({"choices": [{"message": {"content": content}}]})


def _record_payload() -> dict:
    return {
        "records": [
            {
                "question": "Why did RabbitMQ reconnect?",
                "summary": "The heartbeat expired.",
                "resolution": "Increase the heartbeat interval.",
            }
        ]
    }


def test_bifrost_extractor_sends_the_cursor_prompt_and_returns_records(tmp_path):
    transcript = tmp_path / "claude-session-123.jsonl"
    write_transcript(transcript)
    observed: dict = {}

    def transport(url, headers, body, timeout):
        observed["url"] = url
        observed["headers"] = headers
        observed["body"] = body
        observed["timeout"] = timeout
        return _chat_body(_record_payload())

    records = BifrostExtractor(
        base_url="https://bifrost.example/openai/",
        model="bedrock/anthropic.claude",
        api_key="secret-token",
        transport=transport,
        timeout_seconds=120,
    ).extract(transcript)

    cursor_input: dict = {}

    def runner(command, **kwargs):
        cursor_input["input"] = kwargs["input"]
        return cursor_response({"records": []})

    CursorExtractor(runner=runner).extract(transcript)

    assert records[0].question == "Why did RabbitMQ reconnect?"
    assert records[0].source_type == "claude_session"
    assert records[0].operator_id == "test-operator"
    assert observed["url"] == "https://bifrost.example/openai/v1/chat/completions"
    assert observed["headers"]["Authorization"] == "Bearer secret-token"
    assert observed["body"]["model"] == "bedrock/anthropic.claude"
    assert observed["body"]["temperature"] == 0
    assert "max_tokens" not in observed["body"]
    assert "max_completion_tokens" not in observed["body"]
    assert observed["body"]["messages"] == [{"role": "user", "content": cursor_input["input"]}]
    assert observed["timeout"] == 120
    assert "Why did RabbitMQ reconnect?" in observed["body"]["messages"][0]["content"]


def test_bifrost_extractor_accepts_json_wrapped_in_a_fence(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)

    def transport(url, headers, body, timeout):
        return _chat_body(_record_payload(), fenced=True)

    records = BifrostExtractor(
        base_url="https://bifrost.example/openai", model="bedrock/m", api_key="k", transport=transport
    ).extract(transcript)

    assert records[0].summary == "The heartbeat expired."


def test_bifrost_extractor_pending_retries_transport_failures_once(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)
    calls = {"n": 0}

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        raise TimeoutError("timed out")

    with pytest.raises(ExtractionPendingRetry, match="Bifrost unavailable"):
        BifrostExtractor(
            base_url="https://bifrost.example/openai", model="bedrock/m", api_key="k", transport=transport
        ).extract(transcript)

    assert calls["n"] == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_bifrost_extractor_pending_retries_rate_limits_and_server_errors(tmp_path, status):
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)

    def transport(url, headers, body, timeout):
        return status, "nope"

    with pytest.raises(ExtractionPendingRetry, match="Bifrost unavailable"):
        BifrostExtractor(
            base_url="https://bifrost.example/openai", model="bedrock/m", api_key="k", transport=transport
        ).extract(transcript)


@pytest.mark.parametrize("status", [401, 403])
def test_bifrost_extractor_stops_the_run_when_the_gateway_refuses_auth(tmp_path, status):
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)
    calls = {"n": 0}

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        return status, "denied"

    with pytest.raises(EngineConfigurationError):
        BifrostExtractor(
            base_url="https://bifrost.example/openai",
            model="bedrock/m",
            api_key="k",
            transport=transport,
            max_output_retries=2,
        ).extract(transcript)

    assert calls["n"] == 1


def test_bifrost_extractor_retries_invalid_json_then_returns_records(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)
    responses = [ (200, json.dumps({"choices": [{"message": {"content": "not-json"}}]})), _chat_body(_record_payload()) ]

    def transport(url, headers, body, timeout):
        return responses.pop(0)

    records = BifrostExtractor(
        base_url="https://bifrost.example/openai",
        model="bedrock/m",
        api_key="k",
        transport=transport,
        max_output_retries=1,
    ).extract(transcript)

    assert records[0].resolution == "Increase the heartbeat interval."
    assert responses == []


def test_bifrost_extractor_fails_after_invalid_json_retries_are_exhausted(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)

    def transport(url, headers, body, timeout):
        return 200, json.dumps({"choices": [{"message": {"content": "not-json"}}]})

    with pytest.raises(ExtractionError, match="invalid output"):
        BifrostExtractor(
            base_url="https://bifrost.example/openai",
            model="bedrock/m",
            api_key="k",
            transport=transport,
            max_output_retries=1,
        ).extract(transcript)


def test_extract_session_with_bifrost_posts_the_sanitized_session(tmp_path, monkeypatch):
    from memento.cli import run

    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)
    config = tmp_path / "config.toml"
    artifacts = tmp_path / "artifacts"
    config.write_text(
        f'''
operator_id = "op"
artifacts = "{artifacts}"

[extractor]
provider = "bifrost"

[extractor.bifrost]
base_url = "https://bifrost.example/openai"
model = "bedrock/m"
'''
    )
    observed: dict = {}

    def transport(url, headers, body, timeout):
        observed["url"] = url
        observed["authorization"] = headers["Authorization"]
        observed["model"] = body["model"]
        return _chat_body(_record_payload())

    monkeypatch.setattr("memento.extractors.bifrost.urllib_transport", transport)
    monkeypatch.setattr("memento.extractors.bifrost.read_keychain_secret", lambda service, account: "key")

    assert run(["--config", str(config), "extract-session", str(transcript)]) == 0
    assert observed["url"] == "https://bifrost.example/openai/v1/chat/completions"
    assert observed["authorization"] == "Bearer key"
    assert observed["model"] == "bedrock/m"
    saved = list(artifacts.rglob("*.json"))
    assert saved


def test_extract_session_stops_before_the_gateway_when_bifrost_has_no_model(tmp_path, monkeypatch):
    from memento.cli import run

    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)
    config = tmp_path / "config.toml"
    config.write_text(
        f'''
operator_id = "op"
artifacts = "{tmp_path / "artifacts"}"

[extractor]
provider = "bifrost"

[extractor.bifrost]
base_url = "https://bifrost.example/openai"
'''
    )

    def transport(url, headers, body, timeout):
        raise AssertionError("gateway was called")

    monkeypatch.setattr("memento.extractors.bifrost.urllib_transport", transport)

    assert run(["--config", str(config), "extract-session", str(transcript)]) == 3


def test_bifrost_extractor_does_not_call_the_gateway_without_a_key_or_endpoint(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript)

    def transport(url, headers, body, timeout):
        raise AssertionError("gateway was called")

    with pytest.raises(EngineConfigurationError, match="base_url"):
        BifrostExtractor(base_url=None, model="bedrock/m", api_key="k", transport=transport).extract(transcript)
    with pytest.raises(EngineConfigurationError, match="model"):
        BifrostExtractor(base_url="https://bifrost.example/openai", model=None, api_key="k", transport=transport).extract(transcript)

    def missing_secret(service, account):
        raise KeychainSecretError(f"no Keychain item for {service}")

    with pytest.raises(EngineConfigurationError, match="Keychain"):
        BifrostExtractor(
            base_url="https://bifrost.example/openai",
            model="bedrock/m",
            api_key=None,
            transport=transport,
            read_secret=missing_secret,
        ).extract(transcript)
