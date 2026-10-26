from pathlib import Path

import pytest

from memento.app_config import ConfigError, add_project, load_app_config, resolve_app_config


def write_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
operator_id = "config-operator"
artifacts = "~/configured-artifacts"
database = "~/configured-lancedb"

[extractor]
provider = "cursor"
mode = "plan"
model = "configured-model"
max_sanitized_chars = 123456
timeout_seconds = 240

[projects.lvcore]
root = "/work/lvcore"
knowledge_base = "/knowledge/lvcore/Wiki"
""".strip()
        + "\n"
    )


def test_loads_default_config_from_xdg_config_home(tmp_path, monkeypatch):
    config_path = tmp_path / "xdg" / "memory" / "config.toml"
    write_config(config_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MEMORY_CONFIG", raising=False)
    monkeypatch.delenv("SESSION_RAG_CONFIG", raising=False)

    config = load_app_config()

    assert config.path == config_path
    assert config.operator_id == "config-operator"
    assert config.extractor.max_sanitized_chars == 123456
    assert config.extractor.timeout_seconds == 240
    assert config.projects["lvcore"].root == Path("/work/lvcore")


def test_memory_environment_overrides_legacy_memento_environment(tmp_path):
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    config = load_app_config(config_path)

    resolved = resolve_app_config(
        config,
        cwd=Path("/work/lvcore"),
        environ={
            "MEMORY_OPERATOR_ID": "memory-operator",
            "SESSION_RAG_OPERATOR_ID": "legacy-operator",
        },
    )

    assert resolved.operator_id == "memory-operator"


def test_legacy_config_is_loaded_when_canonical_config_does_not_exist(tmp_path, monkeypatch):
    legacy = tmp_path / "xdg" / "session-rag" / "config.toml"
    write_config(legacy)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MEMORY_CONFIG", raising=False)
    monkeypatch.delenv("SESSION_RAG_CONFIG", raising=False)

    config = load_app_config()

    assert config.path == legacy


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="config file does not exist"):
        load_app_config(tmp_path / "missing.toml")


def test_omitted_sources_mean_claude_cursor_and_confluence(tmp_path):
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    config = load_app_config(config_path)
    resolved = resolve_app_config(config, cwd=Path("/work/lvcore"), environ={})

    assert config.sources is None
    assert resolved.sources == ("claude", "cursor", "confluence")
    assert resolved.bifrost_base_url is None
    assert resolved.bifrost_model is None


def test_sources_and_bifrost_settings_are_kept_when_cursor_is_selected(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
sources = ["markdown", "claude"]

[extractor]
provider = "cursor"
mode = "ask"
model = "cursor-model"

[extractor.bifrost]
base_url = "https://bifrost.example/openai"
model = "bedrock/anthropic.claude"
""".strip()
        + "\n"
    )

    resolved = resolve_app_config(load_app_config(config_path), cwd=tmp_path, environ={})

    assert resolved.extractor_provider == "cursor"
    assert resolved.cursor_model == "cursor-model"
    assert resolved.sources == ("markdown", "claude")
    assert resolved.bifrost_base_url == "https://bifrost.example/openai"
    assert resolved.bifrost_model == "bedrock/anthropic.claude"


def test_empty_sources_imports_nothing(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("sources = []\n")

    resolved = resolve_app_config(load_app_config(config_path), cwd=tmp_path, environ={})

    assert resolved.sources == ()


def test_extractor_env_rejects_an_unknown_provider(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")

    with pytest.raises(ConfigError, match="provider"):
        resolve_app_config(
            load_app_config(config_path),
            cwd=tmp_path,
            environ={"MEMENTO_EXTRACTOR": "openai"},
        )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("sources = [\"nope\"]\n", "unknown"),
        ("sources = [\"claude\", \"claude\"]\n", "repeated"),
        ("[extractor]\nprovider = \"openai\"\n", "provider"),
    ],
)
def test_sources_and_provider_names_are_rejected(tmp_path, text, match):
    config_path = tmp_path / "config.toml"
    config_path.write_text(text)

    with pytest.raises(ConfigError, match=match):
        load_app_config(config_path)


