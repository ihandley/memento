from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .app_config import ConfigError, ResolvedAppConfig, add_project, default_config_path, load_app_config, resolve_app_config
from .artifacts import artifact_path, find_record, find_sources_by_project, forget_source, job_failures_for_project, load_active_episode_records, source_hash
from .embeddings import FastEmbedder
from .deduplication import DEFAULT_SEMANTIC_DUPLICATE_THRESHOLD, reconcile_duplicates
from .extractors import create_extractor
from .extractors.cursor import CursorExtractor
from .extractors.raw_knowledge import RawKnowledgeExtractor, raw_source_documents, raw_source_id
from .extractors.base import KnowledgeExtractor, ProjectProvenance
from .hook import format_context, handle_user_prompt
from .health import CursorHealthChecker, HealthChecker, deterministic_findings, grounded_findings, write_report
from .markdown_kb import MarkdownKnowledgeBaseExtractor, markdown_articles, markdown_source_id
from .overlay import (
    InvalidTransition,
    SupersedeRequiresReplacement,
    UnknownReplacementRecord,
    filter_retrievable,
    forget_records,
    read_state,
    reject,
    supersede,
    verify,
)
from .pipeline import run_extraction
from .report import generate_report, never_retrieved, render_report
from .retrieval import RetrievalScope
from .retrieval import purge_traces
from .retrieval import search as retrieval_search
from .store import Embedder, delete_by_source_id, index_episode_records
from .session_sources import SessionSource, claude_sessions, confluence_url_sources, cursor_sessions, parse_since
from .confluence import fetch_confluence_page
from .secrets import KeychainSecretError, read_keychain_secret
from .envconfig import env_value

_CONFLUENCE_KEYCHAIN_SERVICE = "memento-atlassian-api-token"


_NEXT_ACTION = {
    "blocked": "resolve the stated reason, then use --resume if the source is unchanged; use a normal import if it changes",
    "failed": "correct the extractor or input problem, then use --resume if the source is unchanged",
    "pending_retry": "restore Cursor availability or quota, then run the same import with --resume",
}


def _select_batch_candidates(
    sources: list[SessionSource], artifacts: Path, resume: bool
) -> tuple[list[SessionSource], dict, list[dict]]:
    """Shared by every batch-import command (Claude/Cursor sessions, RAW
    sources, Confluence URLs): partitions discovered sources into what's
    eligible to (re-)extract, skipping unchanged revisions and — under
    --resume — anything that hasn't previously failed or whose content
    changed since that failure."""

    counts = {
        "discovered": len(sources), "eligible": 0, "activated": 0, "unchanged": 0,
        "changed_since_failure": 0, "blocked": 0, "failed": 0, "pending_retry": 0,
    }
    attention_required: list[dict] = []
    candidates: list[SessionSource] = []
    for source in sources:
        hash_value = source_hash(source.path)
        existing = artifact_path(artifacts, source_type=source.source_type, source_id=source.source_id, hash_value=hash_value).exists()
        status_file = artifacts / source.source_type / source.source_id / "job_status.json"
        if resume:
            if not status_file.exists():
                continue
            attempted_hash = json.loads(status_file.read_text()).get("attempted_hash")
            if attempted_hash != hash_value:
                counts["changed_since_failure"] += 1
                attention_required.append({
                    "source_type": source.source_type,
                    "source_id": source.source_id,
                    "status": "changed_since_failure",
                    "reason": "source content changed since the recorded failed attempt",
                    "next_action": "run a normal import without --resume to process the new revision",
                })
                continue
        if not resume and existing:
            counts["unchanged"] += 1
            continue
        candidates.append(source)
    counts["eligible"] = len(candidates)
    return candidates, counts, attention_required


def _run_batch_candidates(candidates, counts: dict, attention_required: list[dict], run_one) -> None:
    """Runs `run_one(source) -> ExtractionOutcome` over each candidate,
    folding the result into `counts`/`attention_required` in place."""

    for source in candidates:
        outcome = run_one(source)
        counts[{"no_op": "unchanged"}.get(outcome.status, outcome.status)] += 1
        if outcome.status in {"blocked", "failed", "pending_retry"}:
            attention_required.append({
                "source_type": source.source_type,
                "source_id": source.source_id,
                "status": outcome.status,
                "reason": outcome.reason,
                "next_action": _NEXT_ACTION[outcome.status],
            })


