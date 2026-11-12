"""FastAPI entrypoint for AWS Agent Chat (ChatGPT-style UI + Agent Harness)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import BASE_DIR, Settings, settings
from app.db import Database
from app.harness import AgentHarness
from app.llm import BrainUnavailableError, OpenAICompatClient
from app.tools import ToolRegistry

STATIC_DIR = BASE_DIR / "static"


def _effective_config(base: Settings, db: Database) -> dict[str, Any]:
    overrides = db.get_runtime_settings()
    return {
        "llm_base_url": overrides.get("llm_base_url", base.llm_base_url),
        "llm_api_key": overrides.get("llm_api_key", base.llm_api_key),
        "llm_model": overrides.get("llm_model", base.llm_model),
        "max_agent_steps": int(overrides.get("max_agent_steps", base.max_agent_steps)),
        "enable_shell_tool": bool(overrides.get("enable_shell_tool", base.enable_shell_tool)),
        "enable_python_tool": bool(overrides.get("enable_python_tool", base.enable_python_tool)),
        "enable_web_tools": bool(overrides.get("enable_web_tools", base.enable_web_tools)),
        "fast_no_think_default": bool(overrides.get("fast_no_think_default", base.fast_no_think_default)),
    }


class ConversationCreate(BaseModel):
    title: str = "New chat"
    mode: Literal["harness", "chat"] = "harness"
    system_prompt: str = ""


class ConversationUpdate(BaseModel):
    title: str | None = None
    mode: Literal["harness", "chat"] | None = None
    system_prompt: str | None = None


class ChatRequest(BaseModel):
    conversation_id: str
    message: str = Field(min_length=1, max_length=32000)


class SettingsUpdate(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    max_agent_steps: int | None = Field(default=None, ge=1, le=30)
    enable_shell_tool: bool | None = None
    enable_python_tool: bool | None = None
    enable_web_tools: bool | None = None
    fast_no_think_default: bool | None = None


class MemoryCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)


def create_app(
    custom_settings: Settings | None = None,
    custom_llm: OpenAICompatClient | None = None,
) -> FastAPI:
    cfg = custom_settings or settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(cfg.db_path)
        tools = ToolRegistry(cfg.workspace_dir, db)
        llm = custom_llm or OpenAICompatClient()
        harness = AgentHarness(db=db, tools=tools, llm=llm)
        app.state.cfg = cfg
        app.state.db = db
        app.state.tools = tools
        app.state.llm = llm
        app.state.harness = harness
        try:
            yield
        finally:
            if custom_llm is None:
                await llm.close()
            db.close()

    app = FastAPI(title="AWS Agent Chat", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_problem_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": "about:blank",
                "title": "Request Error",
                "status": exc.status_code,
                "detail": str(exc.detail),
                "instance": str(request.url.path),
            },
        )

    # ------------------------------------------------------------------- Routes
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        db: Database = request.app.state.db
        llm: OpenAICompatClient = request.app.state.llm
        rcfg = _effective_config(request.app.state.cfg, db)
        brain_ok = False
        brain_models: list[str] = []
        brain_error = ""
        try:
            brain_models = await llm.list_models(rcfg["llm_base_url"], rcfg["llm_api_key"])
            brain_ok = True
        except BrainUnavailableError as exc:
            brain_error = str(exc)
        return {
            "status": "ok",
            "brain_connected": brain_ok,
            "brain_error": brain_error,
            "brain_models": brain_models,
            "active_model": rcfg["llm_model"],
            "llm_base_url": rcfg["llm_base_url"],
        }

    @app.get("/api/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        db: Database = request.app.state.db
        tools: ToolRegistry = request.app.state.tools
        rcfg = _effective_config(request.app.state.cfg, db)
        masked = dict(rcfg)
        if masked.get("llm_api_key"):
            key = str(masked["llm_api_key"])
            masked["llm_api_key_set"] = True
            masked["llm_api_key_preview"] = key[:4] + "..." if len(key) > 4 else "****"
        else:
            masked["llm_api_key_set"] = False
            masked["llm_api_key_preview"] = ""
        masked.pop("llm_api_key", None)
        masked["available_tools"] = [
            t["function"]["name"]
            for t in tools.schemas(
                enable_shell=rcfg["enable_shell_tool"],
                enable_python=rcfg["enable_python_tool"],
                enable_web=rcfg["enable_web_tools"],
            )
        ]
        return masked

    @app.put("/api/settings")
    async def update_settings(payload: SettingsUpdate, request: Request) -> dict[str, Any]:
        db: Database = request.app.state.db
        updates = payload.model_dump(exclude_none=True)
        if "llm_api_key" in updates and not str(updates["llm_api_key"]).strip():
            updates.pop("llm_api_key")
        db.set_runtime_settings(updates)
        return await get_settings(request)

    @app.get("/api/conversations")
    async def list_conversations(request: Request) -> list[dict[str, Any]]:
        return request.app.state.db.list_conversations()

    @app.post("/api/conversations")
    async def create_conversation(payload: ConversationCreate, request: Request) -> dict[str, Any]:
        return request.app.state.db.create_conversation(
            title=payload.title,
            mode=payload.mode,
            system_prompt=payload.system_prompt,
        )

    @app.get("/api/conversations/{conv_id}")
    async def get_conversation(conv_id: str, request: Request) -> dict[str, Any]:
        db: Database = request.app.state.db
        conv = db.get_conversation(conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"conversation": conv, "messages": db.get_messages(conv_id)}

    @app.patch("/api/conversations/{conv_id}")
    async def patch_conversation(
        conv_id: str, payload: ConversationUpdate, request: Request
    ) -> dict[str, Any]:
        conv = request.app.state.db.update_conversation(
            conv_id,
            title=payload.title,
            mode=payload.mode,
            system_prompt=payload.system_prompt,
        )
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conv

    @app.delete("/api/conversations/{conv_id}")
    async def delete_conversation(conv_id: str, request: Request) -> dict[str, bool]:
        ok = request.app.state.db.delete_conversation(conv_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"deleted": True}

    @app.get("/api/memories")
    async def list_memories(request: Request, q: str = "") -> list[dict[str, Any]]:
        return request.app.state.db.search_memories(q, limit=50)

    @app.post("/api/memories")
    async def create_memory(payload: MemoryCreate, request: Request) -> dict[str, Any]:
        return request.app.state.db.save_memory(payload.topic, payload.content)

    @app.delete("/api/memories/{mem_id}")
    async def delete_memory(mem_id: str, request: Request) -> dict[str, bool]:
        ok = request.app.state.db.delete_memory(mem_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Memory not found")
        return {"deleted": True}

    @app.post("/api/chat/stream")
    async def stream_chat(payload: ChatRequest, request: Request) -> StreamingResponse:
        db: Database = request.app.state.db
        harness: AgentHarness = request.app.state.harness
        conv = db.get_conversation(payload.conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        rcfg = _effective_config(request.app.state.cfg, db)

        async def event_generator():
            async for event in harness.run_turn(
                conv=conv,
                user_message=payload.message,
                runtime_cfg=rcfg,
            ):
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        async def serve_index() -> FileResponse:
            return FileResponse(str(STATIC_DIR / "index.html"))

    return app


app = create_app()
