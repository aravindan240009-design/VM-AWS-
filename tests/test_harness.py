"""End-to-end tests for AWS Agent Chat backend, tools, and streaming harness."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.llm import OpenAICompatClient, StreamTurnResult, ThinkStreamSplitter, parse_fallback_tool_calls
from app.main import create_app
from app.tools import ToolError, ToolRegistry, _safe_workspace_path


class FakeBrainClient(OpenAICompatClient):
    """Deterministic fake AWS brain that invokes a tool on turn 1 and replies on turn 2."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[dict[str, Any]]] = []

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        return ["huihui-ai/Qwen3-8B-abliterated"]

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
        self.calls.append(messages)
        if len(self.calls) == 1 and tools:
            tc = [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "note.txt", "content": "hello aws"}),
                    },
                }
            ]
            yield {"type": "reasoning", "text": "Writing note first."}
            yield {
                "type": "result",
                "result": StreamTurnResult(
                    content="",
                    reasoning="Writing note first.",
                    tool_calls=tc,
                    ttft_ms=12.0,
                    total_ms=25.0,
                ),
            }
            return

        yield {"type": "token", "text": "Done "}
        yield {"type": "token", "text": "writing note.txt!"}
        yield {
            "type": "result",
            "result": StreamTurnResult(
                content="Done writing note.txt!",
                reasoning="",
                tool_calls=[],
                ttft_ms=8.0,
                total_ms=18.0,
            ),
        }


def test_think_stream_splitter() -> None:
    splitter = ThinkStreamSplitter()
    vis1, thk1 = splitter.feed("Hello <thi")
    vis2, thk2 = splitter.feed("nk>secret plan</think> world")
    vis3, thk3 = splitter.flush()
    assert vis1 + vis2 + vis3 == "Hello  world"
    assert thk1 + thk2 + thk3 == "secret plan"


def test_parse_fallback_tool_calls() -> None:
    raw = 'Let me check.\n<tool_call>{"name": "get_current_time", "arguments": {}}</tool_call>'
    cleaned, calls = parse_fallback_tool_calls(raw)
    assert cleaned == "Let me check."
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_current_time"


def test_workspace_sandbox_blocks_traversal(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    inside = _safe_workspace_path(ws, "sub/file.txt")
    assert inside == (ws / "sub" / "file.txt").resolve()
    with pytest.raises(ToolError):
        _safe_workspace_path(ws, "../outside.txt")


@pytest.mark.asyncio
async def test_tools_file_python_and_memory(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        reg = ToolRegistry(tmp_path / "ws", db)
        res_write = json.loads(await reg.execute("write_file", {"path": "a.txt", "content": "alpha\nbeta"}))
        assert res_write["ok"] is True

        res_read = await reg.execute("read_file", {"path": "a.txt"})
        assert "1: alpha" in res_read

        res_py = json.loads(await reg.execute("python_eval", {"code": "print(6 * 7)"}))
        assert res_py["returncode"] == 0
        assert res_py["stdout"].strip() == "42"

        await reg.execute("memory_save", {"topic": "region", "content": "ap-southeast-2"})
        mems = json.loads(await reg.execute("memory_search", {"query": "southeast"}))
        assert len(mems["memories"]) == 1
    finally:
        db.close()


def test_fastapi_chat_harness_end_to_end(tmp_path: Path) -> None:
    cfg = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        llm_base_url="http://127.0.0.1:8000/v1",
        llm_model="huihui-ai/Qwen3-8B-abliterated",
    )
    fake_llm = FakeBrainClient()
    app = create_app(custom_settings=cfg, custom_llm=fake_llm)

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["brain_connected"] is True

        conv = client.post("/api/conversations", json={"title": "New chat", "mode": "harness"}).json()
        conv_id = conv["id"]

        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"conversation_id": conv_id, "message": "Save note.txt please"},
        ) as resp:
            assert resp.status_code == 200
            events = [
                json.loads(line[5:].strip())
                for line in resp.iter_lines()
                if line.startswith("data:")
            ]

        types = [e["type"] for e in events]
        assert "tool_start" in types
        assert "tool_end" in types
        assert "done" in types
        assert (cfg.workspace_dir / "note.txt").read_text(encoding="utf-8") == "hello aws"

        history = client.get(f"/api/conversations/{conv_id}").json()
        assert len(history["messages"]) == 2
        assert history["messages"][1]["content"] == "Done writing note.txt!"
        assert len(history["messages"][1]["tool_traces"]) == 1