def _add_record_command_args(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("record_id")
    subparser.add_argument("--artifacts", type=Path)


def _add_storage_args(subparser: argparse.ArgumentParser, *, database: bool = True) -> None:
    subparser.add_argument("--artifacts", type=Path)
    if database:
        subparser.add_argument("--database", type=Path)


def _add_scope_args(subparser: argparse.ArgumentParser) -> None:
    # Deliberately CLI/env-only — never derived from the query/prompt text
    # itself (ADR-0004). Unset means "fall back to MEMORY_PROJECT_ID /
    # MEMORY_GLOBAL_SCOPE" (RetrievalScope.from_env()).
    subparser.add_argument("--project-id")
    subparser.add_argument("--global-scope", action="store_true", default=None)


def _scope_from_args(args: argparse.Namespace, resolved: ResolvedAppConfig) -> RetrievalScope:
    if args.project_id is None and args.global_scope is None:
        return RetrievalScope(
            project_id=resolved.project_id,
            global_scope=env_value("GLOBAL_SCOPE", "").lower() == "true",
        )
    return RetrievalScope(project_id=args.project_id, global_scope=bool(args.global_scope))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="memento")
    result.add_argument("--config", type=Path, help="TOML config path (default: ~/.config/memento/config.toml)")
    commands = result.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest")
    _add_storage_args(ingest)
    markdown = commands.add_parser("import-markdown-kb")
    markdown.add_argument("wiki_dir", type=Path, nargs="?")
    markdown.add_argument("--knowledge-base-id", required=True)
    markdown.add_argument("--project-id")
    markdown.add_argument("--project-root")
    markdown.add_argument("--operator-id")
    markdown.add_argument("--temporal-scope", choices=["durable", "time_sensitive"], default="time_sensitive")
    _add_storage_args(markdown, database=False)
    extract = commands.add_parser("extract-session")
    extract.add_argument("transcript", type=Path)
    _add_storage_args(extract, database=False)
    extract.add_argument("--extractor", choices=["cursor"])
    extract.add_argument("--cursor-mode", choices=["ask", "plan"])
    extract.add_argument("--cursor-model")
    extract.add_argument("--operator-id")
    extract.add_argument("--project-id")
    extract.add_argument("--project-root")
    extract.add_argument("--max-sanitized-chars", type=int)
    extract_raw = commands.add_parser("extract-raw-source")
    extract_raw.add_argument("document", type=Path)
    _add_storage_args(extract_raw, database=False)
    extract_raw.add_argument("--cursor-mode", choices=["ask", "plan"])
    extract_raw.add_argument("--cursor-model")
    extract_raw.add_argument("--operator-id")
    extract_raw.add_argument("--project-id")
    extract_raw.add_argument("--project-root")
    extract_raw.add_argument("--max-sanitized-chars", type=int)
    import_raw = commands.add_parser("import-raw-sources")
    import_raw.add_argument("raw_dir", type=Path, nargs="?")
    import_raw.add_argument("--knowledge-base-id", required=True)
    import_raw.add_argument("--project-id")
    import_raw.add_argument("--project-root")
    import_raw.add_argument("--operator-id")
    import_raw.add_argument("--cursor-mode", choices=["ask", "plan"])
    import_raw.add_argument("--cursor-model")
    import_raw.add_argument("--max-sanitized-chars", type=int)
    import_raw.add_argument("--dry-run", action="store_true")
    import_raw.add_argument("--resume", action="store_true", help="Retry only previously failed or blocked documents")
    _add_storage_args(import_raw)
    import_url = commands.add_parser("import-url", help="Import Confluence Cloud pages by URL")
    import_url.add_argument("urls", nargs="+", help="One or more Confluence page URLs")
    import_url.add_argument("--atlassian-email", help="Also read as MEMENTO_ATLASSIAN_EMAIL")
    import_url.add_argument("--project-id")
    import_url.add_argument("--project-root")
    import_url.add_argument("--operator-id")
    import_url.add_argument("--cursor-mode", choices=["ask", "plan"])
    import_url.add_argument("--cursor-model")
    import_url.add_argument("--max-sanitized-chars", type=int)
    import_url.add_argument("--dry-run", action="store_true")
    import_url.add_argument("--resume", action="store_true", help="Retry only previously failed or blocked pages")
    _add_storage_args(import_url)
    search_cmd = commands.add_parser("search")
    search_cmd.add_argument("query")
    _add_storage_args(search_cmd)
    _add_scope_args(search_cmd)
    hook = commands.add_parser("hook")
    _add_storage_args(hook)
    _add_scope_args(hook)
    _add_record_command_args(commands.add_parser("verify"))
    _add_record_command_args(commands.add_parser("reject"))
    supersede_cmd = commands.add_parser("supersede")
    _add_record_command_args(supersede_cmd)
    supersede_cmd.add_argument("replacement_id")
    _add_record_command_args(commands.add_parser("history"))
    duplicates = commands.add_parser("duplicates")
    duplicates.add_argument("--project-id")
    duplicates.add_argument("--artifacts", type=Path)
    health = commands.add_parser("health-check")
    health.add_argument("--project-id", required=True)
    health.add_argument("--ai", action="store_true", help="Send structured Episode Records to Cursor for synthesis")
    health.add_argument("--stale-days", type=int, default=90)
    health.add_argument("--artifacts", type=Path)
    forget_cmd = commands.add_parser("forget")
    forget_cmd.add_argument("source_id", nargs="?")
    forget_cmd.add_argument("--project")
    _add_storage_args(forget_cmd)
    capture = commands.add_parser("capture")
    capture.add_argument("--latest", action="store_true", required=True)
    capture.add_argument("--project-id")
    capture.add_argument("--project-root")
    batch = commands.add_parser("import-sessions")
    batch.add_argument("--source", choices=["claude", "cursor"], default="claude")
    project_selection = batch.add_mutually_exclusive_group()
    project_selection.add_argument("--project")
    project_selection.add_argument("--all-projects", action="store_true")
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--since", help="Only sessions updated on/after YYYY-MM-DD")
    batch.add_argument("--resume", action="store_true", help="Retry only previously failed or blocked sessions")
    batch.add_argument("--cursor-database", type=Path)
    report_cmd = commands.add_parser("report")
    report_cmd.add_argument("--artifacts", type=Path)
    report_cmd.add_argument("--json", action="store_true")
    report_cmd.add_argument("--never-retrieved-days", type=int, default=14)
    config_cmd = commands.add_parser("config")
    config_commands = config_cmd.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show")
    config_commands.add_parser("current")
    add_project_command = config_commands.add_parser("add-project")
    add_project_command.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    add_project_command.add_argument("--id", dest="project_id")
    return result


