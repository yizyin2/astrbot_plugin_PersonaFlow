"""人格解析，以及从人格记忆库只读获取群友的关系与印象。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from astrbot.api import logger

_PERSONA_CACHE_TTL = 30.0

# (event, user_ids) -> {user_id: {"name", "relationship", "impression", "dialogue_count"}}
ImpressionSource = Callable[[object, list[str]], Awaitable[dict[str, dict]]]


@dataclass
class PersonaInfo:
    persona_id: str
    name: str
    prompt: str


@dataclass
class Impression:
    name: str
    relationship: str
    impression: str
    dialogue_count: int


class PersonaResolver:
    def __init__(self, context, impression_source: ImpressionSource | None = None) -> None:
        self.context = context
        # 由插件入口注入，指向人格记忆路由的 get_impressions；没有时判断不参考印象
        self.impression_source = impression_source
        self._cache: dict[str, tuple[float, PersonaInfo]] = {}

    async def get_persona(self, event) -> PersonaInfo:
        """按主 agent 相同的规则解析当前会话生效的人格。"""
        umo = event.unified_msg_origin
        cached = self._cache.get(umo)
        if cached and time.monotonic() - cached[0] < _PERSONA_CACHE_TTL:
            return cached[1]
        info = PersonaInfo(persona_id="", name="", prompt="")
        try:
            conv_mgr = self.context.conversation_manager
            conv_persona_id = None
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if cid:
                conv = await conv_mgr.get_conversation(umo, cid)
                if conv:
                    conv_persona_id = conv.persona_id
            provider_settings = self.context.get_config(umo=umo).get(
                "provider_settings", {}
            )
            persona_id, persona, _, _ = (
                await self.context.persona_manager.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=conv_persona_id,
                    platform_name=event.get_platform_name(),
                    provider_settings=provider_settings,
                )
            )
            if persona:
                info = PersonaInfo(
                    persona_id=str(persona_id or persona.get("name") or ""),
                    name=str(persona.get("name") or persona_id or ""),
                    prompt=str(persona.get("prompt") or ""),
                )
        except Exception as e:
            logger.debug(f"[GroupSense] 解析人格失败: {e}")
        self._cache[umo] = (time.monotonic(), info)
        return info

    async def get_impressions(self, user_ids: list[str], event=None) -> dict[str, Impression]:
        """只读查询这些人在当前会话对应记忆库里的关系与印象。

        选库规则与聊天注入一致（实际人格、Bot、账号、群聊范围）；不在范围或出错时返回空。
        """
        user_ids = [u for u in dict.fromkeys(user_ids) if u]
        if not user_ids or event is None or self.impression_source is None:
            return {}
        try:
            data = await self.impression_source(event, user_ids)
        except Exception as e:
            logger.debug(f"[GroupSense] 读取人物印象失败: {e}")
            return {}
        return {
            str(qq): Impression(
                name=str(v.get("name") or ""),
                relationship=str(v.get("relationship") or ""),
                impression=str(v.get("impression") or ""),
                dialogue_count=int(v.get("dialogue_count") or 0),
            )
            for qq, v in (data or {}).items()
            if isinstance(v, dict)
        }
