"""OpenAI-compatible streaming client.

Reads config from environment, falls back to defaults. Streams tokens via
async generator so the server can fan out to SSE.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import AsyncIterator

import httpx

import db

CONFIG_PATH = Path(__file__).parent / ".arks.env"


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_config() -> dict[str, str]:
    """Read ARKS_* env vars, fallback to .arks.env."""
    file_cfg = _load_env_file(CONFIG_PATH)
    return {
        "base_url": os.environ.get("ARKS_BASE_URL") or file_cfg.get("ARKS_BASE_URL") or "https://api.openai.com/v1",
        "api_key": os.environ.get("ARKS_API_KEY") or file_cfg.get("ARKS_API_KEY") or "",
        "model": os.environ.get("ARKS_MODEL") or file_cfg.get("ARKS_MODEL") or "gpt-4o-mini",
    }


def cache_key(model: str, prompt: str) -> str:
    h = hashlib.sha1()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt.encode("utf-8"))
    return h.hexdigest()


class AIClient:
    def __init__(self, cfg: dict[str, str] | None = None):
        self.cfg = cfg or get_config()
        if not self.cfg["api_key"]:
            raise RuntimeError(
                "ARKS_API_KEY is not set. Set it as env var or in .arks.env"
            )

    async def stream(
        self,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        use_cache: bool = True,
    ) -> AsyncIterator[str]:
        """Yield raw text tokens. Checks LLM cache first."""
        if use_cache:
            cached = db.llm_cache_get(cache_key(self.cfg["model"], prompt))
            if cached is not None:
                cleaned = self.strip_think(cached)
                # Older versions cached a thinking-only response as blank text.
                # Treat it as a cache miss so it cannot permanently poison a
                # word or sentence lookup.
                if cleaned.strip():
                    for chunk in self._chunk_text(cleaned):
                        yield chunk
                    return

        url = f"{self.cfg['base_url'].rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.cfg['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.cfg["model"],
            "stream": True,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        is_minimax = "minimaxi.com" in self.cfg["base_url"].lower()
        if is_minimax:
            # MiniMax otherwise embeds reasoning in content as <think>...</think>.
            payload["reasoning_split"] = True
        if max_tokens is not None:
            # MiniMax's OpenAI-compatible endpoint uses this current name.
            # The former max_tokens parameter can leave all budget to <think>.
            if is_minimax:
                # M3 can consume several hundred tokens before it emits the
                # short structured answer. Keep the caller's output intent,
                # but reserve enough completion budget for that reasoning.
                payload["max_completion_tokens"] = max(max_tokens, 1024)
            else:
                payload["max_tokens"] = max_tokens

        raw_full: list[str] = []
        visible_full: list[str] = []
        think_buffer = ""
        in_think = False

        def consume(piece: str, final: bool = False) -> list[str]:
            """Remove hidden reasoning while tolerating tags split across chunks."""
            nonlocal think_buffer, in_think
            think_buffer += piece
            out: list[str] = []
            while think_buffer:
                if in_think:
                    end = think_buffer.lower().find("</think>")
                    if end < 0:
                        if not final:
                            think_buffer = think_buffer[-8:]
                        else:
                            think_buffer = ""
                        break
                    think_buffer = think_buffer[end + len("</think>"):]
                    in_think = False
                    continue
                start = think_buffer.lower().find("<think>")
                if start < 0:
                    if final:
                        out.append(think_buffer); think_buffer = ""
                    else:
                        keep = len("<think>") - 1
                        if len(think_buffer) > keep:
                            out.append(think_buffer[:-keep]); think_buffer = think_buffer[-keep:]
                    break
                out.append(think_buffer[:start])
                think_buffer = think_buffer[start + len("<think>"):]
                in_think = True
            return [x for x in out if x]
        # A streaming connection may legitimately take time, but an unlimited
        # read timeout leaves the browser waiting forever after an upstream
        # proxy drops the stream mid-response.
        timeout = httpx.Timeout(connect=20.0, read=90.0, write=30.0, pool=20.0)
        finish_reason = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        # SSE comment, skip
                        continue
                    if line.startswith("data:"):
                        data = line[5:].strip()
                    else:
                        data = line.strip()
                    if data == "[DONE]":
                        break
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("error"):
                        message = evt["error"].get("message", "unknown provider error") if isinstance(evt["error"], dict) else str(evt["error"])
                        raise RuntimeError(f"LLM provider error: {message}")
                    choices = evt.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        raw_full.append(piece)
                        for visible in consume(piece):
                            visible_full.append(visible)
                            yield visible

        if finish_reason == "length":
            raise RuntimeError("The model response reached its token limit. Please retry.")

        for visible in consume("", final=True):
            visible_full.append(visible)
            yield visible

        if not "".join(visible_full).strip():
            raise RuntimeError("The model produced no final content after reasoning. Please retry.")

        if use_cache and raw_full and "".join(visible_full).strip():
            try:
                db.llm_cache_put(
                    cache_key(self.cfg["model"], prompt),
                    "".join(visible_full),
                )
            except Exception:
                pass  # cache failure should not break the stream

    @staticmethod
    def _chunk_text(text: str, size: int = 16) -> list[str]:
        return [text[i : i + size] for i in range(0, len(text), size)]

    @staticmethod
    def strip_think(text: str) -> str:
        """Remove reasoning tags from cached or non-streamed model output."""
        return re.sub(r"<think>.*?</think>", "", text or "", flags=re.I | re.S).strip()