def _explicit_config(args: argparse.Namespace) -> dict[str, object]:
    names = (
        "artifacts",
        "database",
        "operator_id",
        "project_id",
        "project_root",
        "cursor_mode",
        "cursor_model",
        "max_sanitized_chars",
    )
    values = {name: getattr(args, name, None) for name in names}
    values["extractor_provider"] = getattr(args, "extractor", None)
    return values


def _require(value, name: str):
    if value is None:
        raise ConfigError(
            f"{name} is required; pass --{name.replace('_', '-')} or configure it in "
            "~/.config/memory/config.toml"
        )
    return value


def _global_config_display(app_config, resolved: ResolvedAppConfig) -> dict:
    return {
        "config_path": str(resolved.config_path) if resolved.config_path else None,
        "operator_id": resolved.operator_id,
        "artifacts": str(resolved.artifacts) if resolved.artifacts else None,
        "database": str(resolved.database) if resolved.database else None,
        "extractor": {
            "provider": resolved.extractor_provider,
            "mode": resolved.cursor_mode,
            "model": resolved.cursor_model,
            "max_sanitized_chars": resolved.max_sanitized_chars,
        },
        "projects": {
            project_id: {
                "root": str(project.root),
                "knowledge_base": str(project.knowledge_base) if project.knowledge_base else None,
                **({"discovered": True} if project.discovered else {}),
                **({"worktrees": [str(path) for path in project.worktrees]} if project.worktrees else {}),
            }
            for project_id, project in app_config.projects.items()
        },
    }


def _current_config_display(resolved: ResolvedAppConfig) -> dict:
    return {
        "config_path": str(resolved.config_path) if resolved.config_path else None,
        "project": {
            "id": resolved.project_id,
            "root": str(resolved.project_root) if resolved.project_root else None,
            "knowledge_base": str(resolved.knowledge_base) if resolved.knowledge_base else None,
        },
    }


