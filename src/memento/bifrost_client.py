from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable

from .extractors.base import EngineConfigurationError, ExtractionPendingRetry
from .secrets import KeychainSecretError

BIFROST_KEYCHAIN_SERVICE = "memento-bifrost-api-key"

Transport = Callable[[str, dict[str, str], dict, int], tuple[int, str]]


def urllib_transport(url: str, headers: dict[str, str], body: dict, timeout: int) -> tuple[int, str]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        raise ExtractionPendingRetry(f"Bifrost unavailable: {type(error).__name__}: {error}") from error


def chat_completions_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/v1/chat/completions"


def complete_chat(
    prompt: str,
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: int,
    transport: Transport = urllib_transport,
) -> dict:
    """One OpenAI-compatible chat completion. The model string is sent unchanged."""

    return _json_object(_post_chat(
        prompt, base_url=base_url, model=model, api_key=api_key, timeout=timeout, transport=transport,
    ))


def probe_chat(
    prompt: str,
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: int,
    transport: Transport = urllib_transport,
) -> str:
    """One chat completion whose only success condition is a non-empty reply."""

    content = _post_chat(
        prompt, base_url=base_url, model=model, api_key=api_key, timeout=timeout, transport=transport,
    ).strip()
    if not content:
        raise ValueError("Bifrost completion was empty")
    return content


def _post_chat(
    prompt: str,
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: int,
    transport: Transport,
) -> str:
    url = chat_completions_url(base_url)
    body = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        status, text = transport(url, headers, body, timeout)
    except ExtractionPendingRetry:
        raise
    except (TimeoutError, OSError) as error:
        raise ExtractionPendingRetry(f"Bifrost unavailable: {type(error).__name__}: {error}") from error
    if status in {401, 403}:
        raise EngineConfigurationError(f"Bifrost refused the call: HTTP {status}")
    if status == 429 or status >= 500:
        raise ExtractionPendingRetry(f"Bifrost unavailable: HTTP {status}")
    if status != 200:
        raise ValueError(f"Bifrost returned HTTP {status}")
    return _message_content(text)


def resolve_api_key(
    api_key: str | None,
    base_url: str,
    read_secret: Callable[[str, str], str],
) -> str:
    if api_key:
        return api_key
    try:
        return read_secret(BIFROST_KEYCHAIN_SERVICE, base_url)
    except KeychainSecretError as error:
        raise EngineConfigurationError(f"Bifrost Keychain credential is unavailable: {error}") from error


def _message_content(text: str) -> str:
    try:
        payload = json.loads(text)
        content = payload["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise ValueError("Bifrost completion was not a chat response") from error
    if not isinstance(content, str):
        raise ValueError("Bifrost completion content must be text")
    return content


def _json_object(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ValueError("Bifrost completion was not JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Bifrost completion must be a JSON object")
    return value