def test_current_directory_selects_the_most_specific_configured_project(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[projects.work]
root = "/work"

[projects.lvcore]
root = "/work/lvcore"
""".strip()
        + "\n"
    )
    config = load_app_config(config_path)

    resolved = resolve_app_config(config, cwd=Path("/work/lvcore/src"), environ={})

    assert resolved.project_id == "lvcore"
    assert resolved.project_root == Path("/work/lvcore")


def test_environment_overrides_config_values(tmp_path):
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    config = load_app_config(config_path)

    resolved = resolve_app_config(
        config,
        cwd=Path("/work/lvcore"),
        environ={
            "SESSION_RAG_OPERATOR_ID": "env-operator",
            "SESSION_RAG_ARTIFACTS": "/env/artifacts",
            "SESSION_RAG_DATABASE": "/env/database",
            "SESSION_RAG_CURSOR_MODE": "ask",
            "SESSION_RAG_CURSOR_MODEL": "env-model",
            "SESSION_RAG_MAX_SANITIZED_CHARS": "654321",
        },
    )

    assert resolved.operator_id == "env-operator"
    assert resolved.artifacts == Path("/env/artifacts")
    assert resolved.database == Path("/env/database")
    assert resolved.cursor_mode == "ask"
    assert resolved.cursor_model == "env-model"
    assert resolved.max_sanitized_chars == 654321


def test_explicit_values_override_environment_and_config(tmp_path):
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    config = load_app_config(config_path)

    resolved = resolve_app_config(
        config,
        cwd=Path("/work/lvcore"),
        environ={"SESSION_RAG_OPERATOR_ID": "env-operator"},
        explicit={
            "operator_id": "cli-operator",
            "artifacts": "/cli/artifacts",
            "database": "/cli/database",
            "project_id": "cli-project",
            "project_root": "/cli/project",
            "cursor_mode": "ask",
            "cursor_model": "cli-model",
            "max_sanitized_chars": 777,
        },
    )

    assert resolved.operator_id == "cli-operator"
    assert resolved.artifacts == Path("/cli/artifacts")
    assert resolved.database == Path("/cli/database")
    assert resolved.project_id == "cli-project"
    assert resolved.project_root == Path("/cli/project")
    assert resolved.cursor_model == "cli-model"
    assert resolved.max_sanitized_chars == 777


def test_timeout_seconds_precedence_is_explicit_then_environment_then_config_then_default(tmp_path):
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    config = load_app_config(config_path)
    env = {"MEMENTO_TIMEOUT_SECONDS": "360"}

    explicit = resolve_app_config(config, cwd=tmp_path, environ=env, explicit={"timeout_seconds": 480})
    from_env = resolve_app_config(config, cwd=tmp_path, environ=env)
    from_config = resolve_app_config(config, cwd=tmp_path, environ={})
    default = resolve_app_config(load_app_config(), cwd=tmp_path, environ={})

    assert explicit.timeout_seconds == 480
    assert from_env.timeout_seconds == 360
    assert from_config.timeout_seconds == 240
    assert default.timeout_seconds == 120


@pytest.mark.parametrize("value", ["0", "-5", '"slow"'])
def test_timeout_seconds_must_be_a_positive_integer(tmp_path, value):
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"[extractor]\ntimeout_seconds = {value}\n")

    with pytest.raises(ConfigError, match="timeout_seconds"):
        load_app_config(config_path)


def test_add_project_appends_without_rewriting_existing_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('# keep this comment\noperator_id = "ian"\n')
    project = tmp_path / "schedule-management-service"
    project.mkdir()

    identifier, root, added = add_project(config_path, project)

    assert (identifier, root, added) == ("schedule-management-service", project.resolve(), True)
    assert config_path.read_text().startswith('# keep this comment\noperator_id = "ian"\n')
    assert load_app_config(config_path).projects[identifier].root == project.resolve()


def test_add_project_uses_nearest_git_root_and_is_idempotent(tmp_path):
    project = tmp_path / "repo"
    nested = project / "src" / "feature"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    config_path = tmp_path / "config.toml"

    assert add_project(config_path, nested) == ("repo", project.resolve(), True)
    assert add_project(config_path, project) == ("repo", project.resolve(), False)


def test_add_project_rejects_conflicting_id_or_root(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_path = tmp_path / "config.toml"
    add_project(config_path, first, "service")

    with pytest.raises(ConfigError, match="already registered with root"):
        add_project(config_path, second, "service")
    with pytest.raises(ConfigError, match="already registered as"):
        add_project(config_path, first, "another-name")


def _git_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _git_worktree(path: Path, main: Path, name: str) -> Path:
    gitdir = main / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    path.mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {gitdir}\n")
    return path


def _discovering_config(tmp_path: Path, root: Path, extra: str = "") -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[discovery]\nroots = ["{root}"]\n{extra}')
    return config_path


def test_discovery_registers_git_repos_and_skips_non_repos(tmp_path):
    root = tmp_path / "work"
    _git_repo(root / "alpha")
    _git_repo(root / "beta")
    (root / "coverage").mkdir()

    config = load_app_config(_discovering_config(tmp_path, root))

    assert set(config.projects) == {"alpha", "beta"}
    assert config.projects["alpha"].discovered
    assert config.projects["alpha"].root == (root / "alpha").resolve()


def test_discovery_folds_worktrees_into_the_main_project(tmp_path):
    root = tmp_path / "work"
    main = _git_repo(root / "alpha")
    inline = _git_worktree(root / "alpha-JET-1", main, "alpha-JET-1")
    nested = _git_worktree(root / "alpha-worktrees" / "feature", main, "feature")
    _git_worktree(root / "orphan-wt", tmp_path / "elsewhere", "orphan-wt")

    config = load_app_config(_discovering_config(tmp_path, root))

    assert set(config.projects) == {"alpha"}
    assert set(config.projects["alpha"].worktrees) == {inline.resolve(), nested.resolve()}


def test_explicit_project_wins_and_receives_worktrees(tmp_path):
    root = tmp_path / "work"
    main = _git_repo(root / "alpha")
    worktree = _git_worktree(root / "alpha-wt", main, "alpha-wt")
    _git_repo(root / "other")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[discovery]\nroots = ["{root}"]\n\n[projects.renamed]\nroot = "{main}"\nknowledge_base = "/kb"\n'
    )

    config = load_app_config(config_path)

    assert set(config.projects) == {"renamed", "other"}
    assert not config.projects["renamed"].discovered
    assert config.projects["renamed"].knowledge_base == Path("/kb")
    assert config.projects["renamed"].worktrees == (worktree.resolve(),)


def test_discovery_exclude_filters_only_discovered_projects(tmp_path):
    root = tmp_path / "work"
    _git_repo(root / "alpha")
    _git_repo(root / "secret")
    explicit = _git_repo(root / "kept")
    config_path = _discovering_config(
        tmp_path, root, f'exclude = ["secret", "kept"]\n\n[projects.kept]\nroot = "{explicit}"\n'
    )

    config = load_app_config(config_path)

    assert set(config.projects) == {"alpha", "kept"}


def test_discovery_env_overrides_toml_and_works_without_a_config_file(tmp_path, monkeypatch):
    toml_root, env_root = tmp_path / "toml", tmp_path / "env"
    _git_repo(toml_root / "from-toml")
    _git_repo(env_root / "from-env")
    _git_repo(env_root / "skipped")
    monkeypatch.setenv("MEMENTO_PROJECT_ROOTS", str(env_root))
    monkeypatch.setenv("MEMENTO_PROJECT_EXCLUDE", "skipped")

    with_toml = load_app_config(_discovering_config(tmp_path, toml_root))
    without_file = load_app_config(
        environ={"MEMENTO_CONFIG": str(tmp_path / "missing.toml"), "MEMENTO_PROJECT_ROOTS": str(env_root)}
    )

    assert set(with_toml.projects) == {"from-env"}
    assert set(without_file.projects) == {"from-env", "skipped"}


def test_cwd_inside_a_worktree_resolves_to_the_main_project(tmp_path):
    root = tmp_path / "work"
    main = _git_repo(root / "alpha")
    worktree = _git_worktree(root / "alpha-wt", main, "alpha-wt")
    config = load_app_config(_discovering_config(tmp_path, root))

    resolved = resolve_app_config(config, cwd=worktree)

    assert resolved.project_id == "alpha"


def test_add_project_ignores_discovered_projects(tmp_path):
    root = tmp_path / "work"
    repo = _git_repo(root / "alpha")
    config_path = _discovering_config(tmp_path, root)

    identifier, _, created = add_project(config_path, repo, "alpha-pinned")

    assert (identifier, created) == ("alpha-pinned", True)
