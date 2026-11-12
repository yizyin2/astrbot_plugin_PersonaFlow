"""首次启动迁移：PersonaFlow 1.x 的配置，以及 GroupSense 的配置和数据。原数据只读不删。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from pathlib import Path

from astrbot.api import logger

from .settings import FIELDS, default_settings

OLD_SENSE_NAME = "astrbot_plugin_GroupSense"
_OLD_MEDIA = f"/{OLD_SENSE_NAME}/media/"


def settings_from_old(old: dict | None, sense_old: dict | None, data_dir: Path) -> tuple[dict, list[str]]:
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
    if isinstance(sense_old, dict):
        for field in FIELDS:
            if field["section"] == "sense" and sense_old.get(field["key"]) is not None:
                data["sense"][field["key"]] = sense_old[field["key"]]
        groups = sense_old.get("enabled_groups") or []
        data["sense"]["enabled_groups"] = groups
        data["sense"]["scope"] = "list" if groups else "all"
        notes.append("导入 GroupSense 的设置" + (f"（受管群 {len(groups)} 个）" if groups else "（接管全部群）"))
    return data, notes


def read_old_sense_config() -> dict | None:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path

        path = Path(get_astrbot_config_path()) / f"{OLD_SENSE_NAME}_config.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(f"[PersonaFlow] 读取 GroupSense 旧配置失败: {e}")
    return None


def _copy_sense_data(src: Path, dst: Path) -> dict:
    report = {"messages": 0, "media": 0}
    dst.mkdir(parents=True, exist_ok=True)
    new_media = f"/{dst.parent.name}/{dst.name}/media/"
    source = sqlite3.connect(f"file:{src / 'groupsense.db'}?mode=ro", uri=True)
    target = sqlite3.connect(dst / "groupsense.db")
    try:
        source.backup(target)
        target.execute("UPDATE media SET path = replace(path, ?, ?)", (_OLD_MEDIA, new_media))
        target.execute(
            "UPDATE messages SET media_json = replace(media_json, ?, ?)", (_OLD_MEDIA, new_media)
        )
        target.commit()
        report["messages"] = target.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        source.close()
        target.close()
    media_src = src / "media"
    if media_src.is_dir():
        (dst / "media").mkdir(exist_ok=True)
        for item in media_src.iterdir():
            if item.is_file() and not (dst / "media" / item.name).exists():
                shutil.copy2(item, dst / "media" / item.name)
                report["media"] += 1
    return report


async def import_sense_data(data_dir: Path) -> str | None:
    """把 GroupSense 的群聊记录和图片缓存复制进本插件数据目录；已有数据时不动。"""
    dst = data_dir / "sense"
    src = data_dir.parent / OLD_SENSE_NAME
    if (dst / "groupsense.db").exists() or not (src / "groupsense.db").is_file():
        return None
    try:
        report = await asyncio.to_thread(_copy_sense_data, src, dst)
    except Exception as e:
        logger.error(f"[PersonaFlow] 导入 GroupSense 数据失败: {e}", exc_info=True)
        for name in ("groupsense.db", "groupsense.db-wal", "groupsense.db-shm"):
            (dst / name).unlink(missing_ok=True)
        return f"导入 GroupSense 群聊记录失败：{e}"
    return f"导入 GroupSense 群聊记录 {report['messages']} 条、图片 {report['media']} 张（原数据保留）"


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
