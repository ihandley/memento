from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from ..bifrost_client import Transport, complete_chat, resolve_api_key, urllib_transport
from ..secrets import read_keychain_secret


def _default_read_secret(service: str, account: str) -> str:
    return read_keychain_secret(service, account)
from .base import (
    EngineConfigurationError,
    ExtractedKnowledge,
    ExtractionBlocked,
    ExtractionError,
    ExtractionPendingRetry,
    ExtractionResult,
    ProjectProvenance,
    StructuredRecord,
)
from .cursor import (
    DEFAULT_MAX_OUTPUT_RETRIES,
    DEFAULT_PROMPT_VERSION,
    CursorExtractor,
    _configured,
    _person_attribution,
    _project_from_environment,
    _resolved_evidence_location,
)
from .cursor_runner import DEFAULT_TIMEOUT_SECONDS
from ..sanitize import DEFAULT_MAX_SANITIZED_CHARS, SanitizationBudgetExceeded, sanitize_session


def run_bifrost_with_retries(
    prompt: str,
    *,
    complete: Callable[[str], dict],
    max_output_retries: int,
) -> list[ExtractedKnowledge]:
    """Retry only invalid model output. Transport and auth failures propagate immediately."""

    last_error: Exception | None = None
    for _ in range(max_output_retries + 1):
        try:
            value = complete(prompt)
            return ExtractionResult.model_validate(value).records
        except (ExtractionPendingRetry, EngineConfigurationError):
            raise
        except Exception as error:
            last_error = error
    raise ExtractionError(
        f"Bifrost returned invalid output after {max_output_retries + 1} attempt(s): "
        f"{type(last_error).__name__}"
    ) from last_error


class BifrostExtractor:
    name = "bifrost"

    def __init__(
        self,
        *,
        base_url: str | None,
        model: str | None,
        api_key: str | None = None,
        transport: Transport | None = None,
        read_secret: Callable[[str, str], str] | None = None,
        sensitive_paths: tuple[str, ...] | None = None,
        max_sanitized_chars: int | None = None,
        timeout_seconds: int | None = None,
        operator_id: str | None = None,
        project: ProjectProvenance | None = None,
        prompt_version: int | None = None,
        max_output_retries: int | None = None,
        source_type: str = "claude_session",
        source_id: str | None = None,
        source_uri: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model or ""
        self._api_key = api_key
        self._transport = transport
        self._read_secret = read_secret or _default_read_secret
        if sensitive_paths is not None:
            self._sensitive_paths = sensitive_paths
        else:
            configured_paths = _configured(None, "MEMENTO_SENSITIVE_PATHS", "")
            self._sensitive_paths = tuple(path for path in configured_paths.split(":") if path)
        self._max_sanitized_chars = max_sanitized_chars or int(
            _configured(None, "MEMENTO_MAX_SANITIZED_CHARS", str(DEFAULT_MAX_SANITIZED_CHARS))
        )
        self._timeout_seconds = timeout_seconds or int(
            _configured(None, "MEMENTO_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        )
        self._operator_id = _configured(operator_id, "MEMENTO_OPERATOR_ID", "")
        if not self._operator_id:
            raise ValueError(
                "operator_id must be configured explicitly (constructor arg or "
                "MEMORY_OPERATOR_ID) — it is never inferred from Git identity"
            )
        self._project = project if project is not None else _project_from_environment()
        self._prompt_version = prompt_version or int(
            _configured(None, "MEMENTO_PROMPT_VERSION", str(DEFAULT_PROMPT_VERSION))
        )
        self._max_output_retries = (
            max_output_retries if max_output_retries is not None else int(
                _configured(None, "MEMENTO_MAX_OUTPUT_RETRIES", str(DEFAULT_MAX_OUTPUT_RETRIES))
            )
        )
        self._source_type = source_type
        self._source_id = source_id
        self._source_uri = source_uri

    @property
    def model(self) -> str:
        return self._model

    @property
    def prompt_version(self) -> int:
        return self._prompt_version

    @property
    def project_id(self) -> str | None:
        return self._project.project_id if self._project else None

    def extract(self, transcript: Path) -> list[StructuredRecord]:
        self._require_endpoint()
        try:
            sanitized = sanitize_session(
                transcript,
                sensitive_paths=self._sensitive_paths,
                max_chars=self._max_sanitized_chars,
            )
        except SanitizationBudgetExceeded as error:
            raise ExtractionBlocked(str(error)) from error
        prompt = CursorExtractor._prompt(sanitized.prompt_text)
        drafts = run_bifrost_with_retries(
            prompt, complete=self._complete, max_output_retries=self._max_output_retries
        )
        try:
            return [
                StructuredRecord(
                    **{
                        **draft.model_dump(),
                        "attribution": _person_attribution(draft.attribution),
                        "evidence_location": _resolved_evidence_location(draft.evidence_location, sanitized),
                    },
                    source=self._source_uri or str(transcript.resolve()),
                    source_session_id=self._source_id or transcript.stem,
                    source_type=self._source_type,
                    operator_id=self._operator_id,
                    project=self._project,
                    prompt_version=self._prompt_version,
                )
                for draft in drafts
            ]
        except ValidationError as error:
            raise ExtractionError(f"Extracted record failed trusted provenance validation: {error}") from error

    def _require_endpoint(self) -> None:
        if not self._base_url:
            raise EngineConfigurationError("extractor.bifrost.base_url is required when provider is bifrost")
        if not self._model:
            raise EngineConfigurationError("extractor.bifrost.model is required when provider is bifrost")

    def _complete(self, prompt: str) -> dict:
        assert self._base_url is not None
        api_key = resolve_api_key(self._api_key, self._base_url, self._read_secret)
        return complete_chat(
            prompt,
            base_url=self._base_url,
            model=self._model,
            api_key=api_key,
            timeout=self._timeout_seconds,
            transport=self._transport or urllib_transport,
        )
