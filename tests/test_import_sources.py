import json
from pathlib import Path

from memento.cli import run
from memento.confluence import ConfluencePage


class KeywordEmbedder:
    dimensions = 2
    model_name = "keyword-test"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0] for _text in texts]


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def _storage(tmp_path: Path) -> str:
    return f'''
operator_id = "op"
artifacts = "{tmp_path / "artifacts"}"
database = "{tmp_path / "database"}"
'''


def test_import_sources_uses_the_default_adapters_when_sources_is_omitted(tmp_path, capsys, monkeypatch):
    config = _config(tmp_path, _storage(tmp_path))
    monkeypatch.setattr("memento.configured_import._cursor_database", lambda: tmp_path / "missing-cursor.db")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))

    assert run(["--config", str(config), "import-sources"], KeywordEmbedder()) == 0

    payload = json.loads(capsys.readouterr().out)
    assert list(payload["adapters"]) == ["claude", "cursor", "confluence"]
    assert payload["adapters"]["claude"]["discovered"] == 0
    assert payload["indexed"] == 0


def test_empty_sources_imports_nothing(tmp_path, capsys):
    config = _config(tmp_path, _storage(tmp_path) + "sources = []\n")

    assert run(["--config", str(config), "import-sources"], KeywordEmbedder()) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["adapters"] == {}


def test_confluence_without_email_does_not_block_markdown(tmp_path, capsys, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "notes.md").write_text("# Notes\n\n## Summary\n\nRabbitMQ heartbeats expire.\n")
    project = tmp_path / "project"
    project.mkdir()
    config = _config(
        tmp_path,
        _storage(tmp_path)
        + f'sources = ["confluence", "markdown"]\n\n[projects.demo]\nroot = "{project}"\nknowledge_base = "{wiki}"\n',
    )
    (tmp_path / "confluence-urls.txt").write_text("demo https://example.atlassian.net/wiki/pages/1\n")
    monkeypatch.delenv("MEMENTO_ATLASSIAN_EMAIL", raising=False)
    monkeypatch.delenv("MEMORY_ATLASSIAN_EMAIL", raising=False)
    monkeypatch.delenv("SESSION_RAG_ATLASSIAN_EMAIL", raising=False)

    assert run(["--config", str(config), "import-sources", "--dry-run"], KeywordEmbedder()) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["adapters"]["confluence"]["failed"] == 1
    assert payload["adapters"]["markdown"]["discovered"] == 1
    assert "indexed" not in payload


def test_since_keeps_a_confluence_page_that_has_no_timestamp(tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    config = _config(
        tmp_path,
        _storage(tmp_path) + f'sources = ["confluence"]\n\n[projects.demo]\nroot = "{project}"\n',
    )
    (tmp_path / "confluence-urls.txt").write_text(
        "demo https://example.atlassian.net/wiki/spaces/SD/pages/1/Notes\n"
    )
    monkeypatch.setenv("MEMENTO_ATLASSIAN_EMAIL", "ian@example.com")
    monkeypatch.setattr("memento.configured_import.read_keychain_secret", lambda service, account: "token")

    def fetch(base_url, page_id, *, email, token, timeout=30):
        return ConfluencePage(
            base_url=base_url, page_id=page_id, title="Notes", text="Heartbeats.", version=1,
            url=f"{base_url}/wiki/spaces/SD/pages/{page_id}",
        )

    calls = {"n": 0}

    class CountingExtractor:
        name = "fake"
        model = "fake"
        prompt_version = 1

        def extract(self, document):
            calls["n"] += 1
            from conftest import make_record
            return [make_record(source=str(document), source_session_id=document.stem, source_type="confluence_page")]

    assert run(
        ["--config", str(config), "import-sources", "--since", "2099-01-01"],
        KeywordEmbedder(),
        CountingExtractor(),
        url_fetcher=fetch,
    ) == 0
    assert calls["n"] == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["adapters"]["confluence"]["activated"] == 1


def test_dry_run_reports_a_missing_bifrost_endpoint_and_still_counts_markdown(tmp_path, capsys):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "notes.md").write_text("# Notes\n\n## Summary\n\nRabbitMQ heartbeats expire.\n")
    project = tmp_path / "project"
    project.mkdir()
    config = _config(
        tmp_path,
        _storage(tmp_path)
        + 'sources = ["claude", "markdown"]\n\n[extractor]\nprovider = "bifrost"\n\n'
        + f'[projects.demo]\nroot = "{project}"\nknowledge_base = "{wiki}"\n',
    )

    assert run(["--config", str(config), "import-sources", "--dry-run"], KeywordEmbedder()) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["adapters"]["claude"]["failed"] == 1
    assert "base_url" in payload["adapters"]["claude"]["attention_required"][0]["reason"]
    assert payload["adapters"]["markdown"]["discovered"] == 1
    assert "indexed" not in payload


def test_dry_run_reports_a_missing_bifrost_key_and_still_counts_markdown(tmp_path, capsys, monkeypatch):
    from memento.secrets import KeychainSecretError

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "notes.md").write_text("# Notes\n\n## Summary\n\nRabbitMQ heartbeats expire.\n")
    project = tmp_path / "project"
    project.mkdir()
    config = _config(
        tmp_path,
        _storage(tmp_path)
        + 'sources = ["claude", "markdown"]\n\n'
        + '[extractor]\nprovider = "bifrost"\n\n'
        + '[extractor.bifrost]\nbase_url = "https://bifrost.example/openai"\nmodel = "bedrock/anthropic.claude"\n\n'
        + f'[projects.demo]\nroot = "{project}"\nknowledge_base = "{wiki}"\n',
    )

    def missing_key(service, account):
        raise KeychainSecretError("not in keychain")

    monkeypatch.setattr("memento.configured_import.read_keychain_secret", missing_key)

    assert run(["--config", str(config), "import-sources", "--dry-run"], KeywordEmbedder()) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["adapters"]["claude"]["failed"] == 1
    assert "Keychain" in payload["adapters"]["claude"]["attention_required"][0]["reason"]
    assert payload["adapters"]["markdown"]["discovered"] == 1
    assert "indexed" not in payload


def test_engine_refusal_stops_later_model_adapters(tmp_path, capsys, monkeypatch):
    from memento.extractors.base import EngineConfigurationError
    from memento.session_sources import _encode_claude_path

    project = tmp_path / "project"
    project.mkdir()
    claude_home = tmp_path / "claude"
    transcript_dir = claude_home / "projects" / _encode_claude_path(project)
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "session.jsonl").write_text("{}\n")
    config = _config(
        tmp_path,
        _storage(tmp_path) + f'sources = ["claude", "cursor"]\n\n[projects.demo]\nroot = "{project}"\n',
    )
    monkeypatch.setattr("memento.configured_import._cursor_database", lambda: tmp_path / "missing.db")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    class RefusingExtractor:
        name = "fake"
        model = "fake"
        prompt_version = 1

        def extract(self, document):
            raise EngineConfigurationError("HTTP 401")

    assert run(["--config", str(config), "import-sources"], KeywordEmbedder(), RefusingExtractor()) == 3

    payload = json.loads(capsys.readouterr().out)
    claude = payload["adapters"]["claude"]
    assert claude["failed"] == 1
    assert "401" in claude["attention_required"][0]["reason"]
    assert payload["adapters"]["cursor"]["failed"] == 1
    assert "401" in payload["adapters"]["cursor"]["attention_required"][0]["reason"]
