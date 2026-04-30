"""
ollama_client.py — Streaming Ollama HTTP client.

- Connects to localhost:11434
- Starts ollama serve if not running
- Streams token-by-token with callback
- Unloads model VRAM on shutdown (ollama stop)
"""

from __future__ import annotations
import json
import logging
import subprocess
import sys
import time
from typing import Callable, Optional

import requests

from config import OLLAMA_BASE, OLLAMA_MODEL, OLLAMA_FALLBACK

logger = logging.getLogger(__name__)

_ollama_proc: Optional[subprocess.Popen] = None


# ── Server lifecycle ──────────────────────────────────────────────────────────

def is_ollama_running() -> bool:
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def list_models() -> list[str]:
    """Return list of available model names."""
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def ensure_ollama_running() -> bool:
    """
    Check if ollama is running. If not, start it.
    Returns True if running after this call.
    """
    global _ollama_proc

    if is_ollama_running():
        return True

    logger.info("Ollama not running — attempting to start...")
    try:
        _ollama_proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait up to 15s for startup
        for i in range(15):
            time.sleep(1)
            if is_ollama_running():
                logger.info("Ollama started (took %ds)", i + 1)
                return True
        logger.error("Ollama failed to start within 15s")
        return False
    except FileNotFoundError:
        logger.error("'ollama' not found in PATH — install from https://ollama.ai")
        return False


def stop_ollama(model: str = OLLAMA_MODEL) -> None:
    """Unload model from VRAM. Does not kill the server."""
    global _ollama_proc
    try:
        subprocess.run(
            ["ollama", "stop", model],
            capture_output=True,
            timeout=10,
        )
        logger.info("Ollama model '%s' unloaded", model)
    except Exception as e:
        logger.debug("ollama stop failed: %s", e)

    if _ollama_proc is not None:
        try:
            _ollama_proc.terminate()
        except Exception:
            pass
        _ollama_proc = None


def resolve_model(preferred: str = OLLAMA_MODEL) -> str:
    """Return preferred model if available, else fallback."""
    available = list_models()
    if not available:
        return preferred
    # Exact match first
    if preferred in available:
        return preferred
    # Prefix match (e.g. "gemma4:e2b" matches "gemma4:e2b-q4_k_s")
    for m in available:
        if m.startswith(preferred.split(":")[0]):
            return m
    # Any available
    if OLLAMA_FALLBACK in available:
        return OLLAMA_FALLBACK
    return available[0]


# ── Chat / streaming ──────────────────────────────────────────────────────────

class OllamaError(Exception):
    pass


def stream_chat(
    system_prompt: str,
    user_message: str,
    model: str = OLLAMA_MODEL,
    on_token: Optional[Callable[[str], None]] = None,
    options: Optional[dict] = None,
) -> str:
    """
    Send a chat request to Ollama with streaming.

    Args:
        system_prompt: The system instruction (OMNI-FORENSIC prompt etc.)
        user_message:  The per-pass context / user query.
        model:         Ollama model name.
        on_token:      Callback called with each content chunk as it arrives.
        options:       Optional Ollama model options (temperature, num_ctx, etc.)

    Returns:
        Full accumulated response string.

    Raises:
        OllamaError on HTTP failure.
    """
    payload = {
        "model":  model,
        "stream": True,
        "messages": [
            {"role": "system",  "content": system_prompt},
            {"role": "user",    "content": user_message},
        ],
    }
    if options:
        payload["options"] = options

    try:
        response = requests.post(
            f"{OLLAMA_BASE}/api/chat",
            json=payload,
            stream=True,
            timeout=(30, 1200),  # 30s connect, 20min read
        )
        response.raise_for_status()
    except requests.ConnectionError:
        raise OllamaError("Cannot connect to Ollama at %s — is it running?" % OLLAMA_BASE)
    except requests.HTTPError as e:
        raise OllamaError("Ollama HTTP error: %s" % e)

    accumulated = []
    first_token_time = None

    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        try:
            chunk = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        content = chunk.get("message", {}).get("content", "")
        if content:
            if first_token_time is None:
                first_token_time = time.time()
            accumulated.append(content)
            if on_token:
                on_token(content)

        if chunk.get("done"):
            break

    return "".join(accumulated)


def chat(
    system_prompt: str,
    user_message: str,
    model: str = OLLAMA_MODEL,
    options: Optional[dict] = None,
) -> str:
    """Non-streaming chat. Returns full response string."""
    payload = {
        "model":  model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
    }
    if options:
        payload["options"] = options

    try:
        r = requests.post(
            f"{OLLAMA_BASE}/api/chat",
            json=payload,
            timeout=(30, 600),
        )
        r.raise_for_status()
        data = r.json()
        return data.get("message", {}).get("content", "")
    except requests.ConnectionError:
        raise OllamaError("Cannot connect to Ollama")
    except requests.HTTPError as e:
        raise OllamaError("Ollama HTTP error: %s" % e)
