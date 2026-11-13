"""AstrBot 自己的路由：读出一个会话走哪份配置文件、人格是从哪一层来的，并在面板操作时修改。

AstrBot 决定会话人格的顺序是：会话规则里强制指定的人格 → 当前对话选的人格 → 配置文件的默认人格。
配置文件则由"配置文件路由"表按会话匹配。这里只在用户点了面板上的按钮时才写，不在后台自动改。
"""

from __future__ import annotations

NO_PERSONA = "[%None]"
RULE_KEY = "session_service_config"
SOURCE_LABELS = {"rule": "会话规则", "conversation": "当前对话", "config": "配置文件"}


def umo_of(session: dict) -> str:
    """会话登记表里的一条 → AstrBot 的会话标识。没发过消息的会话按 QQ 的格式拼出来。"""
    umo = str(session.get("umo") or "")
    if umo:
        return umo
    kind = "GroupMessage" if session.get("type") == "group" else "FriendMessage"
    return f"{session.get('platform_id', '')}:{kind}:{session.get('target_id', '')}"


class AstrRouting:
    def __init__(self, context, sp=None) -> None:
        self.context = context
        self._sp = sp

    @property
    def sp(self):
        if self._sp is None:
            from astrbot.api import sp

            self._sp = sp
        return self._sp

    @property
    def acm(self):
        return getattr(self.context, "astrbot_config_mgr", None)

    @property
    def ucr(self):
        return getattr(self.acm, "ucr", None)

    # ---------- 读取 ----------

    def confs(self) -> list[dict]:
        """全部配置文件（含默认）。"""
        try:
            return [{"id": str(c["id"]), "name": str(c.get("name") or c["id"])} for c in self.acm.get_conf_list()]
        except Exception:
            return []

    def _conf_default_persona(self, umo: str) -> str:
        cfg = self.acm.get_conf(umo)
        runner = cfg.get("agent_runner", {}) or {}
        config = runner.get("config", {}) or {}
        if runner.get("runner_type") == "local":
            return str((config.get("persona", {}) or {}).get("persona_id") or "default")
        return str(config.get("persona_id") or "default")

    def _matched_route(self, umo: str) -> str:
        """配置文件路由表里实际命中的那一条；走默认配置文件时为空。"""
        ucr = self.ucr
        table = getattr(ucr, "umop_to_conf_id", None) or {}
        for pattern in table:
            try:
                if ucr._is_umo_match(pattern, umo):
                    return str(pattern)
            except Exception:
                continue
        return ""

    async def _rule(self, umo: str) -> dict:
        data = await self.sp.session_get(umo, RULE_KEY, {})
        return dict(data) if isinstance(data, dict) else {}

    async def describe(self, umo: str) -> dict:
        """一个会话在 AstrBot 里的路由。读不到时 ok 为 False，其余字段为空，不影响面板其它部分。"""
        out = {"ok": False, "umo": umo, "persona": "", "source": "", "source_label": "",
               "rule_persona": "", "conv_persona": "", "conf_persona": "",
               "conf_id": "", "conf_name": "", "conf_route": "", "conf_exact": False, "error": ""}
        try:
            info = self.acm.get_conf_info(umo)
            out["conf_id"], out["conf_name"] = str(info["id"]), str(info.get("name") or info["id"])
            out["conf_route"] = self._matched_route(umo)
            out["conf_exact"] = out["conf_route"] == umo
            out["conf_persona"] = self._conf_default_persona(umo)
            out["rule_persona"] = str((await self._rule(umo)).get("persona_id") or "")
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if cid:
                conversation = await manager.get_conversation(umo, cid)
                out["conv_persona"] = str(getattr(conversation, "persona_id", None) or "")
            for source, field in (("rule", "rule_persona"), ("conversation", "conv_persona"), ("config", "conf_persona")):
                if out[field]:
                    out["persona"], out["source"] = out[field], source
                    break
            out["source_label"] = SOURCE_LABELS.get(out["source"], "")
            out["ok"] = True
        except Exception as e:  # AstrBot 内部接口变动时只是显示不出来
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    # ---------- 修改 ----------

    async def set_persona(self, umo: str, persona: str) -> dict:
        """在会话规则里给这个会话强制指定人格；persona 为空表示取消强制。"""
        rule = await self._rule(umo)
        if persona:
            rule["persona_id"] = persona
        else:
            rule.pop("persona_id", None)
        if rule:
            await self.sp.session_put(umo, RULE_KEY, rule)
        else:
            await self.sp.session_remove(umo, RULE_KEY)
        return await self.describe(umo)

    async def set_conf(self, umo: str, conf_id: str) -> dict:
        """让这个会话走指定的配置文件；conf_id 为空或 default 表示删掉它单独的那条路由。"""
        ucr = self.ucr
        if ucr is None:
            raise RuntimeError("这个版本的 AstrBot 没有提供配置文件路由接口")
        if not conf_id or conf_id == "default":
            await ucr.delete_route(umo)
            return await self.describe(umo)
        if conf_id not in {c["id"] for c in self.confs()}:
            raise ValueError("配置文件不存在")
        await ucr.update_route(umo, conf_id)
        if ucr.get_conf_id_for_umop(umo) != conf_id:
            # 路由表按先后顺序匹配，前面有通配规则时新加的这条排在后面不会生效，把它提到最前
            rest = {k: v for k, v in ucr.umop_to_conf_id.items() if k != umo}
            await ucr.update_routing_data({umo: conf_id, **rest})
        return await self.describe(umo)