def _latest_claude_transcript(project_root: Path) -> Path:
    claude_home = Path(os.getenv("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    encoded_project = re.sub(r"[^A-Za-z0-9_-]", "-", str(project_root.resolve()))
    transcript_dir = claude_home / "projects" / encoded_project
    transcripts = list(transcript_dir.glob("*.jsonl")) if transcript_dir.is_dir() else []
    if not transcripts:
        raise ConfigError(f"no Claude transcripts found for {project_root} in {transcript_dir}")
    return max(transcripts, key=lambda transcript: transcript.stat().st_mtime_ns)


def _capture_project(app_config, resolved: ResolvedAppConfig):
    if not resolved.project_id:
        raise ConfigError("capture requires a project selected by the current directory or --project-id")
    registered = app_config.projects.get(resolved.project_id)
    if registered is None:
        raise ConfigError(
            f"project {resolved.project_id!r} is not registered in the config; "
            "capture will not infer transcript provenance from an unregistered environment value"
        )
    if resolved.project_root is None or resolved.project_root.resolve() != registered.root.resolve():
        raise ConfigError(
            f"resolved root for project {resolved.project_id!r} does not match its registered config root "
            f"{registered.root}"
        )
    return registered


def _forget_sources(artifacts_root: Path, database: Path, source_ids: list[str], *, project_id: str | None = None) -> int:
    """Run the full forget sequence (artifact, overlay, trace, index) for
    each source — shared by single-source and project-wide forget so the
    erasure guarantee is enforced identically either way.

    LanceDB only ever holds rows from a source's *active* revision, so its
    rows are purged only when this source's active revision was actually
    removed — never unconditionally by source_id alone. Otherwise, for a
    source whose revision history spans more than one project, forgetting
    one project could hard-delete another project's still-active, currently
    indexed rows for that same source_id — an isolation violation."""

    total = 0
    for source_id in source_ids:
        record_ids, active_revision_removed = forget_source(artifacts_root, source_id, project_id=project_id)
        forget_records(artifacts_root, record_ids)
        purge_traces(artifacts_root, set(record_ids))
        if project_id is None or active_revision_removed:
            delete_by_source_id(database, source_id)
        total += len(record_ids)
    return total


def _index_active_records(artifacts: Path, database: Path, embedder: Embedder) -> tuple[int, int, int]:
    records = load_active_episode_records(artifacts)
    threshold = float(env_value("SEMANTIC_DUPLICATE_THRESHOLD", str(DEFAULT_SEMANTIC_DUPLICATE_THRESHOLD)))
    deduplication = reconcile_duplicates(artifacts, records, embedder, semantic_threshold=threshold)
    count = index_episode_records(database, filter_retrievable(artifacts, records), embedder)
    return count, deduplication.exact_duplicates, deduplication.possible_duplicates


def run(
    arguments: list[str] | None = None,
    embedder: Embedder | None = None,
    extractor: KnowledgeExtractor | None = None,
    health_checker: HealthChecker | None = None,
    url_fetcher=None,
) -> int:
    args = parser().parse_args(arguments)
    try:
        app_config = load_app_config(args.config)
        resolved = resolve_app_config(app_config, cwd=Path.cwd(), explicit=_explicit_config(args))
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 3

    if args.command == "config":
        if args.config_command == "add-project":
            config_path = app_config.path or default_config_path()
            try:
                project_id, root, added = add_project(config_path, args.path, args.project_id)
            except ConfigError as error:
                print(f"configuration error: {error}", file=sys.stderr)
                return 3
            print(json.dumps({
                "status": "added" if added else "already_registered",
                "project_id": project_id,
                "root": str(root),
                "config_path": str(config_path),
            }, indent=2))
            return 0
        display = (
            _global_config_display(app_config, resolved)
            if args.config_command == "show"
            else _current_config_display(resolved)
        )
        print(json.dumps(display, indent=2))
        return 0

    try:
        artifacts = _require(resolved.artifacts, "artifacts")
        database = (
            _require(resolved.database, "database")
            if args.command in {"ingest", "search", "hook", "forget", "capture", "import-sessions"}
            else resolved.database
        )
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 3

    if args.command == "ingest":
        selected_embedder = embedder or FastEmbedder()
        count, exact, possible = _index_active_records(artifacts, database, selected_embedder)
        print(f"Indexed {count} episode records in {database} ({exact} exact duplicate(s) skipped, {possible} possible duplicate(s) flagged)")
    elif args.command == "capture":
        try:
            registered_project = _capture_project(app_config, resolved)
            transcript = _latest_claude_transcript(registered_project.root)
            project = ProjectProvenance(
                project_id=resolved.project_id,
                project_root=str(registered_project.root),
            )
            selected_extractor = extractor or create_extractor(
                resolved.extractor_provider,
                cursor_mode=resolved.cursor_mode,
                cursor_model=resolved.cursor_model,
                max_sanitized_chars=resolved.max_sanitized_chars,
                operator_id=resolved.operator_id,
                project=project,
            )
        except (ConfigError, ValueError) as error:
            print(f"configuration error: {error}", file=sys.stderr)
            return 3

        outcome = run_extraction(selected_extractor, transcript, artifacts)
        if outcome.status == "blocked":
            print(f"blocked: {outcome.reason}", file=sys.stderr)
            return 2
        if outcome.status == "pending_retry":
            print(f"pending_retry: {outcome.reason}", file=sys.stderr)
            return 4
        if outcome.status == "failed":
            print(f"failed: {outcome.reason}", file=sys.stderr)
            return 1

        selected_embedder = embedder or FastEmbedder()
        indexed, exact_duplicates, possible_duplicates = _index_active_records(artifacts, database, selected_embedder)
        envelope = json.loads(outcome.artifact_path.read_text())
        print(
            json.dumps(
                {
                    "status": outcome.status,
                    "session_id": transcript.stem,
                    "records": len(envelope["episode_records"]),
                    "indexed": indexed,
                    "exact_duplicates": exact_duplicates,
                    "possible_duplicates": possible_duplicates,
                }
            )
        )
    elif args.command == "import-sessions":
        if args.source == "cursor" and (args.project or args.all_projects):
            print("configuration error: project selection applies only to Claude sessions", file=sys.stderr)
            return 3
        selected_project = args.project
        if args.source == "claude":
            if selected_project == "current":
                selected_project = resolved.project_id
                if selected_project is None:
                    print("configuration error: current directory is not inside a registered project", file=sys.stderr)
                    return 3
            if not args.all_projects and selected_project is None:
                print("configuration error: Claude import requires --project ID, --project current, or --all-projects", file=sys.stderr)
                return 3
            if selected_project and selected_project not in app_config.projects:
                print(f"configuration error: project {selected_project!r} is not registered", file=sys.stderr)
                return 3
        try:
            cutoff = parse_since(args.since)
        except ValueError:
            print("configuration error: --since must be an ISO date such as 2026-09-01", file=sys.stderr)
            return 3
        sources = []
        if args.source == "claude":
            claude_home = Path(os.getenv("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
            sources.extend(claude_sessions(app_config.projects, claude_home, selected_project))
        cursor_temporary = None
        if args.source == "cursor":
            cursor_db = args.cursor_database or Path.home() / "Library/Application Support/Cursor/User/globalStorage/conversation-search.db"
            # Normalized Cursor rows are transient extraction input. The
            # immutable artifact preserves cited evidence; retaining another
            # plaintext copy would make `forget` incomplete.
            cursor_temporary = tempfile.TemporaryDirectory()
            cursor_output = Path(cursor_temporary.name)
            sources.extend(cursor_sessions(cursor_db, cursor_output))
        if cutoff is not None:
            sources = [source for source in sources if source.updated_at is None or source.updated_at >= cutoff]

        candidates, counts, attention_required = _select_batch_candidates(sources, artifacts, args.resume)
        if not args.dry_run:
            def run_one(source: SessionSource):
                project = ProjectProvenance(project_id=source.project_id, project_root=str(source.project_root)) if source.project_id else None
                selected = extractor or CursorExtractor(
                    mode=resolved.cursor_mode, model=resolved.cursor_model,
                    max_sanitized_chars=resolved.max_sanitized_chars,
                    operator_id=resolved.operator_id, project=project,
                    source_type=source.source_type, source_id=source.source_id, source_uri=source.source_uri,
                )
                return run_extraction(
                    selected, source.path, artifacts,
                    source_type=source.source_type, source_id=source.source_id, source_uri=source.source_uri,
                )
            _run_batch_candidates(candidates, counts, attention_required, run_one)
            selected_embedder = embedder or FastEmbedder()
            counts["indexed"], counts["exact_duplicates"], counts["possible_duplicates"] = _index_active_records(
                artifacts, database, selected_embedder
            )
        print(json.dumps({**counts, "attention_required": attention_required}, indent=2))
        if cursor_temporary is not None:
            cursor_temporary.cleanup()
    elif args.command == "import-markdown-kb":
        wiki_dir = args.wiki_dir or resolved.knowledge_base
        project_id = args.project_id or resolved.project_id
        if wiki_dir is None or not wiki_dir.is_dir():
            print(f"not a directory: {wiki_dir}", file=sys.stderr)
            return 1
        try:
            markdown_extractor = MarkdownKnowledgeBaseExtractor(
                knowledge_base_id=args.knowledge_base_id,
                project_id=_require(project_id, "project_id"),
                project_root=str(resolved.project_root) if resolved.project_root else None,
                operator_id=resolved.operator_id,
                temporal_scope=args.temporal_scope,
            )
        except ValueError as error:
            print(f"configuration error: {error}", file=sys.stderr)
            return 3

        articles = markdown_articles(wiki_dir)
        activated = 0
        unchanged = 0
        blocked = 0
        record_count = 0
        for article in articles:
            source_id = markdown_source_id(args.knowledge_base_id, wiki_dir, article)
            outcome = run_extraction(
                markdown_extractor,
                article,
                artifacts,
                source_type="markdown_knowledge_base",
                source_id=source_id,
            )
            if outcome.status == "activated":
                activated += 1
            elif outcome.status == "no_op":
                unchanged += 1
            elif outcome.status == "blocked":
                blocked += 1
                print(f"blocked {article}: {outcome.reason}", file=sys.stderr)
            if outcome.artifact_path:
                record_count += len(json.loads(outcome.artifact_path.read_text())["episode_records"])
        print(json.dumps({"articles": len(articles), "records": record_count, "activated": activated, "unchanged": unchanged}))
        if blocked:
            return 2
    elif args.command == "extract-session":
        try:
            project = (
                ProjectProvenance(
                    project_id=resolved.project_id,
                    project_root=str(resolved.project_root) if resolved.project_root else None,
                    repository_revision=env_value("REPOSITORY_REVISION") or None,
                    working_tree_dirty=(env_value("WORKING_TREE_DIRTY", "").lower() == "true")
                    if env_value("WORKING_TREE_DIRTY")
                    else None,
                )
                if resolved.project_id
                else None
            )
            selected_extractor = extractor or create_extractor(
                resolved.extractor_provider,
                cursor_mode=resolved.cursor_mode,
                cursor_model=resolved.cursor_model,
                max_sanitized_chars=resolved.max_sanitized_chars,
                operator_id=resolved.operator_id,
                project=project,
            )
        except ValueError as error:
            print(f"configuration error: {error}", file=sys.stderr)
            return 3

        outcome = run_extraction(selected_extractor, args.transcript, artifacts)

        if outcome.status == "blocked":
            print(f"blocked: {outcome.reason}", file=sys.stderr)
            return 2
        if outcome.status == "pending_retry":
            print(f"pending_retry: {outcome.reason}", file=sys.stderr)
            return 4
        if outcome.status == "failed":
            print(f"failed: {outcome.reason}", file=sys.stderr)
            return 1

        if outcome.orphaned_questions:
            print(
                f"note: {len(outcome.orphaned_questions)} record(s) from the previous revision have no "
                "obvious counterpart in this extraction — review for verification, rejection, or supersession:",
                file=sys.stderr,
            )
            for question in outcome.orphaned_questions:
                print(f"  - {question}", file=sys.stderr)

        envelope = json.loads(outcome.artifact_path.read_text())
        print(
            json.dumps(
                {"artifact_path": str(outcome.artifact_path), "records": envelope["episode_records"]},
                indent=2,
            )
        )
    elif args.command == "extract-raw-source":
        try:
            project = (
                ProjectProvenance(project_id=resolved.project_id, project_root=str(resolved.project_root) if resolved.project_root else None)
                if resolved.project_id
                else None
            )
            raw_extractor = extractor or RawKnowledgeExtractor(
                mode=resolved.cursor_mode,
                model=resolved.cursor_model,
                max_sanitized_chars=resolved.max_sanitized_chars,
                operator_id=resolved.operator_id,
                project=project,
            )
        except ValueError as error:
            print(f"configuration error: {error}", file=sys.stderr)
            return 3

        outcome = run_extraction(raw_extractor, args.document, artifacts, source_type="raw_knowledge_source")

        if outcome.status == "blocked":
            print(f"blocked: {outcome.reason}", file=sys.stderr)
            return 2
        if outcome.status == "pending_retry":
            print(f"pending_retry: {outcome.reason}", file=sys.stderr)
            return 4
        if outcome.status == "failed":
            print(f"failed: {outcome.reason}", file=sys.stderr)
            return 1

        if outcome.orphaned_questions:
            print(
                f"note: {len(outcome.orphaned_questions)} record(s) from the previous revision have no "
                "obvious counterpart in this extraction — review for verification, rejection, or supersession:",
                file=sys.stderr,
            )
            for question in outcome.orphaned_questions:
                print(f"  - {question}", file=sys.stderr)

        envelope = json.loads(outcome.artifact_path.read_text())
        print(
            json.dumps(
                {"artifact_path": str(outcome.artifact_path), "records": envelope["episode_records"]},
                indent=2,
            )
        )
    elif args.command == "import-raw-sources":
        raw_dir = args.raw_dir or resolved.knowledge_base
        project_id = args.project_id or resolved.project_id
        if raw_dir is None or not raw_dir.is_dir():
            print(f"not a directory: {raw_dir}", file=sys.stderr)
            return 1
        project_id = _require(project_id, "project_id")
        project_root = args.project_root or (str(resolved.project_root) if resolved.project_root else None)

        documents = raw_source_documents(raw_dir)
        sources = [
            SessionSource(
                "raw_knowledge_source",
                raw_source_id(args.knowledge_base_id, raw_dir, document),
                document,
                project_id,
                Path(project_root) if project_root else None,
                updated_at=document.stat().st_mtime,
            )
            for document in documents
        ]

        candidates, counts, attention_required = _select_batch_candidates(sources, artifacts, args.resume)
        if not args.dry_run:
            try:
                raw_extractor = extractor or RawKnowledgeExtractor(
                    mode=args.cursor_mode or resolved.cursor_mode,
                    model=args.cursor_model or resolved.cursor_model,
                    max_sanitized_chars=args.max_sanitized_chars or resolved.max_sanitized_chars,
                    operator_id=args.operator_id or resolved.operator_id,
                    project=ProjectProvenance(project_id=project_id, project_root=project_root),
                )
            except ValueError as error:
                print(f"configuration error: {error}", file=sys.stderr)
                return 3

            def run_one(source: SessionSource):
                return run_extraction(
                    raw_extractor, source.path, artifacts,
                    source_type=source.source_type, source_id=source.source_id,
                )
            _run_batch_candidates(candidates, counts, attention_required, run_one)
            selected_embedder = embedder or FastEmbedder()
            counts["indexed"], counts["exact_duplicates"], counts["possible_duplicates"] = _index_active_records(
                artifacts, database, selected_embedder
            )
        print(json.dumps({**counts, "attention_required": attention_required}, indent=2))
    elif args.command == "import-url":
        project_id = args.project_id or resolved.project_id
        project_id = _require(project_id, "project_id")
        project_root = args.project_root or (str(resolved.project_root) if resolved.project_root else None)
        email = args.atlassian_email or env_value("ATLASSIAN_EMAIL", "")
        if not email:
            print("configuration error: --atlassian-email or MEMENTO_ATLASSIAN_EMAIL is required", file=sys.stderr)
            return 3
        try:
            token = read_keychain_secret(_CONFLUENCE_KEYCHAIN_SERVICE, email)
        except KeychainSecretError as error:
            print(f"configuration error: {error}", file=sys.stderr)
            return 3

        url_temporary = tempfile.TemporaryDirectory()
        sources, fetch_failures = confluence_url_sources(
            args.urls, Path(url_temporary.name),
            email=email, token=token, project_id=project_id,
            project_root=Path(project_root) if project_root else None,
            fetch=url_fetcher or fetch_confluence_page,
        )
        candidates, counts, attention_required = _select_batch_candidates(sources, artifacts, args.resume)
        for failure in fetch_failures:
            counts["failed"] += 1
            attention_required.append({
                "source_type": "confluence_page",
                "source_id": failure["url"],
                "status": "failed",
                "reason": failure["reason"],
                "next_action": "correct the URL or Confluence access, then retry the same import-url call",
            })
        if not args.dry_run:
            try:
                confluence_extractor = extractor or RawKnowledgeExtractor(
                    mode=args.cursor_mode or resolved.cursor_mode,
                    model=args.cursor_model or resolved.cursor_model,
                    max_sanitized_chars=args.max_sanitized_chars or resolved.max_sanitized_chars,
                    operator_id=args.operator_id or resolved.operator_id,
                    project=ProjectProvenance(project_id=project_id, project_root=project_root),
                    source_type="confluence_page",
                )
            except ValueError as error:
                print(f"configuration error: {error}", file=sys.stderr)
                return 3

            def run_one(source: SessionSource):
                return run_extraction(
                    confluence_extractor, source.path, artifacts,
                    source_type=source.source_type, source_id=source.source_id, source_uri=source.source_uri,
                )
            _run_batch_candidates(candidates, counts, attention_required, run_one)
            selected_embedder = embedder or FastEmbedder()
            counts["indexed"], counts["exact_duplicates"], counts["possible_duplicates"] = _index_active_records(
                artifacts, database, selected_embedder
            )
        print(json.dumps({**counts, "attention_required": attention_required}, indent=2))
        url_temporary.cleanup()
    elif args.command == "search":
        selected_embedder = embedder or FastEmbedder()
        results, _trace = retrieval_search(
            database, artifacts, args.query, selected_embedder, scope=_scope_from_args(args, resolved)
        )
        print(format_context(results) if results else "No relevant session memory found.")
    elif args.command == "hook":
        # Construction is deferred to inside handle_user_prompt's timed
        # daemon thread (embedder_factory) rather than built eagerly here —
        # otherwise model init/process startup would fall outside
        # retrieval_timeout_ms, defeating the fail-open guarantee. A
        # caller-supplied embedder (e.g. tests) bypasses the factory and is
        # used directly.
        scope = _scope_from_args(args, resolved)
        try:
            event = json.load(sys.stdin)
            result = handle_user_prompt(
                event,
                database,
                artifacts,
                embedder=embedder,
                embedder_factory=None if embedder else FastEmbedder,
                scope=scope,
            )
            print(json.dumps(result))
        except Exception:
            print("{}")
    elif args.command == "duplicates":
        review_items = []
        for record in load_active_episode_records(artifacts):
            project_id = (record.get("project") or {}).get("project_id")
            if args.project_id and project_id != args.project_id:
                continue
            state = read_state(artifacts, record["id"])
            if state["duplicate_review_status"] == "possible":
                review_items.append(
                    {
                        "record_id": record["id"],
                        "question": record["question"],
                        "project_id": project_id,
                        "source": record["source"],
                        "duplicate_of": state["duplicate_of"],
                        "reinforces": state["reinforces"],
                        "duplicate_review_status": state["duplicate_review_status"],
                    }
                )
        print(json.dumps(review_items, indent=2))
    elif args.command == "health-check":
        if args.stale_days <= 0:
            print("configuration error: --stale-days must be positive", file=sys.stderr)
            return 3
        project_records = [
            record for record in load_active_episode_records(artifacts)
            if (record.get("project") or {}).get("project_id") == args.project_id
        ]
        configured_project = app_config.projects.get(args.project_id)
        if configured_project:
            claude_home = Path(os.getenv("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
        known_revisions = {(record["source_id"], record["source_hash"]) for record in project_records}
        known_revisions.update(
            (status["source_id"], status["attempted_hash"])
            for status in job_failures_for_project(artifacts, args.project_id)
        )
        unprocessed_source_ids = sorted(
            source.source_id
            for source in (
                claude_sessions({args.project_id: configured_project}, claude_home, args.project_id)
                if configured_project else []
            )
            if (source.source_id, source_hash(source.path)) not in known_revisions
        )
        findings = deterministic_findings(
            artifacts,
            args.project_id,
            project_records,
            stale_before=datetime.now(timezone.utc) - timedelta(days=args.stale_days),
            unprocessed_source_ids=unprocessed_source_ids,
        )
        ai_status = "not_requested"
        if args.ai or health_checker is not None:
            sensitive_paths = (str(configured_project.root),) if configured_project else ()
            checker = health_checker or CursorHealthChecker(
                mode=resolved.cursor_mode, model=resolved.cursor_model, sensitive_paths=sensitive_paths
            )
            try:
                proposed = checker.analyze(args.project_id, project_records)
                findings.extend(grounded_findings(proposed, {record["id"] for record in project_records}))
                ai_status = "completed"
            except Exception as error:
                ai_status = "failed"
                print(f"AI health check failed: {error}", file=sys.stderr)
        report_path = write_report(artifacts, args.project_id, findings, ai_status=ai_status)
        rendered_findings = [finding.model_dump() for finding in findings]
        print(json.dumps({
            "report_path": str(report_path),
            "project_id": args.project_id,
            "ai_status": ai_status,
            "findings": rendered_findings,
        }, indent=2))
        return 1 if ai_status == "failed" else 0
    elif args.command in {"verify", "reject", "supersede", "history"}:
        record = find_record(artifacts, args.record_id)
        if record is None:
            print(f"no such record: {args.record_id}", file=sys.stderr)
            return 1

        if args.command == "history":
            state = read_state(artifacts, args.record_id)
            print(json.dumps({**record, **state}, indent=2))
            return 0

        verb = {"verify": "verified", "reject": "rejected", "supersede": "superseded"}[args.command]
        try:
            if args.command == "verify":
                verify(artifacts, args.record_id)
            elif args.command == "reject":
                reject(artifacts, args.record_id)
            else:
                supersede(artifacts, args.record_id, args.replacement_id)
        except (InvalidTransition, SupersedeRequiresReplacement, UnknownReplacementRecord) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"{verb} {args.record_id}")
    elif args.command == "report":
        data = generate_report(artifacts)
        if args.never_retrieved_days != 14:
            data["never_retrieved"] = never_retrieved(artifacts, min_age_days=args.never_retrieved_days)
        print(json.dumps(data, indent=2) if args.json else render_report(data))
    elif args.command == "forget":
        if bool(args.source_id) == bool(args.project):
            print("forget requires exactly one of <source-id> or --project", file=sys.stderr)
            return 1
        if args.source_id:
            source_ids = [args.source_id]
            label = f"source {args.source_id}"
            project_id = None
        else:
            source_ids = find_sources_by_project(artifacts, args.project)
            label = f"project {args.project}"
            project_id = args.project
        total_records = _forget_sources(artifacts, database, source_ids, project_id=project_id)
        # Terminal-only, one-time — never written to a file, matching the
        # erasure guarantee (no record of the deletion itself is retained).
        print(f"forgot {total_records} record(s) across {len(source_ids)} source(s) for {label}")
    return 0


def main() -> None:
    raise SystemExit(run())
