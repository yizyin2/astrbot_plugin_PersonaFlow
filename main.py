"""PersonaFlow 2：人格记忆 + 群聊感知 + 独立上下文库，带管理面板。

这个文件只负责向 AstrBot 注册钩子、指令、工具和 Web API，并把调用转发给 pf/ 下的模块。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

from .pf.context.service import ContextService, resolve_binding
from .pf.context.store import ContextStore
from .pf.logbus import LogBus
from .pf.memory.core import MemoryRouter
from .pf.migrate import (
    OLD_SENSE_NAME,
    import_sense_data,
    read_old_sense_config,
    settings_from_old,
    write_report,
)
from .pf.paths import PLUGIN_NAME, VERSION, get_data_dir
from .pf.sense.members import role_label
from .pf.sense.service import SenseService
from .pf.sessions import SessionRegistry
from .pf.settings import SettingsStore
from .pf.web.api import WebApi



class PersonaFlow(Star):
    """其他插件可通过 ``context.get_registered_star("astrbot_plugin_PersonaFlow").star_cls``
    拿到实例，调用公开接口 ``resolve_profile``、``get_impressions``。"""

    API_VERSION = 2

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.enabled = bool(self.config.get("enable", True))
        self.data_dir = get_data_dir()
        self.settings = SettingsStore(self.data_dir / "settings.json", self.data_dir)
        self._fresh_install = not self.settings.exists()
        if self._fresh_install:
            data, notes = settings_from_old(dict(self.config), read_old_sense_config(), self.data_dir)
            self.settings.replace(data)
            self._migration_notes = notes
        else:
            self.settings.load()
            self._migration_notes = []

        self.bus = LogBus(self.data_dir / "logs.db")
        self.bus.configure(self.settings.data["logs"])
        try:
            self.router = MemoryRouter(context, self.settings.router_config())
        except ValueError as e:
            logger.error(f"[PersonaFlow] 人格记忆档案无效，暂不启用任何档案: {e}")
            self.router = MemoryRouter(context, {"disable_legacy": True, "memory_profiles": []})
        self.router.attach_bus(self.bus)
        self.sense = SenseService(
            context,
            self._sense_config(),
            impression_source=self.get_impressions,
            bus=self.bus,
        )
        self.ctx_store = ContextStore(self.data_dir / "context.db")
        self.ctx = ContextService(self.ctx_store, self.bus)
        self.ctx.configure(self.settings.data["context"])
        self.sessions = SessionRegistry(self.data_dir / "sessions.json")
        self.web = WebApi(self)
        self._ready = False
        self._flush_task: asyncio.Task | None = None
        self._apply_lock = asyncio.Lock()

    def _sense_config(self) -> dict:
        config = self.settings.sense_config()
        config["enable"] = bool(config.get("enable", True)) and self.enabled
        return config

    # ================= 生命周期 =================

    async def initialize(self) -> None:
        await self.bus.start()
        try:
            self.bus.attach(logging.getLogger(f"astrbot.plugin.{PLUGIN_NAME}"))
        except Exception:
            pass
        if self._fresh_install:
            note = await import_sense_data(self.data_dir)
            if note:
                self._migration_notes.append(note)
            if self._migration_notes:
                write_report(self.data_dir, self._migration_notes)
                for note in self._migration_notes:
                    logger.info(f"[PersonaFlow] 迁移：{note}")
        await self.sense.initialize()
        await self.ctx_store.open()
        self.web.register(self.context)
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._ready = True
        if self.old_sense_active():
            logger.warning(
                "[PersonaFlow] 旧的 GroupSense 插件仍在运行，群聊感知暂不接管。请停用或卸载它。"
            )
        self.bus.event("settings", f"PersonaFlow {VERSION} 已启动",
                       profiles=len(self.router.engines()))

    async def terminate(self) -> None:
        self._ready = False
        if self._flush_task:
            self._flush_task.cancel()
        for closer in (self.sense.terminate, self.router.terminate, self.ctx_store.close):
            try:
                await closer()
            except Exception as e:
                logger.error(f"[PersonaFlow] 关闭失败: {e}")
        self.sessions.save(force=True)
        await self.bus.stop()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.sessions.save()

    def old_sense_active(self) -> bool:
        """旧的独立 GroupSense 插件是否还在运行（会和内置的群聊感知重复接管）。"""
        try:
            meta = self.context.get_registered_star(OLD_SENSE_NAME)
            return bool(meta and getattr(meta, "activated", False))
        except Exception:
            return False

    async def apply_settings(self, raw: dict, reason: str = "") -> dict:
        """保存一份新设置并让各模块立即生效。"""
        async with self._apply_lock:
            self.settings.replace(raw)
            try:
                await self.router.reload(self.settings.router_config())
            except ValueError as e:
                logger.error(f"[PersonaFlow] 应用档案失败: {e}")
            self.router.attach_bus(self.bus)
            self.sense.apply_config(self._sense_config())
            self.ctx.configure(self.settings.data["context"])
            self.bus.configure(self.settings.data["logs"])
            self.bus.event("settings", f"设置已更新{('：' + reason) if reason else ''}")
            return self.settings.data

    # ================= 公开接口 =================

    async def resolve_profile(self, event: AstrMessageEvent) -> dict | None:
        """返回该事件对应的记忆配置；当前人格或聊天不在生效范围时返回 None。"""
        return await self.router.resolve_profile(event)

    async def get_impressions(self, event: AstrMessageEvent, user_ids: list[str]) -> dict[str, dict]:
        """只读获取指定用户在该事件对应记忆库中的关系与印象；出错或不在范围时返回空字典。"""
        return await self.router.get_impressions(event, user_ids)

    # ================= 内部工具 =================

    def profile_of(self, req) -> dict | None:
        memory = getattr(req, "_personaflow_memory", None)
        if memory is None:
            return None
        return self.settings.profile(str(memory.config.get("profile_id") or ""))

    def source_of(self, event: AstrMessageEvent) -> dict:
        """这次请求来自哪里，用于上下文库的来源标注。"""
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        sender_name = str(event.get_sender_name() or "") or sender_id
        platform_id = str(event.get_platform_id())
        if group_id:
            key = f"{platform_id}:{group_id}"
            item = self.sessions.sessions.get(f"{platform_id}:group:{group_id}") or {}
            name = self.sense.group_name_cached(key) or item.get("name") or group_id
            raw = getattr(event.message_obj, "raw_message", None)
            sender = raw.get("sender") if isinstance(raw, dict) and isinstance(raw.get("sender"), dict) else {}
            tag = role_label(str(sender.get("role") or ""))
            label = f"【群「{name}」】{sender_name}({sender_id}){f'[{tag}]' if tag else ''}："
            return {"type": "group", "id": group_id, "name": name, "platform_id": platform_id,
                    "sender_id": sender_id, "sender_name": sender_name, "label": label}
        return {"type": "private", "id": sender_id, "name": sender_name, "platform_id": platform_id,
                "sender_id": sender_id, "sender_name": sender_name,
                "label": f"【私聊】{sender_name}({sender_id})："}

    # ================= 消息入口 =================

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群消息和群事件，并交给判断模型决定是否由主模型回复（含被 @ 的消息）。"""
        if not self._ready:
            return
        raw = event.message_obj.raw_message
        if not (isinstance(raw, dict) and raw.get("post_type", "message") != "message"):
            self.sessions.touch(event)
        if self.old_sense_active():
            return
        gen = self.sense.on_group_message(event)
        try:
            async for result in gen:
                yield result
        finally:
            await gen.aclose()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """登记私聊会话，供面板配置路由时选择。"""
        if self._ready and event.get_messages():
            self.sessions.touch(event)

    @filter.regex(r"^(reset|new)\b")
    async def on_builtin_reset(self, event: AstrMessageEvent):
        """内置的 /reset、/new 用在被接管的会话里时，在上下文库打一个断点。"""
        if not (self._ready and self.enabled and event.is_at_or_wake_command):
            return
        if not self.settings.data["context"].get("sync_builtin_reset", True):
            return
        await self._reset_context(event, f"/{event.get_message_str().split()[0]}", quiet=True)

    async def _binding_for(self, event: AstrMessageEvent):
        memory = await self.router._select_memory(event, management=True)
        if memory is None:
            return None
        profile = self.settings.profile(str(memory.config.get("profile_id") or ""))
        return resolve_binding(profile, str(event.get_group_id() or ""),
                               str(event.get_sender_id() or ""), str(event.unified_msg_origin))

    async def _reset_context(self, event: AstrMessageEvent, reason: str, quiet: bool = False) -> None:
        binding = await self._binding_for(event)
        if binding is None:
            if not quiet:
                await event.send(event.plain_result("当前会话没有使用独立上下文库。"))
            return
        # 共享池会影响所有会话，群里的独立池也会影响整个群，只允许管理员重置
        if (binding.mode == "shared" or event.get_group_id()) and not event.is_admin():
            if not quiet:
                await event.send(event.plain_result("❌ 仅 AstrBot 管理员可以重置这份上下文。"))
            return
        who = f"{event.get_sender_name()}({event.get_sender_id()})"
        await self.ctx.reset(binding.pool, f"{reason} by {who}")
        scope = "共享上下文（所有共用它的会话都会从这里重新开始）" if binding.mode == "shared" else "本会话的上下文"
        await event.send(event.plain_result(f"已在{scope}打了断点。旧内容仍保留，可在管理面板里撤销。"))

    # ================= LLM 钩子 =================

    @filter.on_llm_request()
    async def inject_dynamic_persona(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入人格记忆，并把这次请求绑定到对应的记忆库。"""
        if not (self._ready and self.enabled):
            return
        await self.router.inject_dynamic_persona(event, req)
        persona = getattr(event, "_personaflow_persona_id", None)
        self.sessions.touch(event, persona=str(persona) if persona else None)
        memory = getattr(req, "_personaflow_memory", None)
        self.bus.event(
            "route",
            f"人格 {persona or '未选择'} → "
            + (f"档案 {memory.config.get('profile_id')}" if memory else "未匹配档案"),
            level="DEBUG",
            session=str(event.unified_msg_origin),
            persona=str(persona or ""),
            profile=str(memory.config.get("profile_id") or "") if memory else "",
            injected=getattr(event, "_personaflow_memory", None) is not None,
        )

    @filter.on_llm_request(priority=-100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """上下文库接管对话历史，然后给受管群注入群聊记录。"""
        if not self._ready:
            return
        sense_on = not self.old_sense_active()
        managed = sense_on and self.sense.is_managed(event)
        if self.enabled:
            profile = self.profile_of(req)
            if profile is not None:
                since = await self.sense.window_start_ts(event) if managed else None
                await self.ctx.before_request(
                    event, req, profile,
                    source=self.source_of(event),
                    managed=managed,
                    managed_since=since,
                    keep_same_group_turns=self.sense.main_keep_history_turns,
                )
        if sense_on:
            await self.sense.on_llm_request(event, req)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """把回复记进这次请求绑定的记忆库，并按阈值触发总结。"""
        if self._ready and self.enabled:
            await self.router.on_llm_response(event, resp)

    @filter.on_agent_begin()
    async def on_agent_begin(self, event: AstrMessageEvent, run_context):
        if self._ready:
            self.ctx.on_agent_begin(event, run_context)

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, resp: LLMResponse):
        if self._ready:
            await self.ctx.on_agent_done(event, run_context, resp)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """清理模仿记录格式的前缀和重复的文字 @，挂上工具暂存的引用 / @。"""
        if self._ready and not self.old_sense_active():
            await self.sense.on_decorating_result(event)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """把机器人自己发出的消息记进群聊记录。"""
        if self._ready and not self.old_sense_active():
            await self.sense.after_message_sent(event)

    # ================= 给主 LLM 的工具 =================

    @filter.llm_tool(name="quote_message")
    async def quote_message(self, event: AstrMessageEvent, message_ref: str = ""):
        """让你接下来发到群里的回复引用（回复）群聊记录里的某条消息。只在需要明确指向某条消息时使用，调用后直接写回复内容即可。

        Args:
            message_ref(string): 群聊记录中的消息编号，例如 "#123"
        """
        return await self.sense.quote_message(event, message_ref)

    @filter.llm_tool(name="mention_user")
    async def mention_user(self, event: AstrMessageEvent, user_id: str = ""):
        """让你接下来发到群里的回复开头 @ 一位群成员。只在需要点名对方时使用，调用后直接写回复内容即可。

        Args:
            user_id(string): 要 @ 的群成员 QQ 号（群聊记录里名字后面的数字）
        """
        return await self.sense.mention_user(event, user_id)

    @filter.llm_tool(name="poke_user")
    async def poke_user(self, event: AstrMessageEvent, user_id: str = ""):
        """戳一戳群里的某位成员（QQ 的「戳一戳」互动动作）。只在你想用这个动作回应对方时使用，例如对方戳了你；不要频繁使用。

        Args:
            user_id(string): 要戳的群成员 QQ 号（群聊记录里名字后面的数字）
        """
        return await self.sense.poke_user(event, user_id)

    # ================= 指令 =================

    @filter.command_group("osn")
    def osn(self):
        pass

    @osn.command("check")
    async def check_impression(self, event: AstrMessageEvent, page: int = 1):
        """分页查看当前记忆库里的人物印象"""
        async for result in self.router.check_impression(event, page):
            yield result

    @osn.command("checkmem")
    async def check_memory(self, event: AstrMessageEvent, page: int = 1):
        """分页查看当前记忆库里的全文记忆"""
        async for result in self.router.check_memory(event, page):
            yield result

    @osn.command("del")
    async def delete_memory(self, event: AstrMessageEvent, target_id: str):
        """删除某个用户在当前记忆库里的印象和对话记录"""
        async for result in self.router.delete_memory(event, target_id):
            yield result

    @osn.command("clearmem")
    async def clear_memory(self, event: AstrMessageEvent, count: int = 0):
        """删除当前记忆库最近的几条或全部全文记忆"""
        async for result in self.router.clear_memory(event, count):
            yield result

    @osn.command("profile")
    async def check_profile(self, event: AstrMessageEvent):
        """查看当前 Bot、人格、记忆库和生效范围"""
        async for result in self.router.check_profile(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("gs")
    async def gs_status(self, event: AstrMessageEvent, action: str = ""):
        """查看群聊感知的接管状态；/gs clear 清除本群聊天记录"""
        async for result in self.sense.gs_status(event, action):
            yield result

    @filter.command("ctx")
    async def ctx_command(self, event: AstrMessageEvent, action: str = ""):
        """查看当前会话的独立上下文；/ctx reset 从这里重新开始"""
        if action == "reset":
            await self._reset_context(event, "/ctx reset")
            return
        if action:
            yield event.plain_result("用法：/ctx 查看状态；/ctx reset 从这里重新开始")
            return
        if not event.is_admin():
            yield event.plain_result("❌ 仅 AstrBot 管理员可以使用此指令。")
            return
        binding = await self._binding_for(event)
        if binding is None:
            yield event.plain_result("当前会话没有使用独立上下文库，对话历史由 AstrBot 自己保存。")
            return
        turns = await self.ctx.preview(binding, current_umo=str(event.unified_msg_origin),
                                       is_group=bool(event.get_group_id()))
        mode = "共享池" if binding.mode == "shared" else "独立（仅本会话）"
        yield event.plain_result(
            f"上下文库：{mode}\n池：{binding.pool}\n"
            f"下次请求会带上 {len(turns)} 轮，约 {sum(t.chars for t in turns)} 字符"
        )
