import json

from memento.cli import run


def _config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def _bifrost_config(tmp_path) -> str:
    return f'''
operator_id = "op"
artifacts = "{tmp_path / "artifacts"}"

[extractor]
provider = "bifrost"

[extractor.bifrost]
base_url = "https://bifrost.example/openai"
model = "bedrock/anthropic.claude"
'''


def test_check_engine_asks_bifrost_once_and_prints_that_it_responded(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, _bifrost_config(tmp_path))
    observed: dict = {}

    def transport(url, headers, body, timeout):
        observed["calls"] = observed.get("calls", 0) + 1
        observed["url"] = url
        observed["authorization"] = headers["Authorization"]
        observed["body"] = body
        return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("memento.cli.urllib_transport", transport)
    monkeypatch.setattr("memento.cli.read_keychain_secret", lambda service, account: "key")

    assert run(["--config", str(config), "check-engine"]) == 0

    assert observed["calls"] == 1
    assert observed["url"] == "https://bifrost.example/openai/v1/chat/completions"
    assert observed["authorization"] == "Bearer key"
    assert observed["body"]["model"] == "bedrock/anthropic.claude"
    assert observed["body"]["temperature"] == 0
    assert "max_tokens" not in observed["body"]
    assert "max_completion_tokens" not in observed["body"]
    assert "transcript" not in observed["body"]["messages"][0]["content"].lower()
    assert capsys.readouterr().out.strip() == "Bifrost responded"
    assert not (tmp_path / "artifacts" / "health-checks").exists()


def test_check_engine_stops_before_the_gateway_when_bifrost_has_no_model(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, f'''
operator_id = "op"
artifacts = "{tmp_path / "artifacts"}"

[extractor]
provider = "bifrost"

[extractor.bifrost]
base_url = "https://bifrost.example/openai"
''')

    def transport(url, headers, body, timeout):
        raise AssertionError("gateway was called")

    monkeypatch.setattr("memento.cli.urllib_transport", transport)

    assert run(["--config", str(config), "check-engine"]) == 3
    assert "model" in capsys.readouterr().err


def test_check_engine_reports_a_missing_bifrost_key(tmp_path, monkeypatch, capsys):
    from memento.secrets import KeychainSecretError

    config = _config(tmp_path, _bifrost_config(tmp_path))

    def transport(url, headers, body, timeout):
        raise AssertionError("gateway was called")

    def missing_key(service, account):
        raise KeychainSecretError("not in keychain")

    monkeypatch.setattr("memento.cli.urllib_transport", transport)
    monkeypatch.setattr("memento.cli.read_keychain_secret", missing_key)

    assert run(["--config", str(config), "check-engine"]) == 3
    assert "Keychain" in capsys.readouterr().err


def test_check_engine_treats_bifrost_refusal_as_configuration(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, _bifrost_config(tmp_path))
    monkeypatch.setattr("memento.cli.read_keychain_secret", lambda service, account: "key")
    monkeypatch.setattr(
        "memento.cli.urllib_transport",
        lambda url, headers, body, timeout: (401, "no"),
    )

    assert run(["--config", str(config), "check-engine"]) == 3
    assert "401" in capsys.readouterr().err


def test_check_engine_names_bifrost_when_the_gateway_is_unavailable(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, _bifrost_config(tmp_path))
    monkeypatch.setattr("memento.cli.read_keychain_secret", lambda service, account: "key")
    monkeypatch.setattr(
        "memento.cli.urllib_transport",
        lambda url, headers, body, timeout: (429, "slow"),
    )

    assert run(["--config", str(config), "check-engine"]) == 1
    assert "Bifrost" in capsys.readouterr().err


def test_check_engine_honors_a_one_run_extractor_override(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, f'''
operator_id = "op"
artifacts = "{tmp_path / "artifacts"}"

[extractor]
provider = "cursor"

[extractor.bifrost]
base_url = "https://bifrost.example/openai"
model = "bedrock/anthropic.claude"
''')
    observed: dict = {}

    def transport(url, headers, body, timeout):
        observed["model"] = body["model"]
        return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("memento.cli.urllib_transport", transport)
    monkeypatch.setattr("memento.cli.read_keychain_secret", lambda service, account: "key")

    assert run(["--config", str(config), "--extractor", "bifrost", "check-engine"]) == 0
    assert observed["model"] == "bedrock/anthropic.claude"
    assert capsys.readouterr().out.strip() == "Bifrost responded"


def test_check_engine_prints_that_cursor_responded(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, f'''
operator_id = "op"
artifacts = "{tmp_path / "artifacts"}"
''')
    calls = {"n": 0}

    def probe(prompt, **kwargs):
        calls["n"] += 1
        calls["prompt"] = prompt
        return "ok"

    monkeypatch.setattr("memento.cli.probe_cursor", probe)

    assert run(["--config", str(config), "check-engine"]) == 0
    assert calls["n"] == 1
    assert "transcript" not in calls["prompt"].lower()
    assert capsys.readouterr().out.strip() == "Cursor responded"
    assert not (tmp_path / "artifacts" / "health-checks").exists()


def test_check_engine_names_cursor_when_the_agent_times_out(tmp_path, monkeypatch, capsys):
    import subprocess

    config = _config(tmp_path, f'''
operator_id = "op"
artifacts = "{tmp_path / "artifacts"}"
''')

    def probe(prompt, **kwargs):
        raise subprocess.TimeoutExpired("cursor-agent", 1)

    monkeypatch.setattr("memento.cli.probe_cursor", probe)

    assert run(["--config", str(config), "check-engine"]) == 1
    assert "Cursor" in capsys.readouterr().err

