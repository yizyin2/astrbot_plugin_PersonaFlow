"""首次启动迁移：PersonaFlow 1.x 的配置。原数据只读不删。

群聊感知的设置和数据由独立的群聊感知插件（GroupSense）自己迁移。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .settings import FIELDS, default_settings


def settings_from_old(old: dict | None, data_dir: Path) -> tuple[dict, list[str]]:
    """由旧配置生成初始设置，返回 (设置, 迁移说明)。"""
    old = old if isinstance(old, dict) else {}
    data = default_settings()
    notes: list[str] = []
    for field in FIELDS:
        if field["section"] == "memory" and old.get(field["key"]) is not None:
            data["memory"][field["key"]] = old[field["key"]]
    profiles = old.get("memory_profiles")
    if isinstance(profiles, list) and profiles:
        for item in profiles:
            if isinstance(item, dict):
                data["profiles"].append({k: v for k, v in item.items() if not k.startswith("__")})
        notes.append(f"导入 {len(data['profiles'])} 个人格记忆档案，记忆库文件保持原位")
    elif str(old.get("personas_name") or "").strip():
        groups = old.get("apply_to_group_chat") or []
        path = str(old.get("database_path") or "").strip() or str(data_dir / "OSNpermemory.db")
        data["profiles"].append(
            {
                "name": "旧版单库",
                "platform_id": "*",
                "personas_name": str(old["personas_name"]).strip(),
                "database_path": path,
                "apply_to_group_chat": groups,
                "enable_private_chat": not groups,
            }
        )
        notes.append("旧版单库模式已转成一个「任意 Bot」档案，继续使用原数据库")
    return data, notes


def write_report(data_dir: Path, notes: list[str]) -> None:
    try:
        (data_dir / "migration.json").write_text(
            json.dumps({"ts": time.time(), "notes": notes}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def read_report(data_dir: Path) -> dict | None:
    try:
        return json.loads((data_dir / "migration.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
