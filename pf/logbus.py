"""插件日志总线：结构化事件 + 文本日志，写入内存环形缓冲和 SQLite，并推送给面板。

文本日志通过订阅 AstrBot 的日志代理（Dashboard 日志页用的同一个）收集，只留本插件的条目，
控制台输出不受影响；结构化事件由各模块调用 event() 写入，不进控制台。
按插件上架规范，日志一律经由 AstrBot 提供的接口，不自己挂日志处理器。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path

import aiosqlite

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
CATEGORY_LABELS = {
    "log": "文本日志",
    "route": "路由",
    "memory": "人格记忆",
    "context": "上下文库",
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


def _find_broker():
    """找到 AstrBot 的日志代理。先取 LogManager 记下的那个，取不到再顺着 logger 的处理器找。"""
    try:
        from astrbot.core import LogManager

        broker = getattr(LogManager, "_log_broker", None)
        if broker is not None and hasattr(broker, "register"):
            return broker
    except Exception:
        pass
    try:
        from astrbot.api import logger

        node = logger
        while node is not None:
            for handler in getattr(node, "handlers", []):
                broker = getattr(handler, "log_broker", None)
                if broker is not None and hasattr(broker, "register"):
                    return broker
            node = getattr(node, "parent", None)
    except Exception:
        pass
    return None


def entry_from_broker(item: dict, plugin_name: str) -> tuple[str, str, dict] | None:
    """日志代理里的一条 → (级别, 文本, 附加数据)；不是本插件的日志返回 None。"""
    if not isinstance(item, dict) or item.get("type") == "trace":
        return None
    scope = (item.get("instrumentation_scope") or {}).get("name") or ""
    if item.get("plugin_name") != plugin_name and scope != f"astrbot.plugin.{plugin_name}":
        return None
    attrs = item.get("attributes") or {}
    data: dict = {}
    path = str(attrs.get("code.file.path") or "")
    if path:
        data["module"] = Path(path).stem
    if attrs.get("code.line.number") is not None:
        data["line"] = attrs.get("code.line.number")
    full = str(item.get("data") or "")
    if "Traceback (most recent call last)" in full:
        data["traceback"] = full[full.index("Traceback (most recent call last)"):]
    message = str(item.get("body") or item.get("data") or "")
    return str(item.get("level") or item.get("severity_text") or "INFO"), message, data


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
        self._broker = None
        self._broker_queue: asyncio.Queue | None = None
        self._broker_task: asyncio.Task | None = None
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

    def attach(self, plugin_name: str) -> bool:
        """订阅 AstrBot 的日志代理，把本插件的文本日志也收进来。找不到代理时返回 False。"""
        self.detach()
        broker = _find_broker()
        if broker is None:
            return False
        self._broker = broker
        self._broker_queue = broker.register()
        self._broker_task = asyncio.create_task(self._consume(plugin_name, self._broker_queue))
        return True

    async def _consume(self, plugin_name: str, queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            try:
                parsed = entry_from_broker(item, plugin_name)
                if parsed is not None:
                    level, message, data = parsed
                    self.event("log", message, level=level, **data)
            except Exception:
                pass

    def detach(self) -> None:
        if self._broker_task is not None:
            self._broker_task.cancel()
        if self._broker is not None and self._broker_queue is not None:
            try:
                self._broker.unregister(self._broker_queue)
            except Exception:
                pass
        self._broker = self._broker_queue = self._broker_task = None

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

    async def since(self, category: str, ts: float) -> list[dict]:
        """某个类别在 ts 之后的全部条目（只取会话和附加数据），给总览页做统计。"""
        await self.flush()
        if self._db is None:
            return []
        cur = await self._db.execute(
            "SELECT ts, session, data_json FROM logs WHERE category = ? AND ts >= ? ORDER BY id DESC",
            (category, ts),
        )
        out = []
        for ts_, session, data_json in await cur.fetchall():
            try:
                data = json.loads(data_json or "{}")
            except ValueError:
                data = {}
            out.append({"ts": ts_, "session": session, "data": data})
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
