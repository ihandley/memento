from .base import KnowledgeExtractor, ProjectProvenance, StructuredRecord
from .bifrost import BifrostExtractor
from .cursor import CursorExtractor


def create_extractor(
    name: str,
    *,
    cursor_mode: str | None = None,
    cursor_model: str | None = None,
    bifrost_base_url: str | None = None,
    bifrost_model: str | None = None,
    max_sanitized_chars: int | None = None,
    timeout_seconds: int | None = None,
    operator_id: str | None = None,
    project: ProjectProvenance | None = None,
    source_type: str = "claude_session",
    source_id: str | None = None,
    source_uri: str | None = None,
) -> KnowledgeExtractor:
    if name == "cursor":
        return CursorExtractor(
            mode=cursor_mode,
            model=cursor_model,
            max_sanitized_chars=max_sanitized_chars,
            timeout_seconds=timeout_seconds,
            operator_id=operator_id,
            project=project,
            source_type=source_type,
            source_id=source_id,
            source_uri=source_uri,
        )
    if name == "bifrost":
        return BifrostExtractor(
            base_url=bifrost_base_url,
            model=bifrost_model,
            max_sanitized_chars=max_sanitized_chars,
            timeout_seconds=timeout_seconds,
            operator_id=operator_id,
            project=project,
            source_type=source_type,
            source_id=source_id,
            source_uri=source_uri,
        )
    raise ValueError(f"Unknown extractor: {name}")


__all__ = ["BifrostExtractor", "CursorExtractor", "KnowledgeExtractor", "StructuredRecord", "create_extractor"]
