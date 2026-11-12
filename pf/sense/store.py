"""群聊记录存储：每群一个内存滑动窗口 + SQLite 写穿持久化。

群聊记录只追加、从不"消费"，主 LLM 调用失败也不会丢上下文。
同一个库里还存图片缓存索引和群成员身份表。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path

import aiosqlite

from .models import MediaRef, MsgRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key TEXT NOT NULL,
    alias INTEGER NOT NULL,
    msg_id TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    nickname TEXT NOT NULL DEFAULT '',
    card TEXT NOT NULL DEFAULT '',
    is_bot INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    at_bot INTEGER NOT NULL DEFAULT 0,
    reply_bot INTEGER NOT NULL DEFAULT 0,
    media_json TEXT NOT NULL DEFAULT '[]',
    kind TEXT NOT NULL DEFAULT 'msg',
    role TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    recalled INTEGER NOT NULL DEFAULT 0,
    extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_group_alias ON messages(group_key, alias);
CREATE INDEX IF NOT EXISTS idx_messages_group_msgid ON messages(group_key, msg_id);
CREATE TABLE IF NOT EXISTS media (
    md5 TEXT PRIMARY KEY,
    path TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'image',
    summary TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_used REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    group_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    card TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (group_key, user_id)
);
"""

# 0.2.x 的库没有这些列，打开时补上
_ADDED_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'msg'"),
    ("role", "TEXT NOT NULL DEFAULT ''"),
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("recalled", "INTEGER NOT NULL DEFAULT 0"),
    ("extra_json", "TEXT NOT NULL DEFAULT '{}'"),
)

_COLUMNS = (
    "group_key, alias, msg_id, sender_id, nickname, card, is_bot, ts, content, "
    "at_bot, reply_bot, media_json, kind, role, title, recalled, extra_json"
)

# 每插入这么多条做一次按群裁剪
_TRIM_EVERY = 200


