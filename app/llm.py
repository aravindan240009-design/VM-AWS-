"""High-speed async OpenAI-compatible streaming LLM client."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


class BrainUnavailableError(RuntimeError):
    """Raised when the remote AWS model server is unreachable or returns an error."""


@dataclass
class StreamTurnResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    ttft_ms: float | None = None
    total_ms: float = 0.0
    completion_tokens: int = 0


class ThinkStreamSplitter:
    """Separates inline <think>...</think> spans from visible assistant text during streaming."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> tuple[str, str]:
        if not chunk:
            return "", ""
        self._buf += chunk
        visible_out: list[str] = []
        think_out: list[str] = []

        while self._buf:
            if not self._in_think:
                idx = self._buf.find("<think>")
                if idx == -1:
                    # Keep potential partial "<think>" suffix buffered.
                    hold = self._partial_suffix_len(self._buf, "<think>")
                    emit = self._buf[: len(self._buf) - hold] if hold else self._buf
                    if emit:
                        visible_out.append(emit)
                    self._buf = self._buf[len(emit) :]
                    break
                if idx > 0:
                    visible_out.append(self._buf[:idx])
                self._buf = self._buf[idx + len("<think>") :]
                self._in_think = True
            else:
                idx = self._buf.find("</think>")
                if idx == -1:
                    hold = self._partial_suffix_len(self._buf, "</think>")
                    emit = self._buf[: len(self._buf) - hold] if hold else self._buf
                    if emit:
                        think_out.append(emit)
                    self._buf = self._buf[len(emit) :]
                    break
                if idx > 0:
                    think_out.append(self._buf[:idx])
                self._buf = self._buf[idx + len("</think>") :]
                self._in_think = False

        return "".join(visible_out), "".join(think_out)

    def flush(self) -> tuple[str, str]:
        rem = self._buf
        self._buf = ""
        if self._in_think:
            return "", rem
        return rem, ""

    @staticmethod
    def _partial_suffix_len(text: str, tag: str) -> int:
        max_k = min(len(text), len(tag) - 1)
        for k in range(max_k, 0, -1):
            if text.endswith(tag[:k]):
                return k
        return 0


def parse_fallback_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract <tool_call>{name, arguments}</tool_call> tags if model emitted XML-wrapped calls."""
    found: list[dict[str, Any]] = []

    def _replace(match: re.Match[str]) -> str:
        raw_json = match.group(1)
        try:
            payload = json.loads(raw_json)
            name = payload.get("name")
            args = payload.get("arguments", {})
            if isinstance(name, str) and name:
                found.append(
                    {
                        "id": f"call_{uuid.uuid4().hex[:10]}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": args if isinstance(args, str) else json.dumps(args),
                        },
                    }
                )
                return ""
        except ValueError:
            pass
        return match.group(0)

    cleaned = _TOOL_CALL_TAG.sub(_replace, text).strip()
    return cleaned, found


class OpenAICompatClient:
    """Async streaming client for vLLM / Ollama / OpenAI-compatible AWS endpoints."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._external_client = http_client
        self._owned_client: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=10.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
        return self._owned_client

    async def close(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        return headers

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        url = base_url.rstrip("/") + "/models"
        client = await self._client()
        try:
            resp = await client.get(url, headers=self._headers(api_key), timeout=6.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise BrainUnavailableError(f"Cannot reach model endpoint {url}: {exc}") from exc
        rows = data.get("data", [])
        return [row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]

    async def stream_chat(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        fast_no_think: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yields token/reasoning deltas and finally a 'result' event containing StreamTurnResult."""
        url = base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if fast_no_think:
            # Passed cleanly by vLLM/SGLang Qwen3 chat templates; ignored safely if unused.
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        client = await self._client()
        started = time.perf_counter()
        ttft_ms: float | None = None
        splitter = ThinkStreamSplitter()

        visible_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_accum: dict[int, dict[str, Any]] = {}
        completion_tokens = 0

        try:
            async with client.stream(
                "POST",
                url,
                headers=self._headers(api_key),
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    raise BrainUnavailableError(
                        f"AWS model endpoint returned HTTP {resp.status_code}: {body}"
                    )

                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except ValueError:
                        continue

                    usage = chunk.get("usage")
                    if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
                        completion_tokens = usage["completion_tokens"]

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # Explicit reasoning field (vLLM / DeepSeek / Qwen3)
                    r_delta = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if r_delta:
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000.0
                        reasoning_parts.append(r_delta)
                        yield {"type": "reasoning", "text": r_delta}

                    # Content delta (possibly containing <think>...</think>)
                    c_delta = delta.get("content") or ""
                    if c_delta:
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000.0
                        vis, thk = splitter.feed(c_delta)
                        if thk:
                            reasoning_parts.append(thk)
                            yield {"type": "reasoning", "text": thk}
                        if vis:
                            visible_parts.append(vis)
                            yield {"type": "token", "text": vis}

                    # Native OpenAI tool_calls delta
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", 0))
                        entry = tool_accum.setdefault(
                            idx,
                            {
                                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:10]}",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            entry["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            entry["function"]["arguments"] += fn["arguments"]

        except BrainUnavailableError:
            raise
        except httpx.HTTPError as exc:
            raise BrainUnavailableError(f"Connection to {url} failed: {exc}") from exc

        rem_vis, rem_thk = splitter.flush()
        if rem_thk:
            reasoning_parts.append(rem_thk)
            yield {"type": "reasoning", "text": rem_thk}
        if rem_vis:
            visible_parts.append(rem_vis)
            yield {"type": "token", "text": rem_vis}

        full_text = "".join(visible_parts)
        tool_calls = [tool_accum[i] for i in sorted(tool_accum)]
        if not tool_calls and "<tool_call>" in full_text:
            full_text, tool_calls = parse_fallback_tool_calls(full_text)

        total_ms = (time.perf_counter() - started) * 1000.0
        yield {
            "type": "result",
            "result": StreamTurnResult(
                content=full_text.strip(),
                reasoning="".join(reasoning_parts).strip(),
                tool_calls=tool_calls,
                ttft_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
                total_ms=round(total_ms, 1),
                completion_tokens=completion_tokens,
            ),
        }
