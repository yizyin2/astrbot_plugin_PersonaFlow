"""独立上下文库的存储：按"轮"追加，每轮带来源；断点标记"从这里重新开始"。

与 AstrBot 的数据库完全分开。一轮 = 一条 user 消息到最终 assistant 回复之间的全部消息
（含工具调用），按轮取窗口可以保证发给模型的消息序列始终完整。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool TEXT NOT NULL,
    ts REAL NOT NULL,
    source_umo TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    platform_id TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    user_text TEXT NOT NULL DEFAULT '',
    assistant_text TEXT NOT NULL DEFAULT '',
    messages_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_turns_pool ON turns(pool, id);
CREATE TABLE IF NOT EXISTS marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool TEXT NOT NULL,
    after_turn_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_marks_pool ON marks(pool, after_turn_id);
"""
_COLS = ("id, pool, ts, source_umo, source_type, source_id, source_name, platform_id, "
         "sender_id, sender_name, user_text, assistant_text, messages_json")


@dataclass
class Turn:
    id: int
    pool: str
    ts: float
    source_umo: str = ""
    source_type: str = ""
    source_id: str = ""
    source_name: str = ""
    platform_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    user_text: str = ""
    assistant_text: str = ""
    messages: list[dict] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(json.dumps(self.messages, ensure_ascii=False))

    def to_dict(self, with_messages: bool = True) -> dict:
        data = {k: getattr(self, k) for k in (
            "id", "pool", "ts", "source_umo", "source_type", "source_id", "source_name",
            "platform_id", "sender_id", "sender_name", "user_text", "assistant_text")}
        data["chars"] = self.chars
        if with_messages:
            data["messages"] = self.messages
        return data


def _row(row) -> Turn:
    try:
        messages = json.loads(row[12] or "[]")
    except ValueError:
        messages = []
    return Turn(*row[:12], messages=messages if isinstance(messages, list) else [])


