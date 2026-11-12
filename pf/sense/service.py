"""群聊感知：由判断小模型接管群聊回复条件，并给主 LLM 注入完整群聊上下文。

这里是原 GroupSense 插件的主流程。它不直接注册 AstrBot 钩子，由插件入口类转发调用，
这样可以脱离框架单独构造和测试。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import Provider, ProviderRequest
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.platform.message_type import MessageType

from ..keys import CTX_TAKEOVER_KEY
from ..paths import get_data_dir
from .inject import (
    build_directive,
    normalize_mentions,
    rewrite_request,
    strip_echoed_prefix,
    trim_contexts,
)
from .judge import build_prompts, run_judge
from .media import MediaManager, model_image_max_size, prepared_images, thumbnail_data_url
from .members import MemberCache, role_label
from .models import JudgeDecision, MsgRecord, ReplyPlan
from .notice import (
    NOTICE_MARK,
    Notice,
    member_label,
    normalize_rules,
    parse_notice,
    render_notice,
)
from .persona import ImpressionSource, PersonaResolver
from .render import render_bot_chain, render_incoming, truncate
from .scheduler import Pending, Scheduler, SchedulerConfig
from .store import MessageStore
from .transcript import build_transcript, expand_content, sender_label

PLAN_KEY = "_gs_plan"
OUTGOING_KEY = "_gs_outgoing"
APPLIED_REPLY_KEY = "_gs_applied_reply"
TRIMMED_HISTORY_KEY = "_gs_trimmed_history"
POKED_KEY = "_gs_poked"
TOOL_NAMES = ("quote_message", "mention_user", "poke_user")
IDENTITY_TTL = 3600.0
CLEANUP_INTERVAL = 6 * 3600
# 机器人主动戳同一个人的最短间隔
POKE_TOOL_COOLDOWN_S = 60.0
# 可以按群单独覆盖的参数
GROUP_OVERRIDE_KEYS = (
    "judge_provider_id",
    "judge_extra_prompt",
    "judge_context_count",
    "judge_with_images",
    "main_context_count",
    "main_max_images",
)


def _num(config, key: str, default, lo, hi, integer: bool = False):
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        value = float(default)
    value = min(max(value, lo), hi)
    return int(value) if integer else value


def _id_set(values) -> set[str]:
    return {str(v).strip() for v in values or [] if str(v).strip()}


class SenseService:
    def __init__(
        self,
        context,
        config,
        *,
        impression_source: ImpressionSource | None = None,
        bus=None,
        data_dir: Path | None = None,
    ) -> None:
        self.context = context
        self.bus = bus
        self._load_config(config)

        data_dir = Path(data_dir) if data_dir else get_data_dir() / "sense"
        data_dir.mkdir(parents=True, exist_ok=True)
        media_dir = data_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.store = MessageStore(
            data_dir / "groupsense.db",
            window_size=self._window_size,
            max_per_group=self._db_max_messages,
        )
        self.media = MediaManager(
            self.store,
            media_dir,
            context,
            caption_provider_id=self._caption_provider_id,
            caption_prompt=self._caption_prompt,
        )
        self.members = MemberCache(self.store)
        self.persona = PersonaResolver(context, impression_source)
        self.scheduler = Scheduler(self._judge_batch, self._sched_cfg)
        self._ready = False
        self._conflicts_checked = False
        # group_key -> (缓存时间, QQ昵称, 群名片, 群身份)
        self._identity: dict[str, tuple[float, str, str, str]] = {}
        self._group_names: dict[str, tuple[float, str]] = {}
        self._last_decision: dict[str, str] = {}
        # 机器人被单独禁言的到期时间（unix 秒）；全员禁言单独记
        self._mute_until: dict[str, float] = {}
        self._whole_ban: dict[str, bool] = {}
        self._mute_checked: dict[str, float] = {}
        self._poke_triggered: dict[tuple[str, str], float] = {}
        self._poke_sent: dict[tuple[str, str], float] = {}
        self._cleanup_task: asyncio.Task | None = None

    # ================= 配置 =================

    def _load_config(self, c) -> None:
        """从配置读入全部参数；初始化和热更新共用。"""
        self.config = c
        self.enabled = bool(c.get("enable", True))
        self.enabled_groups = _id_set(c.get("enabled_groups"))
        self.excluded_groups = _id_set(c.get("excluded_groups"))
        scope = str(c.get("scope") or "")
        # 旧配置没有 scope：受管群列表为空表示接管全部群
        self.scope = scope if scope in ("all", "list") else ("list" if self.enabled_groups else "all")
        overrides = c.get("group_overrides")
        self.group_overrides = overrides if isinstance(overrides, dict) else {}
        self.judge_provider_id = str(c.get("judge_provider_id", "") or "")
        self.judge_context_count = _num(c, "judge_context_count", 20, 3, 200, True)
        self.judge_timeout_s = _num(c, "judge_timeout_s", 20, 3, 120)
        self.judge_max_retries = _num(c, "judge_max_retries", 1, 0, 5, True)
        self.judge_with_images = bool(c.get("judge_with_images", False))
        self.judge_persona_max_chars = _num(c, "judge_persona_max_chars", 2000, 0, 50000, True)
        self.judge_extra_prompt = str(c.get("judge_extra_prompt", "") or "")
        self.judge_fail_policy = str(c.get("judge_fail_policy", "reply_if_addressed"))
        self.main_context_count = _num(c, "main_context_count", 30, 3, 300, True)
        self.main_max_images = _num(c, "main_max_images", 3, 0, 10, True)
        self.main_keep_history_turns = _num(c, "main_keep_history_turns", 0, 0, 100, True)
        self.media_retention_days = _num(c, "media_retention_days", 7, 1, 365)
        # 群身份与群事件
        self.show_roles = bool(c.get("show_roles", True))
        self.show_titles = bool(c.get("show_titles", False))
        self.notice_rules = normalize_rules(c.get("notice_rules"))
        self.hide_recalled = str(c.get("recall_mode", "mark")) == "hide"
        self.poke_merge_s = _num(c, "poke_merge_s", 10, 0, 600)
        self.poke_cooldown_s = _num(c, "poke_cooldown_s", 30, 0, 3600)
        self.enable_poke_tool = bool(c.get("enable_poke_tool", True))
        self._sched_cfg = SchedulerConfig(
            burst_window_s=_num(c, "burst_window_s", 10, 1, 120),
            burst_threshold=_num(c, "burst_threshold", 5, 0, 100, True),
            debounce_s=_num(c, "debounce_s", 2.5, 0, 30),
            max_batch_wait_s=_num(c, "max_batch_wait_s", 8, 0, 120),
            reply_timeout_s=_num(c, "reply_timeout_s", 180, 10, 1800),
            wait_s=_num(c, "wait_before_judge_s", 3, 0, 60),
        )
        # handler 等待调度结果的上限：前一次回复 + 合并等待 + 判断 + 余量
        self._handler_wait_cap = (
            self._sched_cfg.reply_timeout_s
            + max(self._sched_cfg.max_batch_wait_s, self._sched_cfg.wait_s)
            + self.judge_timeout_s * 2
            + 60
        )
        counts = [self.judge_context_count, self.main_context_count]
        for override in self.group_overrides.values():
            if isinstance(override, dict):
                for name in ("judge_context_count", "main_context_count"):
                    try:
                        counts.append(int(override.get(name) or 0))
                    except (TypeError, ValueError):
                        pass
        self._window_size = min(max(counts), 300) + 30
        self._db_max_messages = _num(c, "db_max_messages_per_group", 3000, 100, 1000000, True)
        self._caption_provider_id = str(c.get("caption_provider_id", "") or "")
        self._caption_prompt = str(c.get("caption_prompt", "") or "")

    def apply_config(self, config) -> None:
        """热更新：重新读入参数并同步到调度器、存储和图片转述。"""
        self._load_config(config)
        self.scheduler.update_config(self._sched_cfg)
        self.store.resize(self._window_size, self._db_max_messages)
        self.media.caption_provider_id = self._caption_provider_id
        if self._caption_prompt:
            self.media.caption_prompt = self._caption_prompt
        self._conflicts_checked = False

    def _opt(self, name: str, group_key: str):
        """读取一个可按群覆盖的参数。"""
        override = self.group_overrides.get(group_key)
        if isinstance(override, dict) and override.get(name) not in (None, ""):
            value = override[name]
            if name in ("judge_context_count", "main_context_count"):
                return _num(override, name, getattr(self, name), 3, 300, True)
            if name == "main_max_images":
                return _num(override, name, self.main_max_images, 0, 10, True)
            if name == "judge_with_images":
                return bool(value)
            return str(value)
        return getattr(self, name)

    def _emit(self, category: str, message: str, *, level: str = "INFO", session: str = "", **data) -> None:
        """向插件日志总线写一条结构化事件；没有总线时什么也不做。"""
        if self.bus is None:
            return
        try:
            self.bus.event(category, message, level=level, session=session, **data)
        except Exception:
            pass

    # ================= 生命周期 =================

    async def initialize(self) -> None:
        await self.store.open()
        self._ready = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def terminate(self) -> None:
        self._ready = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
        await self.scheduler.close()
        await self.media.close()
        await self.store.close()

    # ================= 接管群消息 =================

    async def on_group_message(self, event: AstrMessageEvent):
        """记录群消息和群事件，并交给判断 LLM 决定是否由主 LLM 回复（含被 @ 的消息）。"""
        if not self._is_managed_group(event):
            return
        if not self._conflicts_checked:
            self._conflicts_checked = True
            self._check_conflicts()
        raw = event.message_obj.raw_message
        if isinstance(raw, dict) and raw.get("post_type") == "notice":
            async for result in self._on_notice(event, raw):
                yield result
            return
        if not self._takes_over(event):
            return
        bare_at = self._is_bare_at(event)
        try:
            record = await self._record_incoming(event)
        except Exception:
            logger.exception("[GroupSense] 记录群消息失败，本条交回默认流程")
            return

        # 屏蔽默认 LLM 回复（包括被 @ 的情况）。注意 AstrBot 这个 API 的名字是反的。
        event.should_call_llm(True)
        await self._ensure_group_state(event)
        if self._is_muted(record.group_key) and record.addressed:
            # 有人在叫机器人：向 QQ 核实一次禁言是否还在，防止漏收解禁事件后一直不说话
            await self._recheck_mute(event)
        if self._is_muted(record.group_key):
            self._emit(
                "judge",
                f"机器人被禁言中，#{record.alias} 只记录不判断",
                level="DEBUG",
                session=record.group_key,
                alias=record.alias,
            )
            if bare_at:
                event.stop_event()
            return
        async for result in self._judge_and_reply(event, record, bare_at):
            yield result

    async def _judge_and_reply(self, event: AstrMessageEvent, record: MsgRecord, bare_at: bool):
        """把一条记录交给调度器判断；被选为载体时由这个事件去调用主 LLM。"""
        future = self.scheduler.submit(record.group_key, record, event)
        try:
            plan = await asyncio.wait_for(future, timeout=self._handler_wait_cap)
        except asyncio.TimeoutError:
            plan = None
        if plan is None:
            if bare_at:
                # 挡住内置的"只 @ 不说话"自动回复
                event.stop_event()
            return

        try:
            conversation = await self._ensure_conversation(event)
            event.set_extra(PLAN_KEY, plan)
            prompt = expand_content(plan.target) or "[消息]"
            # handler 在 agent 跑完、回复发出后才会从 yield 返回
            yield event.request_llm(prompt=prompt, conversation=conversation)
        except Exception:
            logger.exception("[GroupSense] 触发主 LLM 回复失败")
        finally:
            await self._restore_trimmed_history(event)
            plan.done.set()
            if bare_at:
                event.stop_event()

    def is_managed_key(self, platform_id: str, group_id: str) -> bool:
        """某个 Bot 的某个群是否在受管范围内（不看插件是否就绪）。"""
        group_id = str(group_id or "")
        if not group_id:
            return False
        keys = {group_id, f"{platform_id}:{group_id}"}
        if self.scope == "all":
            return not (keys & self.excluded_groups)
        return bool(keys & self.enabled_groups)

    def _is_managed_group(self, event: AstrMessageEvent) -> bool:
        if not self._ready or not self.enabled:
            return False
        if event.get_platform_name() != "aiocqhttp":
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        return self.is_managed_key(str(event.get_platform_id()), str(event.get_group_id() or ""))

    def is_managed(self, event: AstrMessageEvent) -> bool:
        return self._is_managed_group(event)

    def _takes_over(self, event: AstrMessageEvent) -> bool:
        if not self._is_managed_group(event):
            return False
        # 命中了指令的消息交给原流程，也不记进群聊记录
        if event.get_extra("handlers_parsed_params"):
            return False
        raw = event.message_obj.raw_message
        if isinstance(raw, dict) and raw.get("post_type", "message") != "message":
            return False
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return False
        return bool(event.get_messages())

    @staticmethod
    def _is_bare_at(event: AstrMessageEvent) -> bool:
        messages = event.get_messages()
        return (
            len(messages) == 1
            and isinstance(messages[0], Comp.At)
            and str(messages[0].qq) == str(event.get_self_id())
        )

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        return f"{event.get_platform_id()}:{event.get_group_id()}"

    async def _record_incoming(self, event: AstrMessageEvent) -> MsgRecord:
        key = self._group_key(event)
        self_id = str(event.get_self_id())
        raw = event.message_obj.raw_message
        raw = raw if isinstance(raw, dict) else {}
        segments = raw.get("message") if isinstance(raw.get("message"), list) else None
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        try:
            ts = float(raw.get("time"))
        except (TypeError, ValueError):
            ts = time.time()

        async def resolve_alias(msg_id: str, sender_id: str, quoted: str) -> int | None:
            rec = await self.store.find_by_msg_id(key, msg_id)
            if rec:
                return rec.alias
            # bot 自己的消息拿不到真实 message_id，按文本匹配最近的 bot 发言
            probe = " ".join(quoted.split())[:20]
            if sender_id == self_id and probe:
                for r in reversed(await self.store.window(key, 60)):
                    if r.is_bot and probe in " ".join(r.content.split()):
                        return r.alias
            return None

        result = await render_incoming(
            segments,
            list(event.get_messages()),
            self_id,
            resolve_alias=resolve_alias,
            ingest_media=self.media.ingest,
        )
        sender_id = str(event.get_sender_id())
        nickname = str(sender.get("nickname") or event.get_sender_name() or "")
        card = str(sender.get("card") or "")
        role = str(sender.get("role") or "")
        title = str(sender.get("title") or "")
        await self.members.observe(
            key,
            sender_id,
            nickname=nickname,
            card=card,
            # 协议端没给身份时不要把已知身份冲掉
            role=role or None,
            title=title if "title" in sender else None,
        )
        record = MsgRecord(
            group_key=key,
            msg_id=str(event.message_obj.message_id or ""),
            sender_id=sender_id,
            nickname=nickname,
            card=card,
            is_bot=False,
            ts=ts,
            content=result.content,
            at_bot=result.at_bot,
            reply_bot=result.reply_bot,
            media=result.media,
            role=role,
            title=title,
        )
        return await self.store.add(record)

    # ================= 群事件 =================

    async def _on_notice(self, event: AstrMessageEvent, raw: dict):
        """处理一条群事件：更新状态、记入群聊记录，需要时交给判断模型。"""
        key = self._group_key(event)
        self_id = str(event.get_self_id())
        notice = parse_notice(raw, self_id)
        if notice is None:
            return
        rule = self.notice_rules.get(notice.category, {"record": True, "trigger": False})
        record = None
        try:
            await self._apply_notice_state(event, key, notice)
            if rule["record"]:
                record = await self._record_notice(event, key, notice)
        except Exception:
            logger.exception("[GroupSense] 处理群事件失败")
        finally:
            # 退群的成员要等事件渲染完（还需要他的名字）再移出缓存
            if notice.kind in ("leave", "kick"):
                await self.members.remove(key, notice.target_id)
                if notice.target_id == self_id:
                    await self.scheduler.drop(key)
        if record is None or not self._should_trigger(key, notice, rule, self_id):
            return
        event.should_call_llm(True)
        async for result in self._judge_and_reply(event, record, False):
            yield result

    async def _apply_notice_state(self, event: AstrMessageEvent, key: str, notice: Notice) -> None:
        """根据群事件更新禁言状态、成员缓存和撤回标记。无论是否记入群聊记录都要做。"""
        self_id = str(event.get_self_id())
        kind = notice.kind
        if kind == "ban":
            if notice.data.get("whole"):
                self._whole_ban[key] = True
            elif notice.target_id == self_id:
                self._mute_until[key] = notice.ts + int(notice.data.get("duration") or 0)
        elif kind == "lift_ban":
            if notice.data.get("whole"):
                self._whole_ban[key] = False
            elif notice.target_id == self_id:
                self._mute_until.pop(key, None)
        elif kind in ("admin_set", "admin_unset"):
            role = "admin" if kind == "admin_set" else "member"
            await self.members.observe(key, notice.target_id, role=role)
            if notice.target_id == self_id:
                self._set_identity(key, role=role)
                if role == "admin":
                    # 被设为管理员时 QQ 会同时解除禁言，但不一定推送解禁事件
                    self._mute_until.pop(key, None)
        elif kind == "card":
            card = str(notice.data.get("card_new") or "")
            await self.members.observe(key, notice.target_id, card=card)
            if notice.target_id == self_id:
                self._set_identity(key, card=card)
        elif kind == "title":
            await self.members.observe(
                key, notice.target_id, title=str(notice.data.get("title") or "")
            )
        elif kind == "recall":
            rec = await self.store.mark_recalled(key, str(notice.data.get("message_id") or ""))
            if rec is not None:
                notice.data["alias"] = rec.alias
                if self.scheduler.cancel(key, rec.alias):
                    self._emit(
                        "judge",
                        f"#{rec.alias} 在判断前被撤回，已从待判队列移除",
                        session=key,
                        alias=rec.alias,
                    )

    async def _notice_label(self, event: AstrMessageEvent, key: str, user_id: str) -> str:
        if not user_id:
            return ""
        self_id = str(event.get_self_id())
        if user_id == self_id:
            return "你"
        member = await self.members.resolve(
            key, user_id, getattr(event, "bot", None), str(event.get_group_id() or "")
        )
        return member_label(member, user_id, self_id, self.show_roles)

    async def _record_notice(self, event: AstrMessageEvent, key: str, notice: Notice) -> MsgRecord | None:
        """把群事件写成群聊记录里的一行；返回 None 表示这次不需要新记录。"""
        self_id = str(event.get_self_id())
        actor = await self._notice_label(event, key, notice.actor_id) or "管理员"
        target = await self._notice_label(event, key, notice.target_id)
        if notice.kind == "poke":
            recent = await self.store.window(key, 5)
            last = recent[-1] if recent else None
            same = (
                last is not None
                and last.is_notice
                and last.extra.get("notice") == "poke"
                and last.extra.get("actor_id") == notice.actor_id
                and last.extra.get("target_id") == notice.target_id
            )
            # 机器人用工具戳人后，协议端会把这次戳一戳回推回来，不重复记
            if notice.actor_id == self_id and any(
                r.is_notice
                and r.extra.get("notice") == "poke"
                and r.extra.get("actor_id") == self_id
                and r.extra.get("target_id") == notice.target_id
                and abs(notice.ts - r.ts) <= 30
                for r in recent
            ):
                return None
            if same and self.poke_merge_s > 0 and notice.ts - last.ts <= self.poke_merge_s:
                count = int(last.extra.get("count") or 1) + 1
                notice.data["count"] = count
                last.extra["count"] = count
                last.ts = notice.ts
                last.content = render_notice(notice, actor=actor, target=target)
                await self.store.update(last)
                return last
        content = render_notice(
            notice, actor=actor, target=target, recalled_alias=notice.data.get("alias")
        )
        record = await self.store.add(
            MsgRecord(
                group_key=key,
                msg_id="",
                sender_id=notice.actor_id or notice.target_id,
                nickname="",
                card="",
                is_bot=False,
                ts=notice.ts,
                content=content,
                kind="notice",
                extra=notice.to_extra(),
            )
        )
        logger.info(f"[GroupSense] {key} 群事件 #{record.alias}: {content}")
        self._emit(
            "notice",
            content,
            session=key,
            alias=record.alias,
            kind=notice.kind,
            actor_id=notice.actor_id,
            target_id=notice.target_id,
        )
        return record

    def _should_trigger(self, key: str, notice: Notice, rule: dict, self_id: str) -> bool:
        """这次群事件要不要交给判断模型。"""
        if not rule.get("trigger"):
            return False
        # 机器人自己做的动作、机器人被移出群，都没有可回应的对象
        if notice.actor_id == self_id:
            return False
        if notice.kind == "kick" and notice.target_id == self_id:
            return False
        if self._is_muted(key):
            return False
        if notice.kind == "poke":
            # 只有戳机器人才算在叫它；自己的戳一戳不触发
            if not notice.data.get("poke_bot"):
                return False
            now = time.monotonic()
            last = self._poke_triggered.get((key, notice.actor_id))
            if last is not None and self.poke_cooldown_s > 0 and now - last < self.poke_cooldown_s:
                return False
            self._poke_triggered[(key, notice.actor_id)] = now
            return True
        return True

    def _is_muted(self, key: str) -> bool:
        """机器人此刻在这个群里能不能说话。"""
        until = self._mute_until.get(key)
        if until is not None:
            if until > time.time():
                return True
            self._mute_until.pop(key, None)
        if self._whole_ban.get(key):
            cached = self._identity.get(key)
            role = cached[3] if cached else ""
            return role not in ("owner", "admin")
        return False

    async def _recheck_mute(self, event: AstrMessageEvent) -> None:
        """向 QQ 查询机器人当前的禁言到期时间，纠正本地状态；同一个群 30 秒内只查一次。"""
        key = self._group_key(event)
        now = time.monotonic()
        if now - self._mute_checked.get(key, -1e9) < 30:
            return
        self._mute_checked[key] = now
        bot = getattr(event, "bot", None)
        if bot is None or key not in self._mute_until:
            return
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(event.get_group_id()),
                user_id=int(event.get_self_id()),
                no_cache=True,
            )
            shut_up = float(info.get("shut_up_timestamp") or 0)
        except Exception as e:
            logger.debug(f"[GroupSense] 核实禁言状态失败: {e}")
            return
        if shut_up > time.time():
            self._mute_until[key] = shut_up
        else:
            self._mute_until.pop(key, None)
            self._emit("notice", "向 QQ 核实后发现禁言已解除，恢复判断", session=key)

    def mute_state(self, key: str) -> dict:
        """给面板和 /gs 显示的禁言状态。"""
        until = self._mute_until.get(key) or 0
        return {
            "muted": self._is_muted(key),
            "mute_until": until if until > time.time() else 0,
            "whole_ban": bool(self._whole_ban.get(key)),
        }

    async def _ensure_group_state(self, event: AstrMessageEvent) -> None:
        """首次遇到某个群时取一次机器人的身份和禁言状态（之后靠群事件维护）。"""
        key = self._group_key(event)
        if key not in self._identity:
            await self._bot_identity(event)

    # ================= 判断 =================

    async def _judge_batch(self, key: str, batch: list[Pending]) -> ReplyPlan | None:
        event = batch[-1].event
        started = time.monotonic()
        batch_aliases = [p.record.alias for p in batch]
        judge_context_count = self._opt("judge_context_count", key)
        judge_provider_id = self._opt("judge_provider_id", key)
        judge_with_images = self._opt("judge_with_images", key)
        records = await self.store.window(key, judge_context_count)
        known = {r.alias for r in records}
        records = sorted(
            records + [p.record for p in batch if p.record.alias not in known],
            key=lambda r: r.alias,
        )
        pending = set(batch_aliases)
        persona = await self.persona.get_persona(event)
        group_id = str(event.get_group_id())
        impressions = await self.persona.get_impressions(
            [r.sender_id for r in records if not r.is_bot and r.sender_id],
            event=event,
        )
        captions = await self.store.get_captions([m.md5 for r in records for m in r.media])
        transcript = build_transcript(
            records,
            captions=captions,
            image_slots=3 if judge_with_images else 0,
            image_aliases=pending,
            pending=pending,
            show_roles=self.show_roles,
            show_titles=self.show_titles,
            hide_recalled=self.hide_recalled,
        )
        group_name = await self._group_name(event)
        _, _, self_role = await self._bot_identity_full(event)

        provider = (
            self.context.get_provider_by_id(judge_provider_id) if judge_provider_id else None
        )
        if not isinstance(provider, Provider):
            decision = JudgeDecision(failed=True, reason="未配置可用的判断模型")
        else:
            system, user = build_prompts(
                persona_name=persona.name,
                persona_prompt=persona.prompt,
                persona_max_chars=self.judge_persona_max_chars,
                self_id=str(event.get_self_id()),
                impressions=impressions,
                extra_prompt=self._opt("judge_extra_prompt", key),
                group_name=f"{group_name}({group_id})",
                transcript=transcript.text,
                self_role=self_role if self.show_roles else "",
                show_roles=self.show_roles,
            )
            # 和主 LLM 一样用 AstrBot 的图片预处理：按最长边缩放，动图转 3x3 帧拼图
            async with prepared_images(
                [img.path for img in transcript.images],
                model_image_max_size(self.context, event.unified_msg_origin),
                self.media.work_dir,
            ) as prepared:
                attached = [
                    (img.index, p) for img, p in zip(transcript.images, prepared) if p
                ]
                if attached:
                    labels = "、".join(f"[图片#{i}]" for i, _ in attached)
                    user += f"\n（附带的图片依次对应记录里的 {labels}）"
                    montages = [f"[图片#{i}]" for i, p in attached if p.montage]
                    if montages:
                        user += (
                            f"\n（{'、'.join(montages)} 是动图，已按阅读顺序拼成 3×3 的帧图）"
                        )
                decision = await run_judge(
                    provider,
                    system,
                    user,
                    image_urls=[p.path for _, p in attached],
                    window_aliases={r.alias for r in records},
                    timeout_s=self.judge_timeout_s,
                    max_retries=self.judge_max_retries,
                )
            if self.bus is not None and getattr(self.bus, "save_prompts", False):
                self._emit(
                    "judge",
                    "判断模型提示词",
                    level="DEBUG",
                    session=key,
                    system_prompt=system,
                    user_prompt=user,
                )
        judge_failed = decision.failed
        if decision.failed:
            decision = self._fallback_decision(batch, decision.reason)

        summary = (
            f"{'回复 #' + str(decision.target) if decision.should_reply else '沉默'}"
            f" | {decision.reason or '-'}"
        )
        self._last_decision[key] = f"{time.strftime('%H:%M:%S')} 判断 {len(batch)} 条 → {summary}"
        logger.info(f"[GroupSense] {key} 判断 {len(batch)} 条{batch_aliases} → {summary}")
        self._emit(
            "judge",
            f"判断 {len(batch)} 条 → {summary}",
            level="WARNING" if judge_failed else "INFO",
            session=key,
            batch=batch_aliases,
            action=decision.action,
            target=decision.target,
            reason=decision.reason,
            failed=judge_failed,
            latency_ms=int((time.monotonic() - started) * 1000),
            model=judge_provider_id,
            persona=persona.persona_id,
            impressions=len(impressions),
        )
        if not decision.should_reply:
            return None

        target = next((r for r in records if r.alias == decision.target), None)
        if target is None or target.is_bot:
            batch_records = [p.record for p in batch]
            target = next((r for r in reversed(batch_records) if r.addressed), batch_records[-1])
        return ReplyPlan(group_key=key, target=target, batch_aliases=batch_aliases)

    def _fallback_decision(self, batch: list[Pending], reason: str) -> JudgeDecision:
        if self.judge_fail_policy == "reply_if_addressed":
            addressed = [p.record for p in batch if p.record.addressed]
            if addressed:
                return JudgeDecision(
                    action="reply",
                    target=addressed[-1].alias,
                    reason=f"判断失败（{reason}），按策略回复叫了你的消息",
                )
        return JudgeDecision(action="silent", reason=f"判断失败（{reason}），保持沉默")

    # ================= 主 LLM =================

    async def _ensure_conversation(self, event: AstrMessageEvent):
        conv_mgr = self.context.conversation_manager
        umo = event.unified_msg_origin
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            cid = await conv_mgr.new_conversation(umo, platform_id=event.get_platform_id())
        return await conv_mgr.get_conversation(umo, cid)

    async def _restore_trimmed_history(self, event: AstrMessageEvent) -> None:
        """把注入时裁掉、没发给模型的旧对话接回 AstrBot 的对话记录。

        AstrBot 保存的是"发给模型的上下文 + 本轮对话"，裁剪后历史会被截短。
        只有本轮确实按裁剪后的内容保存了才接回；调用失败没保存时记录本来就是完整的。
        """
        info = event.get_extra(TRIMMED_HISTORY_KEY)
        if not isinstance(info, dict):
            return
        event.set_extra(TRIMMED_HISTORY_KEY, None)
        try:
            conv_mgr = self.context.conversation_manager
            umo = event.unified_msg_origin
            conv = await conv_mgr.get_conversation(umo, info["cid"])
            if conv is None:
                return
            current = json.loads(conv.history or "[]")
            kept = info["kept"]
            if not isinstance(current, list) or current == info["full"]:
                return

            # AstrBot 保存时会重新序列化消息（可能多出 None 字段），只比较角色序列
            def roles(items):
                return [m.get("role") if isinstance(m, dict) else None for m in items]

            if len(current) <= len(kept) or roles(current[: len(kept)]) != roles(kept):
                logger.debug("[GroupSense] 对话记录已被其他流程改动，不接回旧历史")
                return
            await conv_mgr.update_conversation(
                umo, info["cid"], history=info["dropped"] + current
            )
        except Exception as e:
            logger.warning(f"[GroupSense] 接回对话历史失败: {e}")

    def _outgoing(self, event: AstrMessageEvent) -> dict:
        outgoing = event.get_extra(OUTGOING_KEY)
        if not isinstance(outgoing, dict):
            outgoing = {"reply": "", "reply_alias": 0, "at": []}
            event.set_extra(OUTGOING_KEY, outgoing)
        return outgoing

    def _main_supports_images(self, event: AstrMessageEvent) -> bool:
        provider = None
        selected = event.get_extra("selected_provider")
        if selected:
            provider = self.context.get_provider_by_id(selected)
        if provider is None:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if provider is None:
            return False
        modalities = provider.provider_config.get("modalities", None)
        return modalities == [] or (isinstance(modalities, list) and "image" in modalities)

    async def window_start_ts(self, event: AstrMessageEvent) -> float | None:
        """主 LLM 这次能看到的群聊记录里最早一条的时间；没有记录返回 None。

        上下文库据此跳过已经在群聊记录里的本群轮次。
        """
        key = self._group_key(event)
        records = await self.store.window(key, self._opt("main_context_count", key))
        return records[0].ts if records else None

    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """给受管群的主 LLM 请求注入群聊记录、图片和交接指令。"""
        if not self._is_managed_group(event):
            if req.func_tool:
                for name in TOOL_NAMES:
                    req.func_tool.remove_tool(name)
            return
        if req.func_tool and not self.enable_poke_tool:
            req.func_tool.remove_tool("poke_user")
        key = self._group_key(event)
        records = await self.store.window(key, self._opt("main_context_count", key))
        captions = await self.store.get_captions([m.md5 for r in records for m in r.media])
        transcript = build_transcript(
            records,
            captions=captions,
            image_slots=self._opt("main_max_images", key) if self._main_supports_images(event) else 0,
            show_roles=self.show_roles,
            show_titles=self.show_titles,
            hide_recalled=self.hide_recalled,
        )
        group_name = await self._group_name(event)
        _, _, self_role = await self._bot_identity_full(event)
        plan = event.get_extra(PLAN_KEY)
        directive = None
        if isinstance(plan, ReplyPlan):
            directive = build_directive(
                plan,
                group_name=group_name,
                captions=captions,
                show_roles=self.show_roles,
                show_titles=self.show_titles,
            )
            # 独立上下文库接管时，历史由它装载，这里不再裁剪 AstrBot 的对话历史
            takeover = bool(event.get_extra(CTX_TAKEOVER_KEY))
            if not takeover and req.conversation is not None and isinstance(req.contexts, list):
                try:
                    full_history = json.loads(req.conversation.history or "[]")
                except (TypeError, ValueError):
                    full_history = None
                if isinstance(full_history, list):
                    n_begin = max(0, len(req.contexts) - len(full_history))
                    history = req.contexts[n_begin:]
                    req.contexts = trim_contexts(
                        req.contexts, n_begin, self.main_keep_history_turns
                    )
                    kept = req.contexts[n_begin:]
                    dropped = history[: len(history) - len(kept)]
                    if dropped:
                        # 只是不发给模型；回复保存后再接回对话记录，WebUI 仍能看到完整历史
                        event.set_extra(
                            TRIMMED_HISTORY_KEY,
                            {"cid": req.conversation.cid, "dropped": dropped,
                             "full": full_history, "kept": kept},
                        )
        rewrite_request(
            req,
            transcript=transcript,
            group_name=group_name,
            directive=directive,
            show_roles=self.show_roles,
            self_role=self_role,
        )
        logger.debug(
            f"[GroupSense] 注入主 LLM 请求 {key}: {len(records)} 条记录, "
            f"{len(transcript.images)} 张图, 交接指令={'有' if directive else '无'}"
        )
        self._emit(
            "inject",
            f"注入群聊记录 {len(records)} 条、图片 {len(transcript.images)} 张",
            session=key,
            records=len(records),
            images=len(transcript.images),
            directive=directive or "",
            target=plan.target.alias if isinstance(plan, ReplyPlan) else None,
        )

    async def on_decorating_result(self, event: AstrMessageEvent):
        """清理模仿记录格式的前缀和重复的文字 @，挂上工具暂存的引用 / @。"""
        if not self._is_managed_group(event):
            return
        result = event.get_result()
        if result is None or not result.chain or not result.is_llm_result():
            return
        nick, card = await self._bot_identity(event)
        self_id = event.get_self_id()
        labels = [f"{nick}({card}){self_id}", f"{nick}(){self_id}"]
        for comp in result.chain:
            if isinstance(comp, Comp.Plain):
                stripped = strip_echoed_prefix(comp.text, labels)
                if stripped.strip():
                    comp.text = stripped
                break
        outgoing = event.get_extra(OUTGOING_KEY)
        outgoing = outgoing if isinstance(outgoing, dict) else {}
        key = self._group_key(event)
        records = await self.store.window(key, self._opt("main_context_count", key))
        names: dict[str, set[str]] = {}
        for rec in records:
            if not rec.is_bot and not rec.is_notice:
                names.setdefault(rec.sender_id, set()).update(
                    n for n in (rec.card, rec.nickname) if n
                )
        # 文字 "@名字(QQ号)" 转成真 @，去掉与真 @ 重复的文字 @，并挂上工具暂存的 @
        chain = normalize_mentions(
            result.chain,
            staged_at=list(outgoing.get("at", [])),
            names=names,
            self_id=str(event.get_self_id()),
        )
        if outgoing.get("reply"):
            chain.insert(0, Comp.Reply(id=outgoing["reply"]))
            event.set_extra(APPLIED_REPLY_KEY, outgoing.get("reply_alias", 0))
        result.chain[:] = chain
        # 工具暂存的引用/@ 只挂在接下来的第一条回复上；之后再暂存的挂到下一条
        event.set_extra(OUTGOING_KEY, {"reply": "", "reply_alias": 0, "at": []})

    async def after_message_sent(self, event: AstrMessageEvent):
        """把 bot 自己发出的消息记进群聊记录。"""
        if not self._is_managed_group(event):
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        if result.result_content_type == ResultContentType.AGENT_RUNNER_ERROR:
            return
        if not result.is_model_result() and event.get_extra("handlers_parsed_params"):
            return  # 指令输出不记
        key = self._group_key(event)
        records = await self.store.window(key, self._opt("main_context_count", key))
        reply_prefix = ""
        reply_alias = event.get_extra(APPLIED_REPLY_KEY)
        if reply_alias:
            reply_prefix = f"[引用 #{reply_alias}] "
            event.set_extra(APPLIED_REPLY_KEY, 0)
        else:
            reply_comp = next((c for c in result.chain if isinstance(c, Comp.Reply)), None)
            if reply_comp is not None:
                rec = await self.store.find_by_msg_id(key, str(reply_comp.id))
                reply_prefix = f"[引用 #{rec.alias}] " if rec else "[引用了一条消息] "
        content = render_bot_chain(
            result.chain, lambda qq: self._name_of(records, qq), reply_prefix
        )
        if not content.strip():
            return
        nick, card, role = await self._bot_identity_full(event)
        record = await self.store.add(
            MsgRecord(
                group_key=key,
                msg_id="",
                sender_id=str(event.get_self_id()),
                nickname=nick,
                card=card,
                is_bot=True,
                ts=time.time(),
                content=content,
                role=role,
            )
        )
        self._emit(
            "reply",
            f"机器人发言 #{record.alias}: {truncate(content, 60)}",
            session=key,
            alias=record.alias,
            content=content,
        )

    # ================= 给主 LLM 的工具 =================

    async def quote_message(self, event: AstrMessageEvent, message_ref: str = ""):
        """让接下来发到群里的回复引用群聊记录里的某条消息。"""
        if not self._is_managed_group(event):
            return "当前不在受管群聊中，无法引用消息。"
        match = re.search(r"\d+", message_ref or "")
        if not match:
            return "请提供群聊记录里的消息编号，例如 #123。"
        rec = await self.store.find_by_alias(self._group_key(event), int(match.group()))
        if rec is None:
            return f"找不到消息 #{match.group()}。"
        if rec.is_notice:
            return f"#{rec.alias} 是群事件，不是聊天消息，无法引用。"
        if rec.is_bot or not rec.msg_id:
            return f"#{rec.alias} 是你自己发的消息，无法引用。"
        if rec.recalled:
            return f"#{rec.alias} 已经被撤回，无法引用。"
        outgoing = self._outgoing(event)
        outgoing["reply"] = rec.msg_id
        outgoing["reply_alias"] = rec.alias
        label = sender_label(rec, show_roles=self.show_roles, show_titles=self.show_titles)
        return (
            f"好的，你接下来发出的回复会引用 #{rec.alias}"
            f"（{label}: {truncate(expand_content(rec), 30)}）。现在直接写回复内容。"
        )

    async def mention_user(self, event: AstrMessageEvent, user_id: str = ""):
        """让接下来发到群里的回复开头 @ 一位群成员。"""
        if not self._is_managed_group(event):
            return "当前不在受管群聊中，无法 @ 别人。"
        match = re.search(r"\d{5,12}", user_id or "")
        if not match:
            return "请提供要 @ 的人的 QQ 号。"
        uid = match.group()
        if uid == str(event.get_self_id()):
            return "不能 @ 你自己。"
        outgoing = self._outgoing(event)
        if uid not in outgoing["at"]:
            outgoing["at"] = (outgoing["at"] + [uid])[-5:]
        key = self._group_key(event)
        records = await self.store.window(key, self._opt("main_context_count", key))
        return f"好的，你接下来发出的回复开头会 @{self._name_of(records, uid)}({uid})。现在直接写回复内容。"

    async def poke_user(self, event: AstrMessageEvent, user_id: str = ""):
        """戳一戳群里的某位成员。"""
        if not self._is_managed_group(event):
            return "当前不在受管群聊中，无法戳一戳。"
        if not self.enable_poke_tool:
            return "戳一戳功能没有开启。"
        match = re.search(r"\d{5,12}", user_id or "")
        if not match:
            return "请提供要戳的人的 QQ 号。"
        uid = match.group()
        self_id = str(event.get_self_id())
        if uid == self_id:
            return "不能戳你自己。"
        key = self._group_key(event)
        if self._is_muted(key):
            return "你现在被禁言了，戳不了。"
        if event.get_extra(POKED_KEY):
            return "这一轮你已经戳过一次了，不要连续戳。"
        now = time.monotonic()
        last = self._poke_sent.get((key, uid))
        if last is not None and now - last < POKE_TOOL_COOLDOWN_S:
            return "你刚戳过这个人，过一会儿再戳。"
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前平台不支持戳一戳。"
        group_id = int(event.get_group_id())
        try:
            try:
                await bot.call_action("group_poke", group_id=group_id, user_id=int(uid))
            except Exception:
                await bot.call_action("send_poke", group_id=group_id, user_id=int(uid))
        except Exception as e:
            logger.warning(f"[GroupSense] 戳一戳失败: {e}")
            return "戳一戳没有成功。"
        event.set_extra(POKED_KEY, True)
        self._poke_sent[(key, uid)] = now
        member = await self.members.resolve(key, uid, bot, str(group_id))
        target = member_label(member, uid, self_id, self.show_roles)
        notice = Notice(
            kind="poke",
            group_id=str(group_id),
            actor_id=self_id,
            target_id=uid,
            ts=time.time(),
            data={"action": "戳了戳", "suffix": ""},
        )
        content = render_notice(notice, actor="你", target=target)
        record = await self.store.add(
            MsgRecord(
                group_key=key,
                msg_id="",
                sender_id=self_id,
                nickname="",
                card="",
                is_bot=False,
                ts=notice.ts,
                content=content,
                kind="notice",
                extra=notice.to_extra(),
            )
        )
        self._emit("notice", content, session=key, alias=record.alias, kind="poke", by_tool=True)
        return f"好的，你戳了戳 {target}。如果还想说话，直接写回复内容。"

    # ================= 管理指令 =================

    async def gs_status(self, event: AstrMessageEvent, action: str = ""):
        """查看群聊接管状态；/gs clear 清除本群聊天记录"""
        if action == "clear":
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                yield event.plain_result("请在要清除的群里发送 /gs clear")
                return
            n = await self.store.clear_group(self._group_key(event))
            logger.info(f"[GroupSense] {self._group_key(event)} 已清除 {n} 条聊天记录")
            yield event.plain_result(
                f"已清除本群 {n} 条聊天记录。主 LLM 的对话历史不受影响，需要的话用 /reset 清除。"
            )
            return
        if action:
            yield event.plain_result("用法：/gs 查看状态；/gs clear 清除本群聊天记录")
            return
        lines = ["GroupSense 状态"]
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            if self.scope == "all":
                scope = "全部群" + (
                    f"（排除 {', '.join(sorted(self.excluded_groups))}）"
                    if self.excluded_groups
                    else ""
                )
            else:
                scope = ", ".join(sorted(self.enabled_groups)) or "无"
            lines.append(f"已启用: {self.enabled}，受管群: {scope}")
        else:
            key = self._group_key(event)
            worker = self.scheduler.workers.get(key)
            lines += [
                f"本群受管: {'是' if self._is_managed_group(event) else '否'}",
                f"判断模型: {self._opt('judge_provider_id', key) or '未配置'}",
                f"已存消息: {await self.store.count(key)} 条",
            ]
            if worker:
                lines.append(
                    f"调度: {worker.state}，待判 {worker.pending_count} 条，"
                    f"已判 {worker.batches} 批（合并 {worker.merged_batches}），回复 {worker.replies} 次"
                )
            if self._is_managed_group(event):
                _, _, role = await self._bot_identity_full(event)
                lines.append(f"机器人身份: {role_label(role) or '普通群员'}")
                state = self.mute_state(key)
                if state["muted"]:
                    if state["mute_until"]:
                        until = time.strftime("%H:%M:%S", time.localtime(state["mute_until"]))
                        lines.append(f"禁言中，到 {until} 解除；期间不调用判断模型")
                    else:
                        lines.append("全员禁言中；期间不调用判断模型")
            lines.append(f"最近一次判断: {self._last_decision.get(key, '无')}")
        yield event.plain_result("\n".join(lines))

    # ================= 杂项 =================

    @staticmethod
    def _name_of(records: list[MsgRecord], uid: str) -> str:
        for rec in reversed(records):
            if rec.sender_id == uid and not rec.is_notice:
                return rec.card or rec.nickname or uid
        return uid

    def _set_identity(self, key: str, *, card: str | None = None, role: str | None = None) -> None:
        cached = self._identity.get(key)
        if cached is None:
            return
        ts, nick, old_card, old_role = cached
        self._identity[key] = (
            ts,
            nick,
            old_card if card is None else card,
            old_role if role is None else role,
        )

    async def _bot_identity_full(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        """bot 在本群的 (QQ昵称, 群名片, 群身份)，缓存一小时。

        同一次查询还带回禁言到期时间，用来在插件刚启动时恢复禁言状态。
        """
        key = self._group_key(event)
        cached = self._identity.get(key)
        if cached and time.monotonic() - cached[0] < IDENTITY_TTL:
            return cached[1], cached[2], cached[3]
        nick, card, role = "", "", (cached[3] if cached else "")
        bot = getattr(event, "bot", None)
        if bot is not None:
            try:
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(event.get_group_id()),
                    user_id=int(event.get_self_id()),
                    no_cache=False,
                )
                nick = str(info.get("nickname") or "")
                card = str(info.get("card") or "")
                role = str(info.get("role") or role)
                try:
                    shut_up = float(info.get("shut_up_timestamp") or 0)
                except (TypeError, ValueError):
                    shut_up = 0.0
                if shut_up > time.time() and key not in self._mute_until:
                    self._mute_until[key] = shut_up
            except Exception as e:
                logger.debug(f"[GroupSense] 获取 bot 群名片失败: {e}")
        nick = nick or "bot"
        self._identity[key] = (time.monotonic(), nick, card, role)
        return nick, card, role

    async def _bot_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        """bot 在本群的 (QQ昵称, 群名片)。"""
        nick, card, _ = await self._bot_identity_full(event)
        return nick, card

    async def _group_name(self, event: AstrMessageEvent) -> str:
        key = self._group_key(event)
        cached = self._group_names.get(key)
        if cached and time.monotonic() - cached[0] < IDENTITY_TTL:
            return cached[1]
        group_id = str(event.get_group_id())
        name = ""
        group = getattr(event.message_obj, "group", None)
        if group is not None:
            name = str(getattr(group, "group_name", "") or "")
        bot = getattr(event, "bot", None)
        if not name and bot is not None:
            try:
                info = await bot.call_action("get_group_info", group_id=int(group_id))
                name = str(info.get("group_name") or "")
            except Exception as e:
                logger.debug(f"[GroupSense] 获取群名失败: {e}")
        name = name or group_id
        self._group_names[key] = (time.monotonic(), name)
        return name

    def group_name_cached(self, key: str) -> str:
        cached = self._group_names.get(key)
        return cached[1] if cached else ""

    def conflicts(self) -> list[str]:
        """检查会和群聊感知打架的 AstrBot 设置和插件，返回给人看的提示。"""
        issues: list[str] = []
        cfg = self.context.get_config()
        ltm = cfg.get("provider_ltm_settings", {}) or {}
        if ltm.get("group_icl_enable"):
            issues.append(
                "内置「群聊消息记录注入上下文」已开启，会与群聊感知重复注入，请在 AstrBot 配置里关闭"
            )
        if (ltm.get("active_reply") or {}).get("enable"):
            issues.append("内置「主动回复」已开启，会绕过判断模型，请关闭")
        platform_settings = cfg.get("platform_settings", {}) or {}
        if platform_settings.get("reply_with_mention") or platform_settings.get("reply_with_quote"):
            issues.append(
                "内置「回复时@发送者 / 引用消息」已开启。主模型决定不引用、不@时，"
                "AstrBot 仍会自动加上，建议关闭，交给群聊感知决定"
            )
        heartflow = self.context.get_registered_star("astrbot_plugin_Heartflow")
        if heartflow and getattr(heartflow, "activated", False):
            hf_cfg = getattr(heartflow.star_cls, "config", None) or {}
            if hf_cfg.get("enable_heartflow"):
                issues.append("心流插件已启用，两者会同时决定回复，请关闭心流")
        if not self.judge_provider_id:
            issues.append(
                "未配置判断模型，所有消息将按判断失败策略处理"
                f"（当前: {self.judge_fail_policy}）"
            )
        return issues

    def _check_conflicts(self) -> None:
        try:
            for issue in self.conflicts():
                logger.warning(f"[GroupSense] {issue}")
        except Exception as e:
            logger.debug(f"[GroupSense] 冲突检查失败: {e}")

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                removed = await self.media.cleanup(self.media_retention_days)
                if removed:
                    logger.info(f"[GroupSense] 清理过期图片 {removed} 张")
            except Exception as e:
                logger.warning(f"[GroupSense] 清理图片失败: {e}")
            await asyncio.sleep(CLEANUP_INTERVAL)

    # ================= 管理面板 =================

    async def panel_groups(self) -> list[dict]:
        """每个有记录的群的概况。"""
        groups = []
        for item in await self.store.list_groups():
            key = item["group_key"]
            platform_id, _, group_id = key.rpartition(":")
            worker = self.scheduler.workers.get(key)
            groups.append(
                {
                    **item,
                    "platform_id": platform_id,
                    "group_id": group_id,
                    "name": self.group_name_cached(key),
                    "managed": self.enabled and self.is_managed_key(platform_id, group_id),
                    "last_decision": self._last_decision.get(key, ""),
                    "override": self.group_overrides.get(key) or {},
                    "bot_role": (self._identity.get(key) or (0, "", "", ""))[3],
                    "worker": {
                        "state": worker.state,
                        "pending": worker.pending_count,
                        "batches": worker.batches,
                        "merged_batches": worker.merged_batches,
                        "replies": worker.replies,
                    }
                    if worker
                    else None,
                    **self.mute_state(key),
                }
            )
        return groups

    async def panel_records(self, key: str, before_alias: int | None, limit: int) -> list[dict]:
        """一页群聊记录，正文里的图片占位符已换成文字。"""
        records = await self.store.page(key, before_alias, limit)
        captions = await self.store.get_captions([m.md5 for r in records for m in r.media])
        out = []
        for rec in records:
            out.append(
                {
                    "alias": rec.alias,
                    "ts": rec.ts,
                    "kind": rec.kind,
                    "sender_id": rec.sender_id,
                    "nickname": rec.nickname,
                    "card": rec.card,
                    "role": rec.role,
                    "role_label": role_label(rec.role),
                    "title": rec.title,
                    "is_bot": rec.is_bot,
                    "at_bot": rec.at_bot,
                    "reply_bot": rec.reply_bot,
                    "recalled": rec.recalled,
                    "content": expand_content(rec, captions),
                    "media": [
                        {
                            "md5": m.md5,
                            "kind": m.kind,
                            "summary": m.summary,
                            "caption": captions.get(m.md5, ""),
                            "has_file": bool(m.path),
                        }
                        for m in rec.media
                    ],
                    "extra": rec.extra,
                }
            )
        return out

    async def panel_thumbnail(self, md5: str) -> str:
        media = await self.store.get_media(md5)
        if not media or not media["path"]:
            return ""
        return await thumbnail_data_url(media["path"])

    async def panel_members(self, key: str) -> list[dict]:
        order = {"owner": 0, "admin": 1}
        members = sorted(
            await self.members.list(key),
            key=lambda m: (order.get(m.role, 2), -m.updated_at),
        )
        return [m.to_dict() for m in members]


__all__ = ["SenseService", "NOTICE_MARK", "TOOL_NAMES", "GROUP_OVERRIDE_KEYS"]
