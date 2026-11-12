"""插件日志总线：结构化事件 + 文本日志，写入内存环形缓冲和 SQLite，并推送给面板。

文本日志靠挂在插件专属 logger 上的 Handler 收集，控制台输出不受影响；结构化事件
由各模块调用 event() 写入，不进控制台。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from collections import deque
from pathlib import Path

import aiosqlite

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
CATEGORY_LABELS = {
    "log": "文本日志",
    "route": "路由",
    "judge": "判断",
    "inject": "注入",
    "reply": "回复",
    "memory": "人格记忆",
    "context": "上下文库",
    "notice": "群事件",
    "settings": "设置",
    "error": "错误",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_logs_cat ON logs(category, id);
CREATE INDEX IF NOT EXISTS idx_logs_session ON logs(session, id);
"""


def _level_name(level) -> str:
    name = str(level or "INFO").upper()
    if name == "WARN":
        name = "WARNING"
    if name == "CRITICAL":
        name = "ERROR"
    return name if name in LEVELS else "INFO"


class _BusHandler(logging.Handler):
    def __init__(self, bus: "LogBus") -> None:
        super().__init__(level=logging.DEBUG)
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = {"module": record.module, "line": record.lineno}
            if record.exc_info:
                data["traceback"] = "".join(traceback.format_exception(*record.exc_info))
            self.bus.event("log", record.getMessage(), level=record.levelname, **data)
        except Exception:
            pass


class LogBus:
    def __init__(self, db_path: Path, ring_size: int = 2000) -> None:
        self.db_path = Path(db_path)
        self.level = "INFO"
        self.retention_days = 7
        self.max_rows = 50000
        self.save_prompts = False
        self._ring: deque[dict] = deque(maxlen=ring_size)
        self._pending: list[dict] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._last_id = 0
        self._db: aiosqlite.Connection | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handler: _BusHandler | None = None
        self._logger: logging.Logger | None = None
        self._last_cleanup = 0.0

    def configure(self, settings: dict) -> None:
        self.level = _level_name(settings.get("level"))
        self.retention_days = int(settings.get("retention_days") or 7)
        self.max_rows = int(settings.get("max_rows") or 50000)
        self.save_prompts = bool(settings.get("save_prompts"))

    # ---------- 写入 ----------

    def event(self, category: str, message: str, *, level: str = "INFO", session: str = "", **data) -> None:
        """记一条事件。可在任意线程调用，不阻塞。"""
        level = _level_name(level)
        if LEVELS[level] < LEVELS[self.level]:
            return
        now = time.time()
        self._last_id = max(self._last_id + 1, int(now * 1_000_000))
        entry = {
            "id": self._last_id,
            "ts": now,
            "level": level,
            "category": str(category),
            "session": str(session or ""),
            "message": str(message),
            "data": {k: v for k, v in data.items() if v is not None},
        }
        self._ring.append(entry)
        self._pending.append(entry)
        if not self._subscribers:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._publish(entry)
        elif self._loop is not None:
            self._loop.call_soon_threadsafe(self._publish, entry)

    def _publish(self, entry: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def attach(self, logger: logging.Logger) -> None:
        """把插件 logger 的文本日志也收进来。"""
        self.detach()
        self._handler = _BusHandler(self)
        self._logger = logger
        logger.addHandler(self._handler)

    def detach(self) -> None:
        if self._logger is not None and self._handler is not None:
            self._logger.removeHandler(self._handler)
        self._logger = self._handler = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        self._task = asyncio.create_task(self._writer())

    async def stop(self) -> None:
        self.detach()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self.flush()
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def flush(self) -> None:
        if self._db is None or not self._pending:
            return
        batch, self._pending = self._pending, []
        try:
            await self._db.executemany(
                "INSERT OR IGNORE INTO logs (id, ts, level, category, session, message, data_json) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        e["id"], e["ts"], e["level"], e["category"], e["session"], e["message"],
                        json.dumps(e["data"], ensure_ascii=False, default=str),
                    )
                    for e in batch
                ],
            )
            await self._db.commit()
        except Exception:
            pass

    async def _writer(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self.flush()
            if time.time() - self._last_cleanup > 3600:
                self._last_cleanup = time.time()
                await self.cleanup()

    async def cleanup(self) -> None:
        if self._db is None:
            return
        try:
            await self._db.execute(
                "DELETE FROM logs WHERE ts < ?", (time.time() - self.retention_days * 86400,)
            )
            await self._db.execute(
                "DELETE FROM logs WHERE id <= COALESCE((SELECT id FROM logs ORDER BY id DESC "
                "LIMIT 1 OFFSET ?), 0)",
                (self.max_rows,),
            )
            await self._db.commit()
        except Exception:
            pass

    async def clear(self) -> None:
        self._ring.clear()
        self._pending.clear()
        if self._db is not None:
            await self._db.execute("DELETE FROM logs")
            await self._db.commit()

    # ---------- 读取 ----------

    async def query(
        self,
        *,
        level: str = "",
        category: str = "",
        session: str = "",
        q: str = "",
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """按条件查询，最新的在前。"""
        await self.flush()
        if self._db is None:
            return []
        where, params = [], []
        if level:
            allowed = [name for name, value in LEVELS.items() if value >= LEVELS[_level_name(level)]]
            where.append(f"level IN ({','.join('?' * len(allowed))})")
            params += allowed
        if category:
            where.append("category = ?")
            params.append(category)
        if session:
            where.append("session = ?")
            params.append(session)
        if q:
            where.append("(message LIKE ? OR data_json LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if before_id:
            where.append("id < ?")
            params.append(before_id)
        sql = "SELECT id, ts, level, category, session, message, data_json FROM logs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        cur = await self._db.execute(sql, params)
        rows = await cur.fetchall()
        out = []
        for id_, ts, lvl, cat, sess, msg, data_json in rows:
            try:
                data = json.loads(data_json or "{}")
            except ValueError:
                data = {}
            out.append({"id": id_, "ts": ts, "level": lvl, "category": cat,
                        "session": sess, "message": msg, "data": data})
        return out

    def recent(self, limit: int = 30, categories: tuple[str, ...] | None = None) -> list[dict]:
        items = [e for e in self._ring if not categories or e["category"] in categories]
        return list(reversed(items[-limit:]))

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def stream(self):
        """SSE 数据流：每条事件一行 JSON，定时发心跳。"""
        queue = self.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(entry, ensure_ascii=False, default=str)}\n\n"
        finally:
            self.unsubscribe(queue)
