"""Minimal client for an OpenAI-compatible chat endpoint (LM Studio on Axiom), used by /ask.

Settings come from the environment: AUTODIDACT_LLM_URL (e.g. http://axiom.phfactor.net:1234/v1) and
AUTODIDACT_CHAT_MODEL (e.g. openai/gpt-oss-120b; empty = the first loaded non-embedding model).
Without AUTODIDACT_LLM_URL, /ask answers with sources only.
"""

import json
import os
import urllib.request


def configured() -> bool:
    return bool(os.environ.get("AUTODIDACT_LLM_URL"))


def _url(path: str) -> str:
    return os.environ.get("AUTODIDACT_LLM_URL", "http://localhost:1234/v1").rstrip("/") + path


def model() -> str:
    name = os.environ.get("AUTODIDACT_CHAT_MODEL", "")
    if name:
        return name
    with urllib.request.urlopen(_url("/models"), timeout=10) as r:
        ids = [m["id"] for m in json.load(r)["data"] if "embed" not in m["id"].lower()]
    if not ids:
        raise RuntimeError("no chat model loaded")
    return ids[0]


def chat(messages, max_tokens: int = 1200, timeout: float = 90, temperature: float = 0.2) -> tuple[str, str]:
    """(answer text, model id). Raises on HTTP or connection errors."""
    name = model()
    payload = {"model": name, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
               # gpt-oss reasons before answering; low effort keeps an answer to a few seconds.
               "reasoning_effort": "low"}
    req = urllib.request.Request(_url("/chat/completions"), data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    return (resp["choices"][0]["message"].get("content") or "").strip(), name
