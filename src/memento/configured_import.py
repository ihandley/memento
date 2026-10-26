from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .app_config import ResolvedAppConfig, default_config_path
from .bifrost_client import resolve_api_key
from .confluence import fetch_confluence_page
from .envconfig import env_value
from .extractors import create_extractor
from .extractors.base import EngineConfigurationError, ProjectProvenance
from .extractors.raw_knowledge import raw_source_documents, raw_source_id
from .markdown_kb import MarkdownKnowledgeBaseExtractor, markdown_articles, markdown_source_id
from .pipeline import run_extraction
from .secrets import KeychainSecretError, read_keychain_secret
from .session_sources import SessionSource, claude_sessions, confluence_url_sources, cursor_sessions

_MODEL_SOURCES = frozenset({"claude", "cursor", "raw", "confluence"})
_CONFLUENCE_KEYCHAIN_SERVICE = "memento-atlassian-api-token"


def _adapter_report() -> dict:
    return {
        "discovered": 0,
        "eligible": 0,
        "activated": 0,
        "unchanged": 0,
        "changed_since_failure": 0,
        "blocked": 0,
        "failed": 0,
        "pending_retry": 0,
        "attention_required": [],
    }


def _failed_configuration(reason: str) -> dict:
    report = _adapter_report()
    report["failed"] = 1
    report["attention_required"].append({
        "source_type": "configuration",
        "source_id": "configuration",
        "status": "failed",
        "reason": reason,
        "next_action": "correct the configuration, then run import-sources again",
    })
    return report


def _cursor_database() -> Path:
    return Path.home() / "Library/Application Support/Cursor/User/globalStorage/conversation-search.db"


def confluence_url_path(resolved: ResolvedAppConfig) -> Path:
    base = resolved.config_path.parent if resolved.config_path else default_config_path().parent
    return base / "confluence-urls.txt"


