"""会话登记表：记下插件见过的每个群和私聊，面板里配置路由时可以直接挑选。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def session_key(platform_id: str, kind: str, target_id: str) -> str:
    return f"{platform_id}:{kind}:{target_id}"


class SessionRegistry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.sessions: dict[str, dict] = {}
        self._dirty = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.sessions = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def touch(self, event, *, name: str = "", persona: str | None = None) -> dict | None:
        """登记一次会话活动。"""
        try:
            platform_id = str(event.get_platform_id())
            group_id = str(event.get_group_id() or "")
            kind = "group" if group_id else "private"
            target = group_id or str(event.get_sender_id() or "")
            if not target:
                return None
            if not name:
                if group_id:
                    group = getattr(event.message_obj, "group", None)
                    name = str(getattr(group, "group_name", "") or "")
                else:
                    name = str(event.get_sender_name() or "")
            return self.touch_raw(
                platform_id,
                kind,
                target,
                self_id=str(event.get_self_id() or ""),
                name=name,
                persona=persona,
                umo=str(event.unified_msg_origin),
                platform_name=str(event.get_platform_name() or ""),
            )
        except Exception:
            return None

    def touch_raw(self, platform_id: str, kind: str, target_id: str, *, self_id: str = "",
                  name: str = "", persona: str | None = None, umo: str = "",
                  platform_name: str = "", active: bool = True) -> dict:
        key = session_key(platform_id, kind, target_id)
        item = self.sessions.get(key)
        if item is None:
            item = {"key": key, "platform_id": platform_id, "type": kind, "target_id": target_id,
                    "self_id": "", "name": "", "persona": "", "umo": "", "platform_name": "",
                    "last_seen": 0.0, "count": 0}
            self.sessions[key] = item
        if active:
            item["last_seen"] = time.time()
            item["count"] = int(item.get("count") or 0) + 1
        for field, value in (("self_id", self_id), ("name", name), ("umo", umo),
                             ("platform_name", platform_name), ("persona", persona)):
            if value:
                item[field] = value
        self._dirty = True
        return item

    def list(self) -> list[dict]:
        return sorted(self.sessions.values(), key=lambda s: -float(s.get("last_seen") or 0))

    def remove(self, key: str) -> None:
        if self.sessions.pop(key, None) is not None:
            self._dirty = True

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.sessions, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass
