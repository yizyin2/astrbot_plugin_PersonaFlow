"""管理面板的后端接口。全部注册在 /astrbot_plugin_PersonaFlow/ 下，由 Dashboard 鉴权后转发。"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request, stream_response

from ..astr_route import AstrRouting, umo_of
from ..context.service import resolve_binding, session_key_of
from ..logbus import CATEGORY_LABELS
from ..migrate import read_report
from ..paths import PLUGIN_NAME, VERSION
from ..sense.service import GROUP_OVERRIDE_KEYS
from ..settings import CONTEXT_MODES, fields_for_panel, normalize_profile, profile_db_path

_BACKUP_INTERVAL_S = 600
_BACKUP_KEEP = 5


def _api(func):
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        try:
            result = await func(self, *args, **kwargs)
            return result if not isinstance(result, (dict, list)) else json_response(result)
        except (KeyError, ValueError) as e:
            return error_response(str(e).strip("'\""), status_code=400)
        except Exception as e:
            logger.error(f"[PersonaFlow] 面板接口 {func.__name__} 出错: {e}", exc_info=True)
            return error_response(f"{type(e).__name__}: {e}", status_code=500)

    return wrapper


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WebApi:
    def __init__(self, plugin) -> None:
        self.p = plugin
        self._last_backup: dict[str, float] = {}
        self.astr = AstrRouting(plugin.context)

    def register(self, context) -> None:
        register = getattr(context, "register_web_api", None)
        if register is None:
            return
        routes = [
            ("bootstrap", self.bootstrap, "GET"),
            ("overview", self.overview, "GET"),
            ("settings/save", self.settings_save, "POST"),
            ("profiles/save", self.profile_save, "POST"),
            ("profiles/delete", self.profile_delete, "POST"),
            ("sessions", self.sessions, "GET"),
            ("sessions/refresh", self.sessions_refresh, "POST"),
            ("sessions/remove", self.session_remove, "POST"),
            ("route/test", self.route_test, "GET"),
            ("astr/persona", self.astr_persona, "POST"),
            ("astr/conf", self.astr_conf, "POST"),
            ("memory/stats", self.memory_stats, "GET"),
            ("memory/impressions", self.memory_impressions, "GET"),
            ("memory/impression/save", self.memory_impression_save, "POST"),
            ("memory/impression/delete", self.memory_impression_delete, "POST"),
            ("memory/memories", self.memory_memories, "GET"),
            ("memory/memory/save", self.memory_memory_save, "POST"),
            ("memory/memory/delete", self.memory_memory_delete, "POST"),
            ("memory/messages", self.memory_messages, "GET"),
            ("memory/message/delete", self.memory_message_delete, "POST"),
            ("memory/prompt", self.memory_prompt, "GET"),
            ("memory/summarize", self.memory_summarize, "POST"),
            ("memory/compact", self.memory_compact, "POST"),
            ("sense/groups", self.sense_groups, "GET"),
            ("sense/records", self.sense_records, "GET"),
            ("sense/members", self.sense_members, "GET"),
            ("sense/thumb", self.sense_thumb, "GET"),
            ("sense/toggle", self.sense_toggle, "POST"),
            ("sense/override", self.sense_override, "POST"),
            ("sense/clear", self.sense_clear, "POST"),
            ("ctx/pools", self.ctx_pools, "GET"),
            ("ctx/turns", self.ctx_turns, "GET"),
            ("ctx/window", self.ctx_window, "GET"),
            ("ctx/bind", self.ctx_bind, "POST"),
            ("ctx/turn/save", self.ctx_turn_save, "POST"),
            ("ctx/turn/delete", self.ctx_turn_delete, "POST"),
            ("ctx/mark/add", self.ctx_mark_add, "POST"),
            ("ctx/mark/remove", self.ctx_mark_remove, "POST"),
            ("ctx/pool/clear", self.ctx_pool_clear, "POST"),
            ("ctx/pool/create", self.ctx_pool_create, "POST"),
            ("ctx/pool/remove", self.ctx_pool_remove, "POST"),
            ("ctx/import", self.ctx_import, "POST"),
            ("ctx/export", self.ctx_export, "GET"),
            ("logs", self.logs, "GET"),
            ("logs/stream", self.logs_stream, "GET"),
            ("logs/clear", self.logs_clear, "POST"),
        ]
        for path, handler, method in routes:
            register(f"/{PLUGIN_NAME}/{path}", handler, [method], f"PersonaFlow {path}")

    # ================= 公共数据 =================

    def _platforms(self) -> list[dict]:
        out = []
        try:
            for item in self.p.context.get_config().get("platform", []) or []:
                if isinstance(item, dict) and item.get("id"):
                    out.append({"id": str(item["id"]), "type": str(item.get("type") or ""),
                                "enabled": bool(item.get("enable", True))})
        except Exception:
            pass
        return out

    def _personas(self) -> list[str]:
        try:
            return [str(p.get("name")) for p in self.p.context.provider_manager.personas if p.get("name")]
        except Exception:
            return []

    def _providers(self) -> list[dict]:
        out = []
        try:
            for provider in self.p.context.get_all_providers():
                meta = provider.meta()
                out.append({"id": str(meta.id), "model": str(getattr(meta, "model", "") or "")})
        except Exception:
            pass
        return out

    def _profiles(self) -> list[dict]:
        out = []
        for profile in self.p.settings.data["profiles"]:
            engine = self.p.router.engine_by_profile_id(profile["id"])
            out.append({
                **profile,
                "error": self.p.settings.profile_errors.get(profile["id"], ""),
                "active": engine is not None,
                "db_path": str(profile_db_path(self.p.data_dir, profile)),
            })
        return out

    def _health(self) -> list[dict]:
        issues = []
        if not self.p.enabled:
            issues.append({"level": "warn", "text": "插件总开关已关闭（AstrBot 插件配置里的「启用」）"})
        if self.p.old_sense_active():
            issues.append({"level": "error",
                           "text": "旧的 GroupSense 插件仍在运行，群聊感知暂不接管。请到 AstrBot 插件页停用或卸载它。"})
        try:
            if self.p.sense.enabled:
                issues += [{"level": "warn", "text": text} for text in self.p.sense.conflicts()]
        except Exception:
            pass
        personas = set(self._personas())
        platforms = {p["id"] for p in self._platforms()}
        for profile in self.p.settings.data["profiles"]:
            name = profile["name"] or f"{profile['platform_id']} / {profile['personas_name']}"
            error = self.p.settings.profile_errors.get(profile["id"])
            if error:
                issues.append({"level": "error", "text": f"档案「{name}」未生效：{error}"})
            elif profile["enabled"]:
                if personas and profile["personas_name"] not in personas:
                    issues.append({"level": "warn", "text": f"档案「{name}」的人格 {profile['personas_name']} 不存在"})
                if platforms and profile["platform_id"] != "*" and profile["platform_id"] not in platforms:
                    issues.append({"level": "warn", "text": f"档案「{name}」的 Bot {profile['platform_id']} 不存在"})
        if not self.p.settings.data["profiles"]:
            issues.append({"level": "info", "text": "还没有人格记忆档案。到「路由」页添加后才会记录和注入记忆。"})
        return issues

    def _engine(self, profile_id: str):
        engine = self.p.router.engine_by_profile_id(str(profile_id or ""))
        if engine is None:
            raise KeyError("档案不存在或未生效")
        return engine

    async def _backup(self, engine) -> None:
        """写操作前给记忆库留一份备份（同一个库十分钟内只备份一次，保留最近几份）。"""
        path = Path(engine.db_path)
        now = time.time()
        if now - self._last_backup.get(str(path), 0) < _BACKUP_INTERVAL_S or not path.is_file():
            return
        self._last_backup[str(path)] = now
        backup_dir = self.p.data_dir / "backups"

        def run():
            backup_dir.mkdir(parents=True, exist_ok=True)
            target = backup_dir / f"{path.stem[:16]}-{time.strftime('%Y%m%d-%H%M%S')}.db"
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
            old = sorted(backup_dir.glob(f"{path.stem[:16]}-*.db"))[:-_BACKUP_KEEP]
            for item in old:
                item.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(run)
        except Exception as e:
            logger.warning(f"[PersonaFlow] 备份记忆库失败: {e}")

    # ================= 总览与设置 =================

    @_api
    async def bootstrap(self):
        return {
            "version": VERSION,
            "settings": self.p.settings.data,
            "meta": fields_for_panel(),
            "platforms": self._platforms(),
            "personas": self._personas(),
            "confs": self.astr.confs(),
            "providers": self._providers(),
            "profiles": self._profiles(),
            "log_categories": CATEGORY_LABELS,
            "override_keys": list(GROUP_OVERRIDE_KEYS),
            "context_modes": list(CONTEXT_MODES),
            "migration": read_report(self.p.data_dir),
        }

    @_api
    async def overview(self):
        pools = await self.p.ctx_store.list_pools()
        groups = await self.p.sense.panel_groups()
        engines = []
        for profile in self._profiles():
            stats = None
            if profile["active"]:
                try:
                    stats = await self._engine(profile["id"]).stats()
                except Exception:
                    stats = None
            engines.append({"id": profile["id"], "name": profile["name"],
                            "platform_id": profile["platform_id"], "persona": profile["personas_name"],
                            "active": profile["active"], "error": profile["error"],
                            "context_mode": profile["context"]["mode"], "stats": stats})
        return {
            "health": self._health(),
            "profiles": engines,
            "groups": groups,
            "pools": pools,
            "sessions": len(self.p.sessions.sessions),
            "sense_enabled": self.p.sense.enabled and not self.p.old_sense_active(),
            "recent": self.p.bus.recent(30, ("judge", "notice", "memory", "context", "settings", "error")),
        }

    @_api
    async def settings_save(self):
        body = await request.json(default={})
        data = self.p.settings.snapshot()
        for section in ("memory", "sense", "context", "logs"):
            if isinstance(body.get(section), dict):
                data[section].update(body[section])
        await self.p.apply_settings(data, "面板修改设置")
        return {"settings": self.p.settings.data}

    @_api
    async def profile_save(self):
        body = await request.json(default={})
        profile = normalize_profile(body.get("profile") or {})
        data = self.p.settings.snapshot()
        for index, item in enumerate(data["profiles"]):
            if item["id"] == profile["id"]:
                data["profiles"][index] = profile
                break
        else:
            data["profiles"].append(profile)
        await self.p.apply_settings(data, f"保存档案 {profile['id']}")
        return {"profiles": self._profiles(), "id": profile["id"]}

    @_api
    async def profile_delete(self):
        body = await request.json(default={})
        data = self.p.settings.snapshot()
        data["profiles"] = [p for p in data["profiles"] if p["id"] != body.get("id")]
        await self.p.apply_settings(data, f"删除档案 {body.get('id')}（数据库文件保留）")
        return {"profiles": self._profiles()}

    # ================= 路由 =================

    def _resolve(self, platform_id: str, self_id: str, persona: str, group_id: str, user_id: str,
                 umo: str = "") -> dict:
        steps = []
        engine, key = None, None
        known_persona = persona
        on_platform = [p for p in self.p.settings.data["profiles"] if p["platform_id"] in (platform_id, "*")]
        # 从群列表拉来的会话还不知道是哪个机器人账号：把这个 Bot 下档案里填过的账号都试一遍，
        # 否则填了账号的档案永远匹配不上它
        accounts = [self_id] if self_id else list(dict.fromkeys(["", *(p["bot_self_id"] for p in on_platform)]))
        if not self_id and len(accounts) > 1:
            steps.append("还不知道这个会话对应哪个机器人账号，按档案里填的账号匹配")
        candidates = [persona] if persona else list(dict.fromkeys(p["personas_name"] for p in on_platform))
        if not persona:
            steps.append("没有记录到这个会话的人格（它还没触发过回复），按范围匹配到的第一个档案显示")
        fallback = None
        for name in candidates:
            for account in accounts:
                found_key, found = self.p.router.match(platform_id, name, account)
                if found is None:
                    continue
                if found.allows(group_id, user_id):
                    key, engine, persona = found_key, found, name
                    break
                fallback = fallback or (found_key, found, name)
            if engine is not None:
                break
        if engine is None and fallback is not None:
            key, engine, persona = fallback
        profile = None
        in_scope = False
        reason = ""
        if engine is None:
            if known_persona:
                reason = f"这个会话当前用的人格是「{known_persona}」，没有 Bot + 这个人格的档案"
            elif on_platform:
                reason = "这个 Bot 下没有档案能匹配它"
            else:
                reason = f"Bot「{platform_id}」下还没有档案"
            steps.append(reason)
        else:
            profile = self.p.settings.profile(str(engine.config.get("profile_id") or ""))
            in_scope = engine.allows(group_id, user_id)
            steps.append(f"匹配档案：{'/'.join(key) if key else '旧版单库'}")
            if in_scope:
                steps.append("会话在档案的生效范围内")
            else:
                if session_key_of(group_id, user_id) in (profile or {}).get("exclude_sessions", []):
                    reason = "它被手动移出了这个档案"
                else:
                    reason = "档案只对指定的群生效，这个群不在名单里" if group_id else "档案没有打开「私聊也生效」"
                steps.append(reason + "，所以不记录也不注入")
        binding = resolve_binding(profile, group_id, user_id, umo) if (profile and in_scope) else None
        managed = bool(group_id) and self.p.sense.enabled and self.p.sense.is_managed_key(platform_id, group_id)
        session_key = session_key_of(group_id, user_id)
        override = ((profile or {}).get("context", {}).get("sessions") or {}).get(session_key)
        return {
            "persona": persona,
            "profile_id": profile["id"] if profile else "",
            "profile_name": (profile["name"] or f"{profile['platform_id']} / {profile['personas_name']}") if profile else "",
            "in_scope": in_scope,
            "context_mode": binding.mode if binding else "astrbot",
            "context_pool": binding.pool if binding else "",
            "context_override": bool(override),
            "managed": managed,
            "reason": reason,
            "steps": steps,
        }

    @_api
    async def sessions(self):
        out = []
        for item in self.p.sessions.list():
            is_group = item["type"] == "group"
            # 人格以 AstrBot 现在的路由为准；读不到时退回上次回复时记下的人格
            astr = await self.astr.describe(umo_of(item))
            persona = astr["persona"] if astr["ok"] else item.get("persona", "")
            resolved = self._resolve(
                item["platform_id"], item.get("self_id", ""), persona,
                item["target_id"] if is_group else "", "" if is_group else item["target_id"],
                item.get("umo", ""))
            out.append({**item, "persona": persona, "resolved": resolved, "astr": astr})
        return {"sessions": out}

    @_api
    async def sessions_refresh(self):
        """从在线的 QQ Bot 拉取群列表，补进会话登记表。"""
        added = 0
        manager = getattr(self.p.context, "platform_manager", None)
        for inst in (manager.get_insts() if manager else []):
            try:
                meta = inst.meta()
                if meta.name != "aiocqhttp":
                    continue
                bot = getattr(inst, "bot", None) or inst.get_client()
                try:
                    login = await bot.call_action("get_login_info")
                    account = str((login or {}).get("user_id") or "")
                except Exception:
                    account = ""
                groups = await bot.call_action("get_group_list")
                for group in groups or []:
                    gid = str(group.get("group_id") or "")
                    if gid:
                        before = len(self.p.sessions.sessions)
                        self.p.sessions.touch_raw(
                            str(meta.id), "group", gid, self_id=account,
                            name=str(group.get("group_name") or ""),
                            platform_name="aiocqhttp", active=False)
                        added += len(self.p.sessions.sessions) - before
            except Exception as e:
                logger.debug(f"[PersonaFlow] 拉取群列表失败: {e}")
        self.p.sessions.save()
        return {"added": added}

    @_api
    async def session_remove(self):
        body = await request.json(default={})
        self.p.sessions.remove(str(body.get("key") or ""))
        self.p.sessions.save()
        return {"ok": True}

    def _session(self, key: str) -> dict:
        item = self.p.sessions.sessions.get(key)
        if item is None:
            raise ValueError("会话不存在，刷新页面后再试")
        return item

    @_api
    async def astr_persona(self):
        """改 AstrBot 的会话规则：给一个会话强制指定人格，或取消强制。"""
        body = await request.json(default={})
        item = self._session(str(body.get("key") or ""))
        persona = str(body.get("persona") or "").strip()
        if persona and persona not in self._personas():
            raise ValueError(f"AstrBot 里没有人格「{persona}」")
        umo = umo_of(item)
        before = await self.astr.describe(umo)
        after = await self.astr.set_persona(umo, persona)
        if after["persona"]:
            item["persona"] = after["persona"]
            self.p.sessions.save(force=True)
        self.p.bus.event(
            "settings",
            f"改了 AstrBot 的会话规则：{item.get('name') or item['target_id']} 的人格 "
            f"{before['persona'] or '无'} → {after['persona'] or '无'}"
            + (f"（强制指定 {persona}）" if persona else "（取消强制指定）"),
            session=umo, before=before, after=after)
        return {"astr": after}

    @_api
    async def astr_conf(self):
        """改 AstrBot 的配置文件路由：让一个会话走指定的配置文件，或删掉它单独的路由。"""
        body = await request.json(default={})
        item = self._session(str(body.get("key") or ""))
        umo = umo_of(item)
        before = await self.astr.describe(umo)
        after = await self.astr.set_conf(umo, str(body.get("conf_id") or "").strip())
        if after["persona"]:
            item["persona"] = after["persona"]
            self.p.sessions.save(force=True)
        self.p.bus.event(
            "settings",
            f"改了 AstrBot 的配置文件路由：{item.get('name') or item['target_id']} "
            f"{before['conf_name'] or '?'} → {after['conf_name'] or '?'}",
            session=umo, before=before, after=after)
        return {"astr": after}

    @_api
    async def route_test(self):
        q = request.query
        return self._resolve(
            str(q.get("platform_id", "")), str(q.get("self_id", "")), str(q.get("persona", "")),
            str(q.get("group_id", "")), str(q.get("user_id", "")))

    # ================= 人格记忆 =================

    @_api
    async def memory_stats(self):
        engine = self._engine(request.query.get("profile", ""))
        return {**await engine.stats(), "db_path": engine.db_path}

    @_api
    async def memory_impressions(self):
        q = request.query
        engine = self._engine(q.get("profile", ""))
        rows, total = await engine.list_impressions(
            str(q.get("q", "")), _int(q.get("offset"), 0), min(_int(q.get("limit"), 50), 500))
        return {"items": rows, "total": total}

    @_api
    async def memory_impression_save(self):
        body = await request.json(default={})
        engine = self._engine(body.get("profile"))
        qq = str(body.get("qq_number") or "").strip()
        if not qq:
            raise ValueError("缺少用户 ID")
        await self._backup(engine)
        await engine.save_impression(
            qq, str(body.get("name") or qq), str(body.get("relationship") or ""),
            str(body.get("impression") or ""), _int(body.get("dialogue_count")))
        return {"ok": True}

    @_api
    async def memory_impression_delete(self):
        body = await request.json(default={})
        engine = self._engine(body.get("profile"))
        await self._backup(engine)
        return {"removed": await engine.delete_user(str(body.get("qq_number") or ""))}

    @_api
    async def memory_memories(self):
        engine = self._engine(request.query.get("profile", ""))
        return {"items": await engine.list_memories()}

    @_api
    async def memory_memory_save(self):
        body = await request.json(default={})
        engine = self._engine(body.get("profile"))
        text = str(body.get("memory") or "").strip()
        if not text:
            raise ValueError("记忆内容不能为空")
        await self._backup(engine)
        return {"id": await engine.save_memory(_int(body.get("id")), text)}

    @_api
    async def memory_memory_delete(self):
        body = await request.json(default={})
        engine = self._engine(body.get("profile"))
        await self._backup(engine)
        ids = [i for i in (_int(v) for v in body.get("ids") or []) if i is not None]
        return {"removed": await engine.delete_memories(ids)}

    @_api
    async def memory_messages(self):
        q = request.query
        engine = self._engine(q.get("profile", ""))
        rows, total = await engine.list_messages(
            str(q.get("user", "")), _int(q.get("offset"), 0), min(_int(q.get("limit"), 50), 500))
        return {"items": rows, "total": total}

    @_api
    async def memory_message_delete(self):
        body = await request.json(default={})
        engine = self._engine(body.get("profile"))
        await self._backup(engine)
        ids = [i for i in (_int(v) for v in body.get("ids") or []) if i is not None]
        return {"removed": await engine.delete_messages(ids)}

    @_api
    async def memory_prompt(self):
        return await self._engine(request.query.get("profile", "")).prompt_preview()

    @_api
    async def memory_summarize(self):
        body = await request.json(default={})
        engine = self._engine(body.get("profile"))
        await self._backup(engine)
        summary = await engine.run_memory_summary()
        if not summary:
            raise ValueError("没有生成记忆：可能没有对话记录、全文总结已关闭，或模型没有返回有效结果（见日志）")
        return {"summary": summary}

    @_api
    async def memory_compact(self):
        body = await request.json(default={})
        engine = self._engine(body.get("profile"))
        await self._backup(engine)
        if not await engine.run_compaction(force=bool(body.get("force"))):
            raise ValueError("没有压缩：记忆不足两条，或模型没有返回有效结果（见日志）")
        return {"ok": True}

    # ================= 群聊感知 =================

    @_api
    async def sense_groups(self):
        return {"groups": await self.p.sense.panel_groups()}

    @_api
    async def sense_records(self):
        q = request.query
        key = str(q.get("key", ""))
        return {"items": await self.p.sense.panel_records(
            key, _int(q.get("before")), min(_int(q.get("limit"), 60), 300)),
            "state": self.p.sense.mute_state(key)}

    @_api
    async def sense_members(self):
        return {"items": await self.p.sense.panel_members(str(request.query.get("key", "")))}

    @_api
    async def sense_thumb(self):
        return {"url": await self.p.sense.panel_thumbnail(str(request.query.get("md5", "")))}

    @_api
    async def sense_toggle(self):
        """打开或关闭某个群的群聊感知。"""
        body = await request.json(default={})
        platform_id, group_id = str(body.get("platform_id") or ""), str(body.get("group_id") or "")
        if not group_id:
            raise ValueError("缺少群号")
        on = bool(body.get("managed"))
        data = self.p.settings.snapshot()
        sense = data["sense"]
        keys = {group_id, f"{platform_id}:{group_id}"}
        entry = f"{platform_id}:{group_id}" if platform_id else group_id
        if sense["scope"] == "all":
            sense["excluded_groups"] = [g for g in sense["excluded_groups"] if g not in keys]
            if not on:
                sense["excluded_groups"].append(entry)
        else:
            sense["enabled_groups"] = [g for g in sense["enabled_groups"] if g not in keys]
            if on:
                sense["enabled_groups"].append(entry)
        await self.p.apply_settings(data, f"群 {group_id} 群聊感知{'开启' if on else '关闭'}")
        return {"ok": True}

    @_api
    async def sense_override(self):
        body = await request.json(default={})
        key = str(body.get("key") or "")
        if not key:
            raise ValueError("缺少群")
        values = body.get("values") if isinstance(body.get("values"), dict) else {}
        data = self.p.settings.snapshot()
        data["sense"]["group_overrides"][key] = {
            k: v for k, v in values.items() if k in GROUP_OVERRIDE_KEYS and v not in (None, "")}
        await self.p.apply_settings(data, f"群 {key} 单独设置")
        return {"ok": True}

    @_api
    async def sense_clear(self):
        body = await request.json(default={})
        return {"removed": await self.p.sense.store.clear_group(str(body.get("key") or ""))}

    # ================= 上下文库 =================

    def _pool_label(self, pool: str) -> dict:
        profile_id, _, name = pool.partition("/")
        profile = self.p.settings.profile(profile_id)
        return {
            "profile_id": profile_id,
            "profile_name": (profile["name"] or f"{profile['platform_id']} / {profile['personas_name']}") if profile else "（档案已删除）",
            "kind": "isolated" if name.startswith("@") else "shared",
            "name": name[1:] if name.startswith("@") else name,
        }

    @_api
    async def ctx_pools(self):
        pools = {item["pool"]: item for item in await self.p.ctx_store.list_pools()}
        # 已配置但还没有内容的共享池也列出来
        for profile in self.p.settings.data["profiles"]:
            for name in profile["context"]["pools"]:
                pools.setdefault(f"{profile['id']}/{name}",
                                 {"pool": f"{profile['id']}/{name}", "turns": 0, "last_ts": 0, "sources": 0})
        out = []
        for pool, item in pools.items():
            out.append({**item, **self._pool_label(pool), "users": self._pool_users(pool),
                        "marks": await self.p.ctx_store.list_marks(pool),
                        "source_list": await self.p.ctx_store.list_sources(pool)})
        out.sort(key=lambda x: -x["last_ts"])
        return {"pools": out}

    def _pool_users(self, pool: str) -> dict:
        """一个共享池正在被谁使用：是不是档案的默认去向，以及有几个会话单独指到它。"""
        profile_id, _, name = pool.partition("/")
        profile = self.p.settings.profile(profile_id)
        if profile is None or name.startswith("@"):
            return {"default": False, "sessions": 0}
        ctx = profile["context"]
        return {
            "default": ctx["mode"] == "shared" and ctx["pool"] == name,
            "sessions": sum(1 for o in ctx["sessions"].values()
                            if o["mode"] == "shared" and (o["pool"] or ctx["pool"]) == name),
        }

    @_api
    async def ctx_pool_create(self):
        """在某个档案下创建一个空的共享池，之后可以在路由页把会话指到它。"""
        body = await request.json(default={})
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("请填写名称")
        if "/" in name or name.startswith("@") or len(name) > 40:
            raise ValueError("名称不能包含 /、不能以 @ 开头，且不超过 40 个字")
        data = self.p.settings.snapshot()
        profile = next((p for p in data["profiles"] if p["id"] == body.get("profile")), None)
        if profile is None:
            raise KeyError("档案不存在")
        if name in profile["context"]["pools"]:
            raise ValueError("这个档案下已经有同名的共享池")
        profile["context"]["pools"].append(name)
        await self.p.apply_settings(data, f"新建共享池 {name}")
        return {"pool": f"{profile['id']}/{name}"}

    @_api
    async def ctx_pool_remove(self):
        """删除一个没有被使用的共享池，连同里面的对话。"""
        body = await request.json(default={})
        pool = str(body.get("pool") or "")
        profile_id, _, name = pool.partition("/")
        users = self._pool_users(pool)
        if users["default"] or users["sessions"]:
            raise ValueError("这个共享池还在使用中。先到路由页把用它的会话和默认去向改到别处。")
        removed = await self.p.ctx_store.clear_pool(pool)
        data = self.p.settings.snapshot()
        profile = next((p for p in data["profiles"] if p["id"] == profile_id), None)
        if profile is not None and name in profile["context"]["pools"]:
            profile["context"]["pools"].remove(name)
            await self.p.apply_settings(data, f"删除共享池 {name}")
        return {"removed": removed}

    @_api
    async def ctx_turns(self):
        q = request.query
        pool = str(q.get("pool", ""))
        turns = await self.p.ctx_store.list_turns(
            pool, before_id=_int(q.get("before")), limit=min(_int(q.get("limit"), 40), 200),
            source_umo=str(q.get("source", "")), q=str(q.get("q", "")))
        return {"items": [t.to_dict() for t in turns],
                "marks": await self.p.ctx_store.list_marks(pool),
                "last_mark": await self.p.ctx_store.last_mark(pool)}

    @_api
    async def ctx_window(self):
        """预览某个池下一次请求会带上的轮次。"""
        from ..context.service import Binding

        q = request.query
        pool = str(q.get("pool", ""))
        info = self._pool_label(pool)
        profile = self.p.settings.profile(info["profile_id"])
        binding = Binding(pool, info["kind"], info["profile_id"],
                          bool((profile or {}).get("context", {}).get("private_visible_in_groups", True)))
        source = str(q.get("source", ""))
        turns = await self.p.ctx.preview(binding, current_umo=source, is_group=":GroupMessage:" in source)
        return {"ids": [t.id for t in turns], "chars": sum(t.chars for t in turns)}

    @_api
    async def ctx_bind(self):
        """设置档案的默认上下文模式，或某个会话的单独设置。"""
        body = await request.json(default={})
        data = self.p.settings.snapshot()
        profile = next((p for p in data["profiles"] if p["id"] == body.get("profile")), None)
        if profile is None:
            raise KeyError("档案不存在")
        mode = body.get("mode")
        pool = str(body.get("pool") or "").strip()
        session = str(body.get("session") or "")
        ctx = profile["context"]
        if session:
            if mode in CONTEXT_MODES:
                ctx["sessions"][session] = {"mode": mode, "pool": pool}
            else:
                ctx["sessions"].pop(session, None)
        else:
            if mode not in CONTEXT_MODES:
                raise ValueError("无效的上下文模式")
            ctx["mode"] = mode
            if pool:
                ctx["pool"] = pool
            if "private_visible_in_groups" in body:
                ctx["private_visible_in_groups"] = bool(body["private_visible_in_groups"])
        await self.p.apply_settings(data, "修改上下文路由")
        return {"profiles": self._profiles()}

    @_api
    async def ctx_turn_save(self):
        body = await request.json(default={})
        ok = await self.p.ctx_store.set_turn_text(
            _int(body.get("id"), 0), body.get("user_text"), body.get("assistant_text"))
        if not ok:
            raise KeyError("这一轮不存在")
        return {"ok": True}

    @_api
    async def ctx_turn_delete(self):
        body = await request.json(default={})
        ids = [i for i in (_int(v) for v in body.get("ids") or []) if i is not None]
        return {"removed": await self.p.ctx_store.delete_turns(ids)}

    @_api
    async def ctx_mark_add(self):
        body = await request.json(default={})
        pool = str(body.get("pool") or "")
        if not pool:
            raise ValueError("缺少池")
        if body.get("after") is not None:
            return await self.p.ctx_store.add_mark_after(pool, _int(body["after"], 0), "面板")
        return await self.p.ctx.reset(pool, "面板")

    @_api
    async def ctx_mark_remove(self):
        body = await request.json(default={})
        await self.p.ctx_store.remove_mark(_int(body.get("id"), 0))
        return {"ok": True}

    @_api
    async def ctx_pool_clear(self):
        body = await request.json(default={})
        return {"removed": await self.p.ctx_store.clear_pool(str(body.get("pool") or ""))}

    @_api
    async def ctx_import(self):
        """把一个会话在 AstrBot 里的现有对话历史导入某个池。"""
        body = await request.json(default={})
        pool, umo = str(body.get("pool") or ""), str(body.get("umo") or "")
        if not pool or not umo:
            raise ValueError("缺少池或会话")
        manager = self.p.context.conversation_manager
        cid = await manager.get_curr_conversation_id(umo)
        conversation = await manager.get_conversation(umo, cid) if cid else None
        if conversation is None:
            raise ValueError("这个会话在 AstrBot 里没有对话历史")
        history = json.loads(conversation.history or "[]")
        from ..context.service import sanitize_messages

        history = sanitize_messages([m for m in history if isinstance(m, dict)], True)
        turns, current = [], []
        for message in history:
            if message.get("role") == "user" and current:
                turns.append(current)
                current = []
            current.append(message)
        if current:
            turns.append(current)
        turns = [t for t in turns if t and t[0].get("role") == "user"
                 and any(m.get("role") == "assistant" for m in t)]
        item = next((s for s in self.p.sessions.sessions.values() if s.get("umo") == umo), {})
        base = time.time() - len(turns)
        for index, turn in enumerate(turns):
            await self.p.ctx_store.append_turn(
                pool, turn, ts=base + index, source_umo=umo, source_type=item.get("type", ""),
                source_id=item.get("target_id", ""), source_name=item.get("name", "") or "导入",
                platform_id=item.get("platform_id", ""), sender_name="（导入的历史）")
        return {"imported": len(turns)}

    @_api
    async def ctx_export(self):
        pool = str(request.query.get("pool", ""))
        turns = await self.p.ctx_store.list_turns(pool, limit=500)
        path = self.p.data_dir / "tmp" / f"context-{int(time.time())}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pool": pool, "turns": [t.to_dict() for t in turns]},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        return file_response(path, filename="context.json", content_type="application/json")

    # ================= 日志 =================

    @_api
    async def logs(self):
        q = request.query
        return {"items": await self.p.bus.query(
            level=str(q.get("level", "")), category=str(q.get("category", "")),
            session=str(q.get("session", "")), q=str(q.get("q", "")),
            before_id=_int(q.get("before")), limit=_int(q.get("limit"), 100))}

    async def logs_stream(self):
        return stream_response(self.p.bus.stream())

    @_api
    async def logs_clear(self):
        await self.p.bus.clear()
        return {"ok": True}
