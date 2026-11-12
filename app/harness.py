"""Multi-step ReAct / tool-calling agent harness."""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from app.db import Database
from app.llm import BrainUnavailableError, OpenAICompatClient, StreamTurnResult
from app.tools import ToolRegistry

DEFAULT_HARNESS_SYSTEM_PROMPT = """You are an intelligent, uncensored, helpful AI assistant running inside a full tool-using agent harness.
You have access to tools for live web search, webpage reading, Python execution, shell commands, sandboxed workspace file operations, and long-term cross-conversation memory.
Guidelines:
- Answer directly, clearly, and without preachy refusals or filler.
- Use tools whenever up-to-date information, calculation, code verification, file editing, or memory lookup improves accuracy.
- Keep tool use purposeful and concise; synthesize tool outputs into a clean final answer with markdown formatting.
"""

DEFAULT_CHAT_SYSTEM_PROMPT = """You are a fast, intelligent, direct AI assistant. Answer clearly using clean markdown without unnecessary filler."""


class AgentHarness:
    """Runs either fast direct chat or multi-step tool-calling agent execution."""

    def __init__(
        self,
        *,
        db: Database,
        tools: ToolRegistry,
        llm: OpenAICompatClient,
    ) -> None:
        self.db = db
        self.tools = tools
        self.llm = llm

    def _build_history(
        self,
        conv: dict[str, Any],
        *,
        mode: str,
        memories_context: str,
    ) -> list[dict[str, Any]]:
        custom_sys = (conv.get("system_prompt") or "").strip()
        base_sys = custom_sys or (
            DEFAULT_HARNESS_SYSTEM_PROMPT if mode == "harness" else DEFAULT_CHAT_SYSTEM_PROMPT
        )
        if memories_context:
            base_sys += f"\n\nSaved long-term memories:\n{memories_context}"

        messages: list[dict[str, Any]] = [{"role": "system", "content": base_sys}]
        for row in self.db.get_messages(conv["id"])[-30:]:
            if row["role"] in ("user", "assistant") and row["content"]:
                messages.append({"role": row["role"], "content": row["content"]})
        return messages

    async def run_turn(
        self,
        *,
        conv: dict[str, Any],
        user_message: str,
        runtime_cfg: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        mode = conv.get("mode", "harness")
        if mode == "chat":
            tool_schemas: list[dict[str, Any]] = []
        else:
            tool_schemas = self.tools.schemas(
                enable_shell=bool(runtime_cfg.get("enable_shell_tool", True)),
                enable_python=bool(runtime_cfg.get("enable_python_tool", True)),
                enable_web=bool(runtime_cfg.get("enable_web_tools", True)),
            )

        # Save user message immediately.
        self.db.add_message(conv["id"], "user", user_message)
        if conv["title"] == "New chat":
            new_title = user_message.strip().splitlines()[0][:52] or "New chat"
            self.db.update_conversation(conv["id"], title=new_title)
            yield {"type": "title", "title": new_title}

        recent_memories = self.db.search_memories("", limit=5) if mode == "harness" else []
        mem_ctx = "\n".join(f"- [{m['topic']}] {m['content']}" for m in recent_memories)
        messages = self._build_history(conv, mode=mode, memories_context=mem_ctx)

        max_steps = int(runtime_cfg.get("max_agent_steps", 12)) if tool_schemas else 1
        base_url = str(runtime_cfg["llm_base_url"])
        api_key = str(runtime_cfg.get("llm_api_key", ""))
        model = str(runtime_cfg["llm_model"])
        fast_no_think = bool(runtime_cfg.get("fast_no_think_default", True))

        all_traces: list[dict[str, Any]] = []
        all_reasoning: list[str] = []
        final_content = ""
        last_metrics: dict[str, Any] = {}
        turn_start = time.perf_counter()

        try:
            for step in range(1, max_steps + 1):
                yield {"type": "status", "step": step, "message": "Thinking..." if step == 1 else f"Agent step {step}..."}
                turn_result: StreamTurnResult | None = None

                async for event in self.llm.stream_chat(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    tools=tool_schemas or None,
                    fast_no_think=fast_no_think,
                ):
                    etype = event["type"]
                    if etype == "reasoning":
                        yield {"type": "reasoning", "text": event["text"]}
                    elif etype == "token":
                        yield {"type": "token", "text": event["text"]}
                    elif etype == "result":
                        turn_result = event["result"]

                if turn_result is None:
                    break
                if turn_result.reasoning:
                    all_reasoning.append(turn_result.reasoning)
                last_metrics = {
                    "ttft_ms": turn_result.ttft_ms,
                    "llm_ms": turn_result.total_ms,
                    "completion_tokens": turn_result.completion_tokens,
                    "steps": step,
                    "model": model,
                }

                if not turn_result.tool_calls:
                    final_content = turn_result.content
                    break

                # Append assistant tool-call turn and execute each requested tool.
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn_result.content or "",
                        "tool_calls": turn_result.tool_calls,
                    }
                )

                for tc in turn_result.tool_calls:
                    fn = tc.get("function") or {}
                    tool_name = str(fn.get("name", ""))
                    raw_args = fn.get("arguments", "{}")
                    try:
                        parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except ValueError:
                        parsed_args = {}

                    yield {
                        "type": "tool_start",
                        "id": tc["id"],
                        "name": tool_name,
                        "args": parsed_args,
                    }
                    t0 = time.perf_counter()
                    output = await self.tools.execute(
                        tool_name,
                        parsed_args,
                        enable_shell=bool(runtime_cfg.get("enable_shell_tool", True)),
                        enable_python=bool(runtime_cfg.get("enable_python_tool", True)),
                        enable_web=bool(runtime_cfg.get("enable_web_tools", True)),
                    )
                    dur_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                    trace = {
                        "id": tc["id"],
                        "name": tool_name,
                        "args": parsed_args,
                        "output": output,
                        "duration_ms": dur_ms,
                    }
                    all_traces.append(trace)
                    yield {"type": "tool_end", **trace}

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": output,
                        }
                    )
            else:
                final_content = (
                    final_content
                    or "Reached maximum agent steps. Please ask me to continue if more steps are needed."
                )

        except BrainUnavailableError as exc:
            err_msg = str(exc)
            yield {"type": "error", "message": err_msg}
            return

        last_metrics["total_turn_ms"] = round((time.perf_counter() - turn_start) * 1000.0, 1)
        saved = self.db.add_message(
            conv["id"],
            "assistant",
            final_content,
            reasoning="\n\n".join(all_reasoning),
            tool_traces=all_traces,
            metrics=last_metrics,
        )
        yield {"type": "done", "message": saved, "metrics": last_metrics}
