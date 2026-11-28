"""/drh 用的两张小表：轮次账和撤销栈，存在数据目录的 drh.db。

轮次账（rounds）：每次模型回复完成时记一行，写明这一轮在记忆库 Message 表里是哪一行、在上下文库里是哪一轮，
以及用户消息到达、回复完成、回复发完的时间。/drh 删除时靠它找到要同步删除的 Message 行，
并按时间窗口到 QQ 上找机器人发出的回复来撤回。账里只存回复文字的摘要值，不存原文。

撤销栈（undo）：每次 /drh 删掉的全部内容原样存一份，/drh undo 从栈顶恢复。
会话里一收到新消息（/drh 本身除外），这个会话的撤销栈就清空。

已撤回（recalled）：/drh 撤回成功的 QQ 消息 ID。NapCat 的聊天记录里还会列出撤回过的消息，
撤销后再删同一轮时据此跳过，不再重复撤回。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    ts_user REAL NOT NULL DEFAULT 0,
    ts_done REAL NOT NULL DEFAULT 0,
    ts_sent REAL NOT NULL DEFAULT 0,
    profile_id TEXT NOT NULL DEFAULT '',
    message_row INTEGER NOT NULL DEFAULT 0,
    ctx_pool TEXT NOT NULL DEFAULT '',
    ctx_turn INTEGER NOT NULL DEFAULT 0,
    reply_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rounds_umo ON rounds(umo, id);
CREATE INDEX IF NOT EXISTS idx_rounds_ts ON rounds(ts_done);
CREATE TABLE IF NOT EXISTS undo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    ts REAL NOT NULL,
    operator TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_undo_umo ON undo(umo, id);
CREATE TABLE IF NOT EXISTS recalled (
    message_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""
ROUND_COLS = ("id", "umo", "ts_user", "ts_done", "ts_sent", "profile_id", "message_row",
              "ctx_pool", "ctx_turn", "reply_hash")
# 轮次账保留多久。只用来对应 Message 行和撤回 QQ 消息，太旧的轮次用不上
ROUND_RETENTION_S = 30 * 86400
# 既没对应记忆库记录和上下文库轮次、过了这么久也没发出过消息的轮次，对 /drh 没用，清掉
EMPTY_ROUND_GRACE_S = 600


def reply_hash(text: str | None) -> str:
    text = (text or "").strip()
    return hashlib.sha1(text.encode("utf-8")).hexdigest() if text else ""


class DrhStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._writes = 0

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self.prune()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("DrhStore 尚未打开")
        return self._db

    # ---------- 轮次账 ----------

    async def add_round(self, umo: str, *, ts_user: float, ts_done: float, profile_id: str = "",
                        message_row: int = 0, ctx_pool: str = "", ctx_turn: int = 0,
                        reply: str = "") -> int:
        async with self._lock:
            cur = await self.db.execute(
                "INSERT INTO rounds (umo, ts_user, ts_done, profile_id, message_row, ctx_pool, "
                "ctx_turn, reply_hash) VALUES (?,?,?,?,?,?,?,?)",
                (umo, float(ts_user or 0), float(ts_done or 0), profile_id, int(message_row or 0),
                 ctx_pool, int(ctx_turn or 0), reply_hash(reply)))
            await self.db.commit()
            self._writes += 1
            round_id = int(cur.lastrowid)
        if self._writes % 200 == 0:
            await self.prune()
        return round_id

    async def mark_sent(self, round_id: int, ts: float | None = None) -> None:
        async with self._lock:
            await self.db.execute("UPDATE rounds SET ts_sent=? WHERE id=?",
                                  (float(ts or time.time()), int(round_id)))
            await self.db.commit()

    async def recent_rounds(self, umo: str, limit: int) -> list[dict]:
        """这个会话最近的若干轮，新的在前。"""
        async with self._lock:
            cur = await self.db.execute(
                f"SELECT {', '.join(ROUND_COLS)} FROM rounds WHERE umo=? ORDER BY id DESC LIMIT ?",
                (umo, max(1, int(limit))))
            rows = await cur.fetchall()
        return [dict(zip(ROUND_COLS, r)) for r in rows]

    async def rounds_for_turns(self, pool: str, turn_ids: list[int]) -> dict[int, dict]:
        if not turn_ids:
            return {}
        async with self._lock:
            cur = await self.db.execute(
                f"SELECT {', '.join(ROUND_COLS)} FROM rounds WHERE ctx_pool=? AND ctx_turn IN "
                f"({','.join('?' * len(turn_ids))})", [pool, *map(int, turn_ids)])
            rows = await cur.fetchall()
        return {r[8]: dict(zip(ROUND_COLS, r)) for r in rows}

    async def delete_rounds(self, round_ids: list[int]) -> None:
        if not round_ids:
            return
        async with self._lock:
            await self.db.execute(
                f"DELETE FROM rounds WHERE id IN ({','.join('?' * len(round_ids))})",
                [int(i) for i in round_ids])
            await self.db.commit()

    async def restore_rounds(self, rows: list[dict]) -> None:
        if not rows:
            return
        async with self._lock:
            await self.db.executemany(
                f"INSERT OR REPLACE INTO rounds ({', '.join(ROUND_COLS)}) "
                f"VALUES ({','.join('?' * len(ROUND_COLS))})",
                [tuple(r[c] for c in ROUND_COLS) for r in rows])
            await self.db.commit()

    async def prune(self) -> None:
        async with self._lock:
            cutoff = time.time() - ROUND_RETENTION_S
            await self.db.execute("DELETE FROM rounds WHERE ts_done < ?", (cutoff,))
            await self.db.execute(
                "DELETE FROM rounds WHERE message_row=0 AND ctx_turn=0 AND ts_sent=0 AND ts_done < ?",
                (time.time() - EMPTY_ROUND_GRACE_S,))
            await self.db.execute("DELETE FROM recalled WHERE ts < ?", (cutoff,))
            await self.db.commit()

    # ---------- 已撤回的 QQ 消息 ----------

    async def add_recalled(self, message_ids: list) -> None:
        if not message_ids:
            return
        now = time.time()
        async with self._lock:
            await self.db.executemany(
                "INSERT OR REPLACE INTO recalled (message_id, ts) VALUES (?, ?)",
                [(str(i), now) for i in message_ids])
            await self.db.commit()

    async def recalled_among(self, message_ids: list) -> set[str]:
        ids = list(dict.fromkeys(str(i) for i in message_ids if i is not None))
        if not ids:
            return set()
        async with self._lock:
            cur = await self.db.execute(
                f"SELECT message_id FROM recalled WHERE message_id IN ({','.join('?' * len(ids))})", ids)
            return {r[0] for r in await cur.fetchall()}

    # ---------- 撤销栈 ----------

    async def push_undo(self, umo: str, operator: str, payload: dict, depth: int) -> int:
        async with self._lock:
            cur = await self.db.execute(
                "INSERT INTO undo (umo, ts, operator, payload) VALUES (?,?,?,?)",
                (umo, time.time(), operator, json.dumps(payload, ensure_ascii=False)))
            undo_id = int(cur.lastrowid)
            await self.db.execute(
                "DELETE FROM undo WHERE umo=? AND id NOT IN "
                "(SELECT id FROM undo WHERE umo=? ORDER BY id DESC LIMIT ?)",
                (umo, umo, max(1, int(depth))))
            await self.db.commit()
            return undo_id

    async def peek_undo(self, umo: str) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, ts, operator, payload FROM undo WHERE umo=? ORDER BY id DESC LIMIT 1",
                (umo,))
            row = await cur.fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[3])
        except ValueError:
            payload = {}
        return {"id": row[0], "ts": row[1], "operator": row[2], "payload": payload}

    async def drop_undo(self, undo_id: int) -> None:
        async with self._lock:
            await self.db.execute("DELETE FROM undo WHERE id=?", (int(undo_id),))
            await self.db.commit()

    async def undo_sessions(self) -> set[str]:
        async with self._lock:
            cur = await self.db.execute("SELECT DISTINCT umo FROM undo")
            return {r[0] for r in await cur.fetchall()}

    async def clear_undo(self, umo: str) -> int:
        async with self._lock:
            cur = await self.db.execute("DELETE FROM undo WHERE umo=?", (umo,))
            await self.db.commit()
            return int(cur.rowcount or 0)

    async def count_undo(self, umo: str) -> int:
        async with self._lock:
            cur = await self.db.execute("SELECT COUNT(*) FROM undo WHERE umo=?", (umo,))
            return int((await cur.fetchone())[0])
