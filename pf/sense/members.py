"""群成员缓存：每群一份「QQ 号 → 昵称、群名片、群身份、专属头衔」。

每条群消息都会带上发言人的最新信息，用它刷新缓存；管理员变动、改名片等群事件
同步更新。群事件里只有 QQ 号，名字和身份都从这里取，取不到再问 QQ 接口。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from astrbot.api import logger

from .store import MessageStore

ROLE_LABELS = {"owner": "群主", "admin": "管理员"}
# 问过接口仍然没有结果的成员，这么久之内不再重复问
_LOOKUP_RETRY_S = 600.0


def role_label(role: str) -> str:
    """群身份的中文名；普通群员和未知身份返回空串。"""
    return ROLE_LABELS.get(role or "", "")


@dataclass
class Member:
    user_id: str
    nickname: str = ""
    card: str = ""
    role: str = ""
    title: str = ""
    updated_at: float = 0.0

    @property
    def display_name(self) -> str:
        return self.card or self.nickname or self.user_id

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "card": self.card,
            "role": self.role,
            "role_label": role_label(self.role),
            "title": self.title,
            "updated_at": self.updated_at,
        }


class MemberCache:
    def __init__(self, store: MessageStore) -> None:
        self.store = store
        self._groups: dict[str, dict[str, Member]] = {}
        self._failed_lookups: dict[tuple[str, str], float] = {}

    async def _group(self, group_key: str) -> dict[str, Member]:
        group = self._groups.get(group_key)
        if group is None:
            group = {}
            if self.store.is_open:
                for uid, nickname, card, role, title, updated_at in await self.store.get_members(
                    group_key
                ):
                    group[str(uid)] = Member(
                        user_id=str(uid),
                        nickname=nickname or "",
                        card=card or "",
                        role=role or "",
                        title=title or "",
                        updated_at=float(updated_at or 0),
                    )
            self._groups[group_key] = group
        return group

    async def observe(
        self,
        group_key: str,
        user_id: str,
        *,
        nickname: str | None = None,
        card: str | None = None,
        role: str | None = None,
        title: str | None = None,
    ) -> Member:
        """用最新看到的信息更新一位成员；传 None 的字段保持不变。只在有变化时写库。"""
        user_id = str(user_id)
        group = await self._group(group_key)
        member = group.get(user_id)
        created = member is None
        if member is None:
            member = Member(user_id=user_id)
            group[user_id] = member
        changed = created
        for field_name, value in (
            ("nickname", nickname),
            ("card", card),
            ("role", role),
            ("title", title),
        ):
            if value is None:
                continue
            value = str(value)
            if getattr(member, field_name) != value:
                setattr(member, field_name, value)
                changed = True
        if changed:
            member.updated_at = time.time()
            if self.store.is_open:
                await self.store.put_member(
                    group_key,
                    user_id,
                    member.nickname,
                    member.card,
                    member.role,
                    member.title,
                )
        return member

    async def get(self, group_key: str, user_id: str) -> Member | None:
        return (await self._group(group_key)).get(str(user_id))

    async def resolve(
        self, group_key: str, user_id: str, bot=None, group_id: str = ""
    ) -> Member:
        """返回一位成员；缓存里没有时用 get_group_member_info 补查，仍查不到就只带 QQ 号。"""
        user_id = str(user_id)
        member = await self.get(group_key, user_id)
        if member is not None and (member.nickname or member.card):
            return member
        last_failed = self._failed_lookups.get((group_key, user_id), 0.0)
        if bot is not None and group_id and time.monotonic() - last_failed > _LOOKUP_RETRY_S:
            try:
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=False,
                )
                if isinstance(info, dict) and info:
                    return await self.observe(
                        group_key,
                        user_id,
                        nickname=str(info.get("nickname") or ""),
                        card=str(info.get("card") or ""),
                        role=str(info.get("role") or ""),
                        title=str(info.get("title") or ""),
                    )
            except Exception as e:
                logger.debug(f"[GroupSense] 查询群成员 {user_id} 失败: {e}")
            self._failed_lookups[(group_key, user_id)] = time.monotonic()
        return member or Member(user_id=user_id)

    async def remove(self, group_key: str, user_id: str) -> None:
        """成员退群或被踢后移除。"""
        user_id = str(user_id)
        group = await self._group(group_key)
        if group.pop(user_id, None) is not None and self.store.is_open:
            await self.store.delete_member(group_key, user_id)

    async def list(self, group_key: str) -> list[Member]:
        return list((await self._group(group_key)).values())
