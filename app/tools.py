"""Full agent harness tool registry and sandboxed async execution."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from app.db import Database


class ToolError(RuntimeError):
    """Raised when tool arguments or execution fail."""


def _safe_workspace_path(workspace: Path, raw_path: str) -> Path:
    """Resolve a relative path inside the sandboxed workspace directory."""
    workspace_resolved = workspace.resolve()
    workspace_resolved.mkdir(parents=True, exist_ok=True)
    cleaned = (raw_path or ".").strip().lstrip("/\\")
    target = (workspace_resolved / cleaned).resolve()
    if target != workspace_resolved and workspace_resolved not in target.parents:
        raise ToolError(f"Path '{raw_path}' escapes the sandboxed workspace.")
    return target


def _truncate(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


class ToolRegistry:
    """Manages available harness tools, schemas, and execution."""

    def __init__(self, workspace: Path, db: Database) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db = db

    def schemas(
        self,
        *,
        enable_shell: bool = True,
        enable_python: bool = True,
        enable_web: bool = True,
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "description": "Get the current UTC and local date and time.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files and directories inside the agent workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative directory path (default '.')"}
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read text from a file inside the agent workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative path inside workspace"},
                            "start_line": {"type": "integer", "description": "1-based start line (default 1)"},
                            "end_line": {"type": "integer", "description": "1-based end line (default 250)"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Create or update a file inside the agent workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative path inside workspace"},
                            "content": {"type": "string", "description": "File contents to write"},
                            "append": {"type": "boolean", "description": "Append instead of overwriting"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_files",
                    "description": "Search text or regex across files inside the agent workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Text or regex pattern to find"}
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_save",
                    "description": "Save a durable fact, user preference, or note to long-term memory across conversations.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "topic": {"type": "string", "description": "Short topic label"},
                            "content": {"type": "string", "description": "Fact or note to remember"},
                        },
                        "required": ["topic", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_search",
                    "description": "Search saved long-term memories across conversations.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Keyword to search (empty lists recent)"}
                        },
                        "required": [],
                    },
                },
            },
        ]

        if enable_web:
            tools.extend(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "description": "Search the live web via DuckDuckGo and return titles, URLs, and snippets.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search query"},
                                    "max_results": {"type": "integer", "description": "Max results (default 6)"},
                                },
                                "required": ["query"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "fetch_url",
                            "description": "Fetch and extract readable text from an HTTP/HTTPS web page.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "url": {"type": "string", "description": "Full http:// or https:// URL"}
                                },
                                "required": ["url"],
                            },
                        },
                    },
                ]
            )

        if enable_python:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "python_eval",
                        "description": "Execute Python code in an isolated subprocess inside the workspace and return stdout/stderr.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string", "description": "Python code to execute"}
                            },
                            "required": ["code"],
                        },
                    },
                }
            )

        if enable_shell:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "shell_exec",
                        "description": "Run a shell command inside the sandboxed workspace directory with a 20s timeout.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "description": "Command string to execute"}
                            },
                            "required": ["command"],
                        },
                    },
                }
            )

        return tools

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        enable_shell: bool = True,
        enable_python: bool = True,
        enable_web: bool = True,
    ) -> str:
        handlers: dict[str, Callable[[dict[str, Any]], Coroutine[Any, Any, str]]] = {
            "get_current_time": self._get_current_time,
            "list_files": self._list_files,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "search_files": self._search_files,
            "memory_save": self._memory_save,
            "memory_search": self._memory_search,
        }
        if enable_web:
            handlers["web_search"] = self._web_search
            handlers["fetch_url"] = self._fetch_url
        if enable_python:
            handlers["python_eval"] = self._python_eval
        if enable_shell:
            handlers["shell_exec"] = self._shell_exec

        handler = handlers.get(name)
        if not handler:
            return json.dumps({"error": f"Tool '{name}' is unavailable or disabled."})

        try:
            return await handler(args or {})
        except ToolError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    async def _get_current_time(self, _: dict[str, Any]) -> str:
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now().astimezone()
        return json.dumps(
            {
                "utc": now_utc.isoformat(),
                "local": now_local.isoformat(),
                "timezone": str(now_local.tzinfo),
            }
        )

    async def _list_files(self, args: dict[str, Any]) -> str:
        target = _safe_workspace_path(self.workspace, str(args.get("path", ".")))
        if not target.exists():
            raise ToolError(f"Directory does not exist: {args.get('path', '.')}")
        if not target.is_dir():
            raise ToolError(f"Not a directory: {args.get('path', '.')}")
        entries = []
        for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            entries.append(
                {
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else 0,
                }
            )
        return json.dumps({"workspace_root": str(self.workspace), "entries": entries[:200]})

    async def _read_file(self, args: dict[str, Any]) -> str:
        path = str(args.get("path", ""))
        target = _safe_workspace_path(self.workspace, path)
        if not target.is_file():
            raise ToolError(f"File not found: {path}")
        start = max(1, int(args.get("start_line") or 1))
        end = max(start, int(args.get("end_line") or 250))
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start - 1 : end]
        numbered = [f"{idx}: {line}" for idx, line in enumerate(selected, start=start)]
        return _truncate("\n".join(numbered))

    async def _write_file(self, args: dict[str, Any]) -> str:
        path = str(args.get("path", ""))
        content = str(args.get("content", ""))
        append = bool(args.get("append", False))
        target = _safe_workspace_path(self.workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if append and target.exists():
            with target.open("a", encoding="utf-8") as fh:
                fh.write(content)
        else:
            target.write_text(content, encoding="utf-8")
        return json.dumps({"ok": True, "path": str(target.relative_to(self.workspace)), "bytes": len(content.encode("utf-8"))})

    async def _search_files(self, args: dict[str, Any]) -> str:
        pattern_str = str(args.get("pattern", "")).strip()
        if not pattern_str:
            raise ToolError("Pattern is required.")
        try:
            regex = re.compile(pattern_str, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern_str), re.IGNORECASE)

        matches: list[dict[str, Any]] = []
        for file_path in self.workspace.rglob("*"):
            if not file_path.is_file() or file_path.stat().st_size > 1_000_000:
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "file": str(file_path.relative_to(self.workspace)),
                            "line": idx,
                            "text": line[:200],
                        }
                    )
                    if len(matches) >= 50:
                        return json.dumps({"matches": matches, "truncated": True})
        return json.dumps({"matches": matches, "truncated": False})

    async def _memory_save(self, args: dict[str, Any]) -> str:
        topic = str(args.get("topic", "general"))
        content = str(args.get("content", ""))
        if not content.strip():
            raise ToolError("Memory content cannot be empty.")
        saved = self.db.save_memory(topic, content)
        return json.dumps({"saved": saved})

    async def _memory_search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        items = self.db.search_memories(query, limit=10)
        return json.dumps({"memories": items})

    async def _web_search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ToolError("Search query cannot be empty.")
        max_results = min(10, max(1, int(args.get("max_results") or 6)))
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers=headers,
            )
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict[str, str]] = []
        for card in soup.select(".result"):
            a = card.select_one("a.result__a")
            snippet_el = card.select_one(".result__snippet")
            if not a:
                continue
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
            results.append(
                {
                    "title": a.get_text(" ", strip=True),
                    "url": href,
                    "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                }
            )
            if len(results) >= max_results:
                break
        return json.dumps({"query": query, "results": results})

    async def _fetch_url(self, args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ToolError("Only valid http:// or https:// URLs are allowed.")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" in ctype.lower():
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                text = "\n".join(
                    line.strip()
                    for line in soup.get_text("\n").splitlines()
                    if line.strip()
                )
                return _truncate(text, 12000)
            return _truncate(resp.text, 12000)

    async def _python_eval(self, args: dict[str, Any]) -> str:
        code = str(args.get("code", ""))
        if not code.strip():
            raise ToolError("Python code cannot be empty.")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            code,
            cwd=str(self.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return json.dumps({"error": "Python execution timed out after 15s."})
        return json.dumps(
            {
                "returncode": proc.returncode,
                "stdout": _truncate(stdout.decode("utf-8", errors="replace"), 6000),
                "stderr": _truncate(stderr.decode("utf-8", errors="replace"), 3000),
            }
        )

    async def _shell_exec(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("Command cannot be empty.")
        if os.name == "nt":
            shell_args = ["powershell", "-NoProfile", "-Command", command]
        else:
            shell_args = ["bash", "-lc", command]

        proc = await asyncio.create_subprocess_exec(
            *shell_args,
            cwd=str(self.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return json.dumps({"error": "Shell command timed out after 20s."})
        return json.dumps(
            {
                "returncode": proc.returncode,
                "stdout": _truncate(stdout.decode("utf-8", errors="replace"), 6000),
                "stderr": _truncate(stderr.decode("utf-8", errors="replace"), 3000),
            }
        )