def confluence_url_rows(path: Path) -> tuple[list[tuple[str, str]], str | None]:
    if not path.exists():
        return [], None
    rows: list[tuple[str, str]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 2:
            return [], f"{path.name}:{line_number} must be 'PROJECT_ID URL'"
        rows.append((parts[0], parts[1]))
    return rows, None


def _filter_since(sources: list[SessionSource], cutoff: float | None) -> list[SessionSource]:
    if cutoff is None:
        return sources
    return [source for source in sources if source.updated_at is None or source.updated_at >= cutoff]


def _provenance(source: SessionSource) -> ProjectProvenance | None:
    if not source.project_id:
        return None
    root = str(source.project_root) if source.project_root else None
    return ProjectProvenance(project_id=source.project_id, project_root=root)


def import_configured_sources(
    app_config,
    resolved: ResolvedAppConfig,
    artifacts: Path,
    *,
    dry_run: bool,
    resume: bool,
    cutoff: float | None,
    extractor,
    url_fetcher,
) -> tuple[dict, int]:
    """Run the configured adapters in order. A failure in one does not skip the rest."""

    from .cli import _bifrost_complete, _extractor_kwargs, _knowledge_extractor, _run_batch_candidates, _select_batch_candidates

    reports: dict[str, dict] = {}
    stopped_reason: str | None = None
    cannot_start = False
    claude_home = Path(os.getenv("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    cursor_temporary = tempfile.TemporaryDirectory()

    def discover_and_run(sources: list[SessionSource], run_one) -> tuple[dict, EngineConfigurationError | None]:
        candidates, counts, attention_required = _select_batch_candidates(sources, artifacts, resume)
        if dry_run:
            return {**counts, "attention_required": attention_required}, None
        engine_error = _run_batch_candidates(
            candidates, counts, attention_required, run_one, provider=resolved.extractor_provider
        )
        return {**counts, "attention_required": attention_required}, engine_error

    def model_extractor(source: SessionSource):
        if extractor is not None:
            return extractor
        return create_extractor(
            resolved.extractor_provider,
            project=_provenance(source),
            source_type=source.source_type,
            source_id=source.source_id,
            source_uri=source.source_uri,
            **_extractor_kwargs(resolved),
        )

    try:
        for name in resolved.sources:
            if name in _MODEL_SOURCES and stopped_reason is not None:
                reports[name] = _failed_configuration(stopped_reason)
                cannot_start = True
                continue
            if name in _MODEL_SOURCES and resolved.extractor_provider == "bifrost":
                try:
                    _bifrost_complete(resolved)
                    resolve_api_key(None, resolved.bifrost_base_url or "", read_keychain_secret)
                except EngineConfigurationError as error:
                    reports[name] = _failed_configuration(str(error))
                    stopped_reason = str(error)
                    cannot_start = True
                    continue

            engine_error: EngineConfigurationError | None = None
            if name == "claude":
                sources = _filter_since(claude_sessions(app_config.projects, claude_home), cutoff)

                def run_claude(source: SessionSource):
                    return run_extraction(
                        model_extractor(source), source.path, artifacts,
                        source_type=source.source_type, source_id=source.source_id, source_uri=source.source_uri,
                    )

                report, engine_error = discover_and_run(sources, run_claude)
            elif name == "cursor":
                sources = _filter_since(
                    cursor_sessions(_cursor_database(), Path(cursor_temporary.name)), cutoff
                )

                def run_cursor(source: SessionSource):
                    return run_extraction(
                        model_extractor(source), source.path, artifacts,
                        source_type=source.source_type, source_id=source.source_id, source_uri=source.source_uri,
                    )

                report, engine_error = discover_and_run(sources, run_cursor)
            elif name == "markdown":
                sources = []
                for project_id, project in app_config.projects.items():
                    wiki = project.knowledge_base
                    if wiki is None or not wiki.is_dir():
                        continue
                    for article in markdown_articles(wiki):
                        sources.append(SessionSource(
                            "markdown_knowledge_base",
                            markdown_source_id(project_id, wiki, article),
                            article,
                            project_id,
                            project.root,
                            updated_at=article.stat().st_mtime,
                        ))
                sources = _filter_since(sources, cutoff)

                def run_markdown(source: SessionSource):
                    selected = MarkdownKnowledgeBaseExtractor(
                        knowledge_base_id=source.project_id or "markdown",
                        project_id=source.project_id or "markdown",
                        project_root=str(source.project_root) if source.project_root else None,
                        operator_id=resolved.operator_id,
                    )
                    return run_extraction(
                        selected, source.path, artifacts,
                        source_type="markdown_knowledge_base", source_id=source.source_id,
                    )

                report, engine_error = discover_and_run(sources, run_markdown)
            elif name == "raw":
                sources = []
                for project_id, project in app_config.projects.items():
                    raw_dir = project.knowledge_base
                    if raw_dir is None or not raw_dir.is_dir():
                        continue
                    for document in raw_source_documents(raw_dir):
                        sources.append(SessionSource(
                            "raw_knowledge_source",
                            raw_source_id(project_id, raw_dir, document),
                            document,
                            project_id,
                            project.root,
                            updated_at=document.stat().st_mtime,
                        ))
                sources = _filter_since(sources, cutoff)

                def run_raw(source: SessionSource):
                    selected = extractor or _knowledge_extractor(
                        resolved,
                        None,
                        mode=resolved.cursor_mode,
                        model=resolved.cursor_model,
                        max_sanitized_chars=resolved.max_sanitized_chars,
                        timeout_seconds=resolved.timeout_seconds,
                        operator_id=resolved.operator_id,
                        project=_provenance(source),
                    )
                    return run_extraction(
                        selected, source.path, artifacts,
                        source_type=source.source_type, source_id=source.source_id,
                    )

                report, engine_error = discover_and_run(sources, run_raw)
            elif name == "confluence":
                rows, malformed = confluence_url_rows(confluence_url_path(resolved))
                if malformed:
                    reports[name] = _failed_configuration(malformed)
                    cannot_start = True
                    continue
                if not rows:
                    reports[name] = _adapter_report()
                    continue
                email = env_value("ATLASSIAN_EMAIL", "") or ""
                if not email:
                    reports[name] = _failed_configuration(
                        "MEMENTO_ATLASSIAN_EMAIL is required to import Confluence pages"
                    )
                    cannot_start = True
                    continue
                try:
                    token = read_keychain_secret(_CONFLUENCE_KEYCHAIN_SERVICE, email)
                except KeychainSecretError as error:
                    reports[name] = _failed_configuration(str(error))
                    cannot_start = True
                    continue
                grouped: dict[str, list[str]] = {}
                for project_id, url in rows:
                    grouped.setdefault(project_id, []).append(url)
                page_root = Path(cursor_temporary.name) / "confluence"
                sources = []
                fetch_failures: list[dict] = []
                for project_id, urls in grouped.items():
                    project = app_config.projects.get(project_id)
                    found, failures = confluence_url_sources(
                        urls,
                        page_root,
                        email=email,
                        token=token,
                        project_id=project_id,
                        project_root=project.root if project else None,
                        fetch=url_fetcher or fetch_confluence_page,
                    )
                    sources.extend(found)
                    fetch_failures.extend(failures)
                sources = _filter_since(sources, cutoff)

                def run_confluence(source: SessionSource):
                    selected = extractor or _knowledge_extractor(
                        resolved,
                        None,
                        mode=resolved.cursor_mode,
                        model=resolved.cursor_model,
                        max_sanitized_chars=resolved.max_sanitized_chars,
                        timeout_seconds=resolved.timeout_seconds,
                        operator_id=resolved.operator_id,
                        project=_provenance(source),
                        source_type="confluence_page",
                    )
                    return run_extraction(
                        selected, source.path, artifacts,
                        source_type=source.source_type, source_id=source.source_id, source_uri=source.source_uri,
                    )

                report, engine_error = discover_and_run(sources, run_confluence)
                for failure in fetch_failures:
                    report["failed"] += 1
                    report["attention_required"].append({
                        "source_type": "confluence_page",
                        "source_id": failure["url"],
                        "status": "failed",
                        "reason": failure["reason"],
                        "next_action": "correct the URL or Confluence access, then retry import-sources",
                    })
            else:
                continue
            if engine_error is not None:
                cannot_start = True
                stopped_reason = str(engine_error)
                report["failed"] += 1
                report["attention_required"].append({
                    "source_type": "configuration",
                    "source_id": "configuration",
                    "status": "failed",
                    "reason": stopped_reason,
                    "next_action": "correct the configuration, then run import-sources again",
                })
            reports[name] = report
    finally:
        cursor_temporary.cleanup()
    return {"adapters": reports}, 3 if cannot_start else 0
