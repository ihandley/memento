from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .confluence import ConfluenceFetchError, ConfluenceURLError, confluence_source_id, fetch_confluence_page, resolve_confluence_url


@dataclass(frozen=True)
class SessionSource:
    source_type: str
    source_id: str
    path: Path
    project_id: str | None = None
    project_root: Path | None = None
    source_uri: str | None = None
    updated_at: float | None = None


def _encode_claude_path(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(path.expanduser().resolve()))


def claude_sessions(projects: dict, claude_home: Path, project_id: str | None = None) -> list[SessionSource]:
    selected = {project_id: projects[project_id]} if project_id else projects
    transcripts_root = claude_home / "projects"
    found: list[SessionSource] = []
    for identifier, project in selected.items():
        # Linked worktrees have their own transcript directories; Claude Code's
        # own worktrees live under `<repo>/.claude/worktrees/<name>`, which
        # encodes to `<repo>--claude-worktrees-<name>`.
        locations: list[tuple[Path, Path]] = []
        for checkout in (project.root, *getattr(project, "worktrees", ())):
            encoded = _encode_claude_path(checkout)
            locations.append((transcripts_root / encoded, checkout))
            for claude_worktree in sorted(transcripts_root.glob(f"{encoded}--claude-worktrees-*")):
                locations.append((claude_worktree, project.root))
        for directory, root in locations:
            for path in directory.glob("*.jsonl"):
                found.append(SessionSource("claude_session", path.stem, path, identifier, root, updated_at=path.stat().st_mtime))
    return sorted(found, key=lambda item: item.path.stat().st_mtime_ns)


def cursor_sessions(database: Path, output_dir: Path) -> list[SessionSource]:
    """Read Cursor's local conversation search index through a SQLite snapshot.

    Only local rows are imported; cloud-cache rows can duplicate them. Cursor's
    root fingerprint is intentionally not interpreted as project provenance.
    """
    if not database.exists():
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / "cursor.db"
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
        with sqlite3.connect(snapshot) as connection:
            rows = connection.execute(
                "SELECT c.id, c.title, f.body, c.updated_at FROM conversations c "
                "JOIN conversation_fts f ON f.rowid = c.fts_rowid WHERE c.source = 'local' ORDER BY c.updated_at"
            ).fetchall()
    result = []
    for identifier, title, body, updated_at in rows:
        safe_identifier = hashlib.sha256(identifier.encode()).hexdigest()
        path = output_dir / f"{safe_identifier}.jsonl"
        payload = {"type": "user", "uuid": identifier, "message": {"content": f"{title}\n\n{body}"}}
        path.write_text(json.dumps(payload) + "\n")
        timestamp = float(updated_at)
        if timestamp > 100_000_000_000:  # Cursor stores Unix milliseconds.
            timestamp /= 1000
        result.append(SessionSource("cursor_session", identifier, path, source_uri=f"cursor://conversation/{identifier}", updated_at=timestamp))
    return result


def confluence_url_sources(
    urls: list[str],
    output_dir: Path,
    *,
    email: str,
    token: str,
    project_id: str | None,
    project_root: Path | None,
    fetch=fetch_confluence_page,
) -> tuple[list[SessionSource], list[dict]]:
    """Fetch each URL fresh (one cheap REST call) and cache its converted
    text locally so `source_hash` can detect real content changes across
    runs — mirrors `cursor_sessions`' fresh-snapshot-then-hash approach
    rather than a separately-invalidated on-disk cache.

    A URL that fails to parse or fetch is skipped, not fatal to the batch;
    the second return value lists what failed and why so the caller can
    surface it the same way a blocked/failed extraction is surfaced.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    sources: list[SessionSource] = []
    fetch_failures: list[dict] = []
    for url in urls:
        try:
            base_url, page_id = resolve_confluence_url(url, email=email, token=token)
            page = fetch(base_url, page_id, email=email, token=token)
        except (ConfluenceURLError, ConfluenceFetchError) as error:
            fetch_failures.append({"url": url, "reason": str(error)})
            continue
        source_id = confluence_source_id(base_url, page_id)
        path = output_dir / f"{source_id}.md"
        path.write_text(f"# {page.title}\n\n{page.text}\n")
        sources.append(SessionSource("confluence_page", source_id, path, project_id, project_root, source_uri=page.url))
    return sources, fetch_failures


def parse_since(value: str | None) -> float | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).timestamp()