def message_text(message: dict) -> str:
    """取一条消息里的文字部分。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _set_text(message: dict, text: str) -> None:
    content = message.get("content")
    if isinstance(content, list):
        others = [p for p in content if not (isinstance(p, dict) and p.get("type") == "text")]
        message["content"] = [{"type": "text", "text": text}, *others]
    else:
        message["content"] = text


class ContextStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ContextStore 尚未打开")
        return self._db

    async def append_turn(self, pool: str, messages: list[dict], *, ts: float | None = None,
                          source_umo: str = "", source_type: str = "", source_id: str = "",
                          source_name: str = "", platform_id: str = "", sender_id: str = "",
                          sender_name: str = "") -> int:
        user_text = next((message_text(m) for m in messages if m.get("role") == "user"), "")
        assistant_text = next(
            (message_text(m) for m in reversed(messages) if m.get("role") == "assistant"), "")
        async with self._lock:
            cur = await self.db.execute(
                "INSERT INTO turns (pool, ts, source_umo, source_type, source_id, source_name, "
                "platform_id, sender_id, sender_name, user_text, assistant_text, messages_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (pool, ts or time.time(), source_umo, source_type, source_id, source_name,
                 platform_id, sender_id, sender_name, user_text, assistant_text,
                 json.dumps(messages, ensure_ascii=False)),
            )
            await self.db.commit()
            return int(cur.lastrowid)

    async def last_mark(self, pool: str) -> int:
        """最新断点之前的最后一轮 ID；没有断点返回 0。"""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT COALESCE(MAX(after_turn_id), 0) FROM marks WHERE pool=?", (pool,))
            return int((await cur.fetchone())[0])

    async def recent(self, pool: str, limit: int) -> list[Turn]:
        """最新断点之后的最近 limit 轮，按时间正序。"""
        after = await self.last_mark(pool)
        async with self._lock:
            cur = await self.db.execute(
                f"SELECT {_COLS} FROM turns WHERE pool=? AND id>? ORDER BY id DESC LIMIT ?",
                (pool, after, limit))
            rows = await cur.fetchall()
        return [_row(r) for r in reversed(rows)]

    async def add_mark(self, pool: str, reason: str = "") -> dict:
        async with self._lock:
            cur = await self.db.execute("SELECT COALESCE(MAX(id), 0) FROM turns WHERE pool=?", (pool,))
            after = int((await cur.fetchone())[0])
            now = time.time()
            cur = await self.db.execute(
                "INSERT INTO marks (pool, after_turn_id, ts, reason) VALUES (?,?,?,?)",
                (pool, after, now, reason))
            await self.db.commit()
            return {"id": int(cur.lastrowid), "pool": pool, "after_turn_id": after, "ts": now,
                    "reason": reason}

    async def add_mark_after(self, pool: str, turn_id: int, reason: str = "") -> dict:
        async with self._lock:
            now = time.time()
            cur = await self.db.execute(
                "INSERT INTO marks (pool, after_turn_id, ts, reason) VALUES (?,?,?,?)",
                (pool, int(turn_id), now, reason))
            await self.db.commit()
            return {"id": int(cur.lastrowid), "pool": pool, "after_turn_id": int(turn_id),
                    "ts": now, "reason": reason}

    async def remove_mark(self, mark_id: int) -> None:
        async with self._lock:
            await self.db.execute("DELETE FROM marks WHERE id=?", (int(mark_id),))
            await self.db.commit()

    async def list_marks(self, pool: str) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, after_turn_id, ts, reason FROM marks WHERE pool=? ORDER BY after_turn_id, id",
                (pool,))
            rows = await cur.fetchall()
        return [{"id": r[0], "after_turn_id": r[1], "ts": r[2], "reason": r[3]} for r in rows]

    async def list_pools(self) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT pool, COUNT(*), MAX(ts), COUNT(DISTINCT source_umo) FROM turns GROUP BY pool")
            rows = await cur.fetchall()
        return [{"pool": r[0], "turns": int(r[1]), "last_ts": float(r[2] or 0), "sources": int(r[3])}
                for r in rows]

    async def list_sources(self, pool: str) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT source_umo, source_type, source_id, MAX(source_name), COUNT(*) FROM turns "
                "WHERE pool=? GROUP BY source_umo ORDER BY COUNT(*) DESC", (pool,))
            rows = await cur.fetchall()
        return [{"umo": r[0], "type": r[1], "id": r[2], "name": r[3] or "", "turns": int(r[4])}
                for r in rows]

    async def list_turns(self, pool: str, *, before_id: int | None = None, limit: int = 50,
                         source_umo: str = "", q: str = "") -> list[Turn]:
        sql, params = f"SELECT {_COLS} FROM turns WHERE pool=?", [pool]
        if before_id:
            sql += " AND id<?"
            params.append(int(before_id))
        if source_umo:
            sql += " AND source_umo=?"
            params.append(source_umo)
        if q:
            sql += " AND (user_text LIKE ? OR assistant_text LIKE ? OR sender_name LIKE ?)"
            params += [f"%{q}%"] * 3
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        async with self._lock:
            cur = await self.db.execute(sql, params)
            rows = await cur.fetchall()
        return [_row(r) for r in reversed(rows)]

    async def set_turn_text(self, turn_id: int, user_text: str | None, assistant_text: str | None) -> bool:
        async with self._lock:
            cur = await self.db.execute(f"SELECT {_COLS} FROM turns WHERE id=?", (int(turn_id),))
            row = await cur.fetchone()
            if not row:
                return False
            turn = _row(row)
            if user_text is not None:
                for message in turn.messages:
                    if message.get("role") == "user":
                        _set_text(message, user_text)
                        break
                turn.user_text = user_text
            if assistant_text is not None:
                for message in reversed(turn.messages):
                    if message.get("role") == "assistant":
                        _set_text(message, assistant_text)
                        break
                turn.assistant_text = assistant_text
            await self.db.execute(
                "UPDATE turns SET user_text=?, assistant_text=?, messages_json=? WHERE id=?",
                (turn.user_text, turn.assistant_text,
                 json.dumps(turn.messages, ensure_ascii=False), int(turn_id)))
            await self.db.commit()
        return True

    async def delete_turns(self, turn_ids: list[int]) -> int:
        if not turn_ids:
            return 0
        async with self._lock:
            cur = await self.db.execute(
                f"DELETE FROM turns WHERE id IN ({','.join('?' * len(turn_ids))})",
                [int(i) for i in turn_ids])
            await self.db.commit()
            return int(cur.rowcount or 0)

    async def clear_pool(self, pool: str) -> int:
        async with self._lock:
            cur = await self.db.execute("DELETE FROM turns WHERE pool=?", (pool,))
            await self.db.execute("DELETE FROM marks WHERE pool=?", (pool,))
            await self.db.commit()
            return int(cur.rowcount or 0)
