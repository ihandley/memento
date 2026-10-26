from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from .base import ExtractedKnowledge, ExtractionError, ExtractionPendingRetry, ExtractionResult
from ..cursor_client import run_cursor_json

Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_TIMEOUT_SECONDS = 120


def run_extraction_with_retries(
    prompt: str,
    *,
    executable: str,
    runner: Runner,
    workspace: Path,
    mode: str,
    model: str,
    max_output_retries: int,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[ExtractedKnowledge]:
    """Call Cursor and parse its response, retrying only invalid output a
    bounded number of times. Shared by every Cursor-backed extractor
    (session, RAW knowledge source, ...) so retry/error-classification
    semantics can't drift between them.

    Only genuine subprocess-level infra failures (timeout, nonzero exit,
    Cursor binary unavailable) raise ExtractionPendingRetry immediately,
    without in-process retry — that's a distinct, externally-retried job
    status. A non-success envelope is NOT assumed to be infra-level (we have
    no evidence for what subtypes Cursor actually reports for a bad request
    vs. real unavailability), so it's treated the same as malformed output:
    retried, then failed.
    """

    last_error: Exception | None = None
    for _ in range(max_output_retries + 1):
        try:
            value = run_cursor_json(
                prompt, executable=executable, runner=runner, workspace=workspace,
                mode=mode, model=model, timeout=timeout,
            )
            return ExtractionResult.model_validate(value).records
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as error:
            raise ExtractionPendingRetry(f"Cursor unavailable: {type(error).__name__}: {error}") from error
        except Exception as error:
            last_error = error
    raise ExtractionError(
        f"Cursor returned invalid output after {max_output_retries + 1} attempt(s): "
        f"{type(last_error).__name__}"
    ) from last_error
