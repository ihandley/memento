from __future__ import annotations

import os
import json
import re
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

from .envconfig import ENV_PREFIX, LEGACY_ENV_PREFIXES


class ConfigError(ValueError):
    """The application configuration exists but is invalid."""


PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ExtractorSettings:
    provider: str = "cursor"
    mode: str = "ask"
    model: str = "auto"
    max_sanitized_chars: int = 20_000


@dataclass(frozen=True)
class ProjectSettings:
    root: Path
    knowledge_base: Path | None = None
    # Git worktrees of `root`: their sessions and working directories belong to
    # this project rather than becoming separate ones.
    worktrees: tuple[Path, ...] = ()
    discovered: bool = False


@dataclass(frozen=True)
class DiscoverySettings:
    """Directories whose Git repositories are registered implicitly. Explicit
    `[projects.*]` tables always win; `exclude` holds repo directory names."""

    roots: tuple[Path, ...] = ()
    exclude: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AppConfig:
    path: Path | None = None
    operator_id: str | None = None
    artifacts: Path | None = None
    database: Path | None = None
    extractor: ExtractorSettings = field(default_factory=ExtractorSettings)
    projects: dict[str, ProjectSettings] = field(default_factory=dict)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)


@dataclass(frozen=True)
class ResolvedAppConfig:
    config_path: Path | None
    operator_id: str | None
    artifacts: Path | None
    database: Path | None
    extractor_provider: str
    cursor_mode: str
    cursor_model: str
    max_sanitized_chars: int
    project_id: str | None
    project_root: Path | None
    knowledge_base: Path | None


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    configured = env.get("MEMENTO_CONFIG") or env.get("MEMORY_CONFIG") or env.get("SESSION_RAG_CONFIG")
    if configured:
        return Path(configured).expanduser()
    xdg_home = env.get("XDG_CONFIG_HOME")
    base = Path(xdg_home).expanduser() if xdg_home else Path.home() / ".config"
    return base / "memento" / "config.toml"


