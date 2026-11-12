"""SQLite persistence for conversations, messages, long-term memory, and runtime settings."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class Database:
    """Thread-safe SQLite wrapper using WAL mode for fast concurrent reads/writes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'harness',
                    system_prompt TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    reasoning TEXT NOT NULL DEFAULT '',
                    tool_traces TEXT NOT NULL DEFAULT '[]',
                    metrics TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conv_created
                ON messages(conversation_id, created_at);

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------------- Settings
    def get_runtime_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM runtime_settings").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except ValueError:
                out[row["key"]] = row["value"]
        return out

    def set_runtime_settings(self, values: dict[str, Any]) -> None:
        with self._lock:
            for key, val in values.items():
                self._conn.execute(
                    "INSERT INTO runtime_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(val)),
                )
            self._conn.commit()

    # ------------------------------------------------------------ Conversations
    def list_conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, mode, system_prompt, created_at, updated_at "
                "FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def create_conversation(
        self,
        title: str = "New chat",
        mode: str = "harness",
        system_prompt: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        conv = {
            "id": uuid.uuid4().hex[:12],
            "title": title.strip() or "New chat",
            "mode": mode,
            "system_prompt": system_prompt,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations (id, title, mode, system_prompt, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conv["id"],
                    conv["title"],
                    conv["mode"],
                    conv["system_prompt"],
                    conv["created_at"],
                    conv["updated_at"],
                ),
            )
            self._conn.commit()
        return conv

    def get_conversation(self, conv_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, mode, system_prompt, created_at, updated_at "
                "FROM conversations WHERE id = ?",
                (conv_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_conversation(
        self,
        conv_id: str,
        *,
        title: str | None = None,
        mode: str | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, Any] | None:
        conv = self.get_conversation(conv_id)
        if not conv:
            return None
        new_title = title if title is not None else conv["title"]
        new_mode = mode if mode is not None else conv["mode"]
        new_prompt = system_prompt if system_prompt is not None else conv["system_prompt"]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET title = ?, mode = ?, system_prompt = ?, updated_at = ? WHERE id = ?",
                (new_title, new_mode, new_prompt, now, conv_id),
            )
            self._conn.commit()
        return self.get_conversation(conv_id)

    def delete_conversation(self, conv_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------------- Messages
    def get_messages(self, conv_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, conversation_id, role, content, reasoning, tool_traces, metrics, created_at "
                "FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conv_id,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["tool_traces"] = json.loads(d["tool_traces"] or "[]")
            d["metrics"] = json.loads(d["metrics"] or "{}")
            items.append(d)
        return items

    def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        *,
        reasoning: str = "",
        tool_traces: list[dict[str, Any]] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        msg_id = uuid.uuid4().hex[:12]
        traces = tool_traces or []
        meta = metrics or {}
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, reasoning, tool_traces, metrics, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg_id,
                    conv_id,
                    role,
                    content,
                    reasoning,
                    json.dumps(traces),
                    json.dumps(meta),
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conv_id),
            )
            self._conn.commit()
        return {
            "id": msg_id,
            "conversation_id": conv_id,
            "role": role,
            "content": content,
            "reasoning": reasoning,
            "tool_traces": traces,
            "metrics": meta,
            "created_at": now,
        }

    # ------------------------------------------------------------------- Memory
    def save_memory(self, topic: str, content: str) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex[:10],
            "topic": topic.strip()[:120] or "general",
            "content": content.strip()[:4000],
            "created_at": time.time(),
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO memories (id, topic, content, created_at) VALUES (?, ?, ?, ?)",
                (item["id"], item["topic"], item["content"], item["created_at"]),
            )
            self._conn.commit()
        return item

    def search_memories(self, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        q = query.strip()
        with self._lock:
            if not q:
                rows = self._conn.execute(
                    "SELECT id, topic, content, created_at FROM memories ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                pattern = f"%{q}%"
                rows = self._conn.execute(
                    "SELECT id, topic, content, created_at FROM memories "
                    "WHERE topic LIKE ? OR content LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (pattern, pattern, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, mem_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            self._conn.commit()
        return cur.rowcount > 0