class MessageStore:
    def __init__(self, db_path: Path, window_size: int, max_per_group: int) -> None:
        self._db_path = db_path
        self._window_size = max(10, window_size)
        self._max_per_group = max(self._window_size, max_per_group)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._windows: dict[str, deque[MsgRecord]] = {}
        self._next_alias: dict[str, int] = {}
        self._inserts_since_trim: dict[str, int] = {}

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        # 先补列再建索引和新表，旧库和新库走同一条路径
        cur = await self._db.execute("PRAGMA table_info(messages)")
        existing = {row[1] for row in await cur.fetchall()}
        if existing:
            for name, ddl in _ADDED_COLUMNS:
                if name not in existing:
                    await self._db.execute(
                        f"ALTER TABLE messages ADD COLUMN {name} {ddl}"
                    )
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("MessageStore 尚未打开")
        return self._db

    @property
    def is_open(self) -> bool:
        return self._db is not None

    def resize(self, window_size: int, max_per_group: int) -> None:
        """调整窗口大小；已加载的窗口丢弃，下次访问时按新大小从库里重新加载。"""
        window_size = max(10, window_size)
        self._max_per_group = max(window_size, max_per_group)
        if window_size != self._window_size:
            self._window_size = window_size
            self._windows.clear()

    # ---------- 消息 ----------

    async def add(self, rec: MsgRecord) -> MsgRecord:
        """追加一条消息并分配群内短编号。重复的 msg_id 返回已有记录。"""
        async with self._lock:
            window = await self._ensure_group(rec.group_key)
            if rec.msg_id:
                for existing in window:
                    if existing.msg_id == rec.msg_id:
                        return existing
            rec.alias = self._next_alias[rec.group_key]
            self._next_alias[rec.group_key] += 1
            window.append(rec)
            await self.db.execute(
                f"INSERT INTO messages ({_COLUMNS}) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.group_key,
                    rec.alias,
                    rec.msg_id,
                    rec.sender_id,
                    rec.nickname,
                    rec.card,
                    int(rec.is_bot),
                    rec.ts,
                    rec.content,
                    int(rec.at_bot),
                    int(rec.reply_bot),
                    json.dumps([m.to_dict() for m in rec.media], ensure_ascii=False),
                    rec.kind,
                    rec.role,
                    rec.title,
                    int(rec.recalled),
                    json.dumps(rec.extra, ensure_ascii=False),
                ),
            )
            count = self._inserts_since_trim.get(rec.group_key, 0) + 1
            if count >= _TRIM_EVERY:
                await self._trim(rec.group_key)
                count = 0
            self._inserts_since_trim[rec.group_key] = count
            await self.db.commit()
        return rec

    async def update(self, rec: MsgRecord) -> None:
        """把一条已有记录的正文、时间、撤回标记和事件信息写回库。

        窗口里的记录是同一个对象，调用方直接改它的字段后再调用本方法即可。
        """
        async with self._lock:
            await self.db.execute(
                "UPDATE messages SET content=?, ts=?, recalled=?, extra_json=? "
                "WHERE group_key=? AND alias=?",
                (
                    rec.content,
                    rec.ts,
                    int(rec.recalled),
                    json.dumps(rec.extra, ensure_ascii=False),
                    rec.group_key,
                    rec.alias,
                ),
            )
            await self.db.commit()

    async def mark_recalled(self, group_key: str, msg_id: str) -> MsgRecord | None:
        """把一条消息标为已撤回，返回这条记录；找不到返回 None。"""
        if not msg_id:
            return None
        async with self._lock:
            window = await self._ensure_group(group_key)
            target = next((r for r in reversed(window) if r.msg_id == msg_id), None)
            if target is None:
                cur = await self.db.execute(
                    f"SELECT {_COLUMNS} FROM messages WHERE group_key=? AND msg_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (group_key, msg_id),
                )
                row = await cur.fetchone()
                target = _row_to_record(row) if row else None
            if target is None:
                return None
            target.recalled = True
            await self.db.execute(
                "UPDATE messages SET recalled=1 WHERE group_key=? AND alias=?",
                (group_key, target.alias),
            )
            await self.db.commit()
        return target

    async def window(self, group_key: str, n: int) -> list[MsgRecord]:
        """返回最近 n 条记录，按时间正序。"""
        async with self._lock:
            window = await self._ensure_group(group_key)
            records = list(window)
        return records[-n:] if n > 0 else []

    async def page(
        self, group_key: str, before_alias: int | None = None, limit: int = 50
    ) -> list[MsgRecord]:
        """直接从库里分页读取（管理面板用），按时间正序返回。"""
        sql = f"SELECT {_COLUMNS} FROM messages WHERE group_key=?"
        params: list = [group_key]
        if before_alias is not None:
            sql += " AND alias<?"
            params.append(before_alias)
        sql += " ORDER BY alias DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        async with self._lock:
            cur = await self.db.execute(sql, params)
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in reversed(rows)]

    async def list_groups(self) -> list[dict]:
        """列出库里有记录的群：条数和最后一条的时间。"""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT group_key, COUNT(*), MAX(ts) FROM messages GROUP BY group_key "
                "ORDER BY MAX(ts) DESC"
            )
            rows = await cur.fetchall()
        return [
            {"group_key": key, "count": int(count), "last_ts": float(last or 0)}
            for key, count, last in rows
        ]

    async def find_by_alias(self, group_key: str, alias: int) -> MsgRecord | None:
        async with self._lock:
            window = await self._ensure_group(group_key)
            for rec in window:
                if rec.alias == alias:
                    return rec
            cur = await self.db.execute(
                f"SELECT {_COLUMNS} FROM messages WHERE group_key=? AND alias=? LIMIT 1",
                (group_key, alias),
            )
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def find_by_msg_id(self, group_key: str, msg_id: str) -> MsgRecord | None:
        if not msg_id:
            return None
        async with self._lock:
            window = await self._ensure_group(group_key)
            for rec in reversed(window):
                if rec.msg_id == msg_id:
                    return rec
            cur = await self.db.execute(
                f"SELECT {_COLUMNS} FROM messages WHERE group_key=? AND msg_id=? "
                "ORDER BY id DESC LIMIT 1",
                (group_key, msg_id),
            )
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def count(self, group_key: str) -> int:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT COUNT(*) FROM messages WHERE group_key=?", (group_key,)
            )
            (n,) = await cur.fetchone()
        return int(n)

    async def clear_group(self, group_key: str) -> int:
        """删除一个群的全部聊天记录，返回删除条数。短编号继续递增，不会和旧编号重复。"""
        async with self._lock:
            window = await self._ensure_group(group_key)
            cur = await self.db.execute(
                "DELETE FROM messages WHERE group_key=?", (group_key,)
            )
            await self.db.commit()
            window.clear()
            self._inserts_since_trim.pop(group_key, None)
        return int(cur.rowcount or 0)

    async def _ensure_group(self, group_key: str) -> deque[MsgRecord]:
        """调用方需持有 self._lock。首次访问时从 DB 加载窗口。"""
        window = self._windows.get(group_key)
        if window is not None:
            return window
        cur = await self.db.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE group_key=? ORDER BY id DESC LIMIT ?",
            (group_key, self._window_size),
        )
        rows = await cur.fetchall()
        window = deque(
            (_row_to_record(r) for r in reversed(rows)), maxlen=self._window_size
        )
        self._windows[group_key] = window
        if group_key not in self._next_alias:
            cur = await self.db.execute(
                "SELECT MAX(alias) FROM messages WHERE group_key=?", (group_key,)
            )
            (max_alias,) = await cur.fetchone()
            self._next_alias[group_key] = int(max_alias or 0) + 1
        return window

    async def _trim(self, group_key: str) -> None:
        cur = await self.db.execute(
            "SELECT id FROM messages WHERE group_key=? ORDER BY id DESC LIMIT 1 OFFSET ?",
            (group_key, self._max_per_group),
        )
        row = await cur.fetchone()
        if row:
            await self.db.execute(
                "DELETE FROM messages WHERE group_key=? AND id<=?", (group_key, row[0])
            )

    # ---------- 群成员 ----------

    async def put_member(
        self, group_key: str, user_id: str, nickname: str, card: str, role: str, title: str
    ) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO members (group_key, user_id, nickname, card, role, title, "
                "updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(group_key, user_id) DO UPDATE SET nickname=excluded.nickname, "
                "card=excluded.card, role=excluded.role, title=excluded.title, "
                "updated_at=excluded.updated_at",
                (group_key, user_id, nickname, card, role, title, time.time()),
            )
            await self.db.commit()

    async def get_members(self, group_key: str) -> list[tuple]:
        """返回 (user_id, nickname, card, role, title, updated_at) 列表。"""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT user_id, nickname, card, role, title, updated_at FROM members "
                "WHERE group_key=?",
                (group_key,),
            )
            return list(await cur.fetchall())

    async def delete_member(self, group_key: str, user_id: str) -> None:
        async with self._lock:
            await self.db.execute(
                "DELETE FROM members WHERE group_key=? AND user_id=?",
                (group_key, user_id),
            )
            await self.db.commit()

    # ---------- 媒体 ----------

    async def get_media(self, md5: str) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT md5, path, kind, summary, caption FROM media WHERE md5=?",
                (md5,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return dict(zip(("md5", "path", "kind", "summary", "caption"), row))

    async def put_media(self, md5: str, path: str, kind: str, summary: str) -> None:
        now = time.time()
        async with self._lock:
            await self.db.execute(
                "INSERT INTO media (md5, path, kind, summary, caption, created_at, last_used) "
                "VALUES (?,?,?,?,'',?,?) "
                "ON CONFLICT(md5) DO UPDATE SET path=excluded.path, last_used=excluded.last_used",
                (md5, path, kind, summary, now, now),
            )
            await self.db.commit()

    async def touch_media(self, md5: str) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE media SET last_used=? WHERE md5=?", (time.time(), md5)
            )
            await self.db.commit()

    async def set_caption(self, md5: str, caption: str) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE media SET caption=? WHERE md5=?", (caption, md5)
            )
            await self.db.commit()

    async def get_captions(self, md5s: list[str]) -> dict[str, str]:
        md5s = [m for m in dict.fromkeys(md5s) if m]
        if not md5s:
            return {}
        placeholders = ",".join("?" * len(md5s))
        async with self._lock:
            cur = await self.db.execute(
                f"SELECT md5, caption FROM media WHERE md5 IN ({placeholders}) AND caption!=''",
                md5s,
            )
            rows = await cur.fetchall()
        return {md5: caption for md5, caption in rows}

    async def expire_media(self, older_than: float) -> list[str]:
        """返回最后使用早于 older_than 的媒体文件路径，并把它们的 path 置空。

        保留行本身，这样图片描述还在，旧记录仍能显示"[图片:描述]"。
        """
        async with self._lock:
            cur = await self.db.execute(
                "SELECT path FROM media WHERE last_used<? AND path!=''", (older_than,)
            )
            paths = [r[0] for r in await cur.fetchall()]
            await self.db.execute(
                "UPDATE media SET path='' WHERE last_used<? AND path!=''", (older_than,)
            )
            await self.db.commit()
        return paths


def _row_to_record(row) -> MsgRecord:
    (
        group_key,
        alias,
        msg_id,
        sender_id,
        nickname,
        card,
        is_bot,
        ts,
        content,
        at_bot,
        reply_bot,
        media_json,
        kind,
        role,
        title,
        recalled,
        extra_json,
    ) = row
    try:
        media = [MediaRef.from_dict(m) for m in json.loads(media_json or "[]")]
    except (ValueError, TypeError, AttributeError):
        media = []
    try:
        extra = json.loads(extra_json or "{}")
        if not isinstance(extra, dict):
            extra = {}
    except (ValueError, TypeError):
        extra = {}
    return MsgRecord(
        group_key=group_key,
        msg_id=msg_id or "",
        sender_id=sender_id or "",
        nickname=nickname or "",
        card=card or "",
        is_bot=bool(is_bot),
        ts=float(ts),
        content=content or "",
        at_bot=bool(at_bot),
        reply_bot=bool(reply_bot),
        media=media,
        alias=int(alias),
        kind=kind or "msg",
        role=role or "",
        title=title or "",
        recalled=bool(recalled),
        extra=extra,
    )