def _legacy_config_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Older config locations, checked in order, oldest last."""

    env = environ if environ is not None else os.environ
    xdg_home = env.get("XDG_CONFIG_HOME")
    base = Path(xdg_home).expanduser() if xdg_home else Path.home() / ".config"
    return [base / "memory" / "config.toml", base / "session-rag" / "config.toml"]


def _optional_path(value: object, *, field_name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty path string")
    return Path(value).expanduser()


def _string(value: object, *, field_name: str, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string")
    return value.strip()


def _integer(value: object, *, field_name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{field_name} must be a positive integer")
    return value


def load_app_config(
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    include_discovered: bool = True,
) -> AppConfig:
    config = _load_explicit_config(path, environ=environ)
    if not include_discovered:
        return config
    env = environ if environ is not None else os.environ
    discovery = _discovery_settings(config.discovery, env)
    if not discovery.roots:
        return config
    projects = discover_projects(discovery, config.projects)
    return replace(config, projects=projects, discovery=discovery)


def _discovery_settings(configured: DiscoverySettings, env: Mapping[str, str]) -> DiscoverySettings:
    """Env vars replace the TOML values (CLI > env > TOML)."""

    roots_env = _env(env, "PROJECT_ROOTS")
    exclude_env = _env(env, "PROJECT_EXCLUDE")
    roots = (
        tuple(Path(entry).expanduser() for entry in roots_env.split(os.pathsep) if entry.strip())
        if roots_env is not None
        else configured.roots
    )
    exclude = (
        frozenset(name.strip() for name in re.split(rf"[,{re.escape(os.pathsep)}]", exclude_env) if name.strip())
        if exclude_env is not None
        else configured.exclude
    )
    return DiscoverySettings(roots=roots, exclude=exclude)


def _load_explicit_config(path: Path | None, *, environ: Mapping[str, str] | None) -> AppConfig:
    explicit_path = path is not None
    selected_path = path.expanduser() if path is not None else default_config_path(environ)
    env = environ if environ is not None else os.environ
    has_explicit_env_path = bool(env.get("MEMENTO_CONFIG") or env.get("MEMORY_CONFIG") or env.get("SESSION_RAG_CONFIG"))
    if not explicit_path and not has_explicit_env_path and not selected_path.exists():
        for legacy_path in _legacy_config_paths(environ):
            if legacy_path.exists():
                selected_path = legacy_path
                break
    if not selected_path.exists():
        if explicit_path:
            raise ConfigError(f"config file does not exist: {selected_path}")
        return AppConfig(path=None)
    try:
        raw = tomllib.loads(selected_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {selected_path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"{selected_path} must contain a TOML table")

    extractor_raw = raw.get("extractor", {})
    if not isinstance(extractor_raw, dict):
        raise ConfigError("extractor must be a TOML table")
    extractor = ExtractorSettings(
        provider=_string(extractor_raw.get("provider"), field_name="extractor.provider", default="cursor"),
        mode=_string(extractor_raw.get("mode"), field_name="extractor.mode", default="ask"),
        model=_string(extractor_raw.get("model"), field_name="extractor.model", default="auto"),
        max_sanitized_chars=_integer(
            extractor_raw.get("max_sanitized_chars"),
            field_name="extractor.max_sanitized_chars",
            default=20_000,
        ),
    )
    if extractor.mode not in {"ask", "plan"}:
        raise ConfigError("extractor.mode must be 'ask' or 'plan'")

    projects_raw = raw.get("projects", {})
    if not isinstance(projects_raw, dict):
        raise ConfigError("projects must be a TOML table")
    projects: dict[str, ProjectSettings] = {}
    for project_id, project_raw in projects_raw.items():
        if not isinstance(project_raw, dict):
            raise ConfigError(f"projects.{project_id} must be a TOML table")
        root = _optional_path(project_raw.get("root"), field_name=f"projects.{project_id}.root")
        if root is None:
            raise ConfigError(f"projects.{project_id}.root is required")
        projects[project_id] = ProjectSettings(
            root=root,
            knowledge_base=_optional_path(
                project_raw.get("knowledge_base"), field_name=f"projects.{project_id}.knowledge_base"
            ),
        )

    discovery_raw = raw.get("discovery", {})
    if not isinstance(discovery_raw, dict):
        raise ConfigError("discovery must be a TOML table")
    discovery = DiscoverySettings(
        roots=tuple(_string_list(discovery_raw.get("roots"), field_name="discovery.roots", path=True)),
        exclude=frozenset(_string_list(discovery_raw.get("exclude"), field_name="discovery.exclude")),
    )

    return AppConfig(
        path=selected_path,
        operator_id=_string(raw.get("operator_id"), field_name="operator_id"),
        artifacts=_optional_path(raw.get("artifacts"), field_name="artifacts"),
        database=_optional_path(raw.get("database"), field_name="database"),
        extractor=extractor,
        projects=projects,
        discovery=discovery,
    )


def _string_list(value: object, *, field_name: str, path: bool = False) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{field_name} must be a list of non-empty strings")
    return [Path(item).expanduser() if path else item.strip() for item in value]


def _linked_worktree_main(path: Path) -> Path | None:
    """The main checkout a linked worktree belongs to, or None when `path` is
    not one (a main checkout's `.git` is a directory; submodules also have a
    `.git` file but point into `.git/modules`, not `.git/worktrees`)."""

    git = path / ".git"
    if not git.is_file():
        return None
    try:
        first_line = git.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first_line.startswith("gitdir:"):
        return None
    gitdir = (path / first_line.removeprefix("gitdir:").strip()).resolve()
    if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
        return None
    return gitdir.parent.parent.parent


def _git_checkouts(root: Path) -> list[Path]:
    """Git checkouts directly under `root`, plus those one level inside a
    non-repo child (e.g. `lvcore-worktrees/<branch>`)."""

    found: list[Path] = []
    try:
        children = sorted(child for child in root.expanduser().iterdir() if child.is_dir() and not child.name.startswith("."))
    except OSError:
        return found
    for child in children:
        if (child / ".git").exists():
            found.append(child)
            continue
        try:
            found.extend(sorted(g for g in child.iterdir() if g.is_dir() and not g.name.startswith(".") and (g / ".git").exists()))
        except OSError:
            continue
    return found


def discover_projects(discovery: DiscoverySettings, explicit: dict[str, ProjectSettings]) -> dict[str, ProjectSettings]:
    """Merge Git repositories found under `discovery.roots` into `explicit`.

    A main checkout becomes a project named after its directory. A linked
    worktree is attached to its main checkout's project (explicit or
    discovered) and never becomes a project itself; a worktree whose main
    checkout is not a known project is ignored. Explicit projects win on any ID
    or root conflict, and `exclude` only filters discovered projects."""

    projects = dict(explicit)
    known_roots = {project.root.expanduser().resolve() for project in projects.values()}
    checkouts = [checkout for root in discovery.roots for checkout in _git_checkouts(root)]
    for checkout in checkouts:
        resolved = checkout.resolve()
        if _linked_worktree_main(checkout) is not None or resolved in known_roots:
            continue
        identifier = checkout.name
        if identifier in discovery.exclude or identifier in projects or not PROJECT_ID_PATTERN.fullmatch(identifier):
            continue
        projects[identifier] = ProjectSettings(root=resolved, discovered=True)
        known_roots.add(resolved)

    by_root = {project.root.expanduser().resolve(): identifier for identifier, project in projects.items()}
    for checkout in checkouts:
        main = _linked_worktree_main(checkout)
        identifier = by_root.get(main.resolve()) if main is not None else None
        if identifier is None:
            continue
        project = projects[identifier]
        worktree = checkout.resolve()
        if worktree not in project.worktrees:
            projects[identifier] = replace(project, worktrees=(*project.worktrees, worktree))
    return projects


def _nearest_project_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ConfigError(f"project path is not a directory: {resolved}")
    return next((candidate for candidate in (resolved, *resolved.parents) if (candidate / ".git").exists()), resolved)


def add_project(config_path: Path, path: Path, project_id: str | None = None) -> tuple[str, Path, bool]:
    """Append a project to the global TOML config without rewriting existing content."""
    root = _nearest_project_root(path)
    identifier = project_id or root.name
    if not PROJECT_ID_PATTERN.fullmatch(identifier):
        raise ConfigError("project ID must use only letters, numbers, dots, underscores, or hyphens")

    existing = load_app_config(config_path, include_discovered=False) if config_path.exists() else AppConfig()
    if identifier in existing.projects:
        registered_root = existing.projects[identifier].root.expanduser().resolve()
        if registered_root == root:
            return identifier, root, False
        raise ConfigError(f"project {identifier!r} is already registered with root {registered_root}")
    for registered_id, project in existing.projects.items():
        if project.root.expanduser().resolve() == root:
            raise ConfigError(f"project root {root} is already registered as {registered_id!r}")

    config_path = config_path.expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    current = config_path.read_text() if config_path.exists() else ""
    separator = "" if not current else ("\n" if current.endswith("\n") else "\n\n")
    addition = f'{separator}[projects.{json.dumps(identifier)}]\nroot = {json.dumps(str(root))}\n'
    with tempfile.NamedTemporaryFile("w", dir=config_path.parent, delete=False) as temporary:
        temporary.write(current + addition)
        temporary_path = Path(temporary.name)
    if config_path.exists():
        temporary_path.chmod(config_path.stat().st_mode)
    temporary_path.replace(config_path)
    return identifier, root, True


def _project_for_cwd(config: AppConfig, cwd: Path) -> tuple[str | None, ProjectSettings | None]:
    matches: list[tuple[int, str, ProjectSettings]] = []
    resolved_cwd = cwd.expanduser().resolve()
    for project_id, project in config.projects.items():
        for candidate in (project.root, *project.worktrees):
            root = candidate.resolve()
            if resolved_cwd == root or resolved_cwd.is_relative_to(root):
                matches.append((len(root.parts), project_id, project))
    if not matches:
        return None, None
    _, project_id, project = max(matches, key=lambda match: match[0])
    return project_id, project


def _first(*values):
    return next((value for value in values if value is not None and value != ""), None)


def _env(env: Mapping[str, str], suffix: str) -> str | None:
    value = env.get(ENV_PREFIX + suffix)
    if value is not None:
        return value
    for legacy_prefix in LEGACY_ENV_PREFIXES:
        value = env.get(legacy_prefix + suffix)
        if value is not None:
            return value
    return None


def _resolved_path(value: object) -> Path | None:
    if value is None or value == "":
        return None
    return Path(str(value)).expanduser()


def resolve_app_config(
    config: AppConfig,
    *,
    cwd: Path,
    environ: Mapping[str, str] | None = None,
    explicit: Mapping[str, object] | None = None,
) -> ResolvedAppConfig:
    env = environ if environ is not None else os.environ
    cli = explicit or {}

    selected_project_id = _first(cli.get("project_id"), _env(env, "PROJECT_ID"))
    matched_id, matched_project = _project_for_cwd(config, cwd)
    if selected_project_id is None:
        selected_project_id = matched_id
    configured_project = config.projects.get(str(selected_project_id)) if selected_project_id else matched_project
    project_root = _resolved_path(
        _first(
            cli.get("project_root"),
            _env(env, "PROJECT_ROOT"),
            configured_project.root if configured_project else None,
        )
    )

    max_chars_raw = _first(
        cli.get("max_sanitized_chars"),
        _env(env, "MAX_SANITIZED_CHARS"),
        config.extractor.max_sanitized_chars,
    )
    try:
        max_chars = int(max_chars_raw)
    except (TypeError, ValueError) as error:
        raise ConfigError("max_sanitized_chars must be a positive integer") from error
    if max_chars <= 0:
        raise ConfigError("max_sanitized_chars must be a positive integer")

    return ResolvedAppConfig(
        config_path=config.path,
        operator_id=_first(cli.get("operator_id"), _env(env, "OPERATOR_ID"), config.operator_id),
        artifacts=_resolved_path(_first(cli.get("artifacts"), _env(env, "ARTIFACTS"), config.artifacts)),
        database=_resolved_path(_first(cli.get("database"), _env(env, "DATABASE"), config.database)),
        extractor_provider=str(
            _first(cli.get("extractor_provider"), _env(env, "EXTRACTOR"), config.extractor.provider)
        ),
        cursor_mode=str(_first(cli.get("cursor_mode"), _env(env, "CURSOR_MODE"), config.extractor.mode)),
        cursor_model=str(_first(cli.get("cursor_model"), _env(env, "CURSOR_MODEL"), config.extractor.model)),
        max_sanitized_chars=max_chars,
        project_id=str(selected_project_id) if selected_project_id else None,
        project_root=project_root,
        knowledge_base=configured_project.knowledge_base if configured_project else None,
    )
