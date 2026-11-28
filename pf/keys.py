"""各模块之间、以及和群聊感知插件（GroupSense）之间通过事件 extra 传递数据时用的键名。"""

# 独立上下文库已接管本次请求时为 True；群聊感知插件据此不再裁剪 AstrBot 的对话历史
CTX_TAKEOVER_KEY = "_pf_ctx_takeover"
# 本次请求在上下文库里的归属信息（池、来源、请求对象等）
CTX_INFO_KEY = "_pf_ctx"
# 群聊感知插件写入：这次请求来自它接管的群。值为
#   {"managed": True, "since": 主模型能看到的群聊记录里最早一条的时间或 None, "keep_turns": 本群保留的对话轮数}
# 它在 on_llm_request 优先级 -90 写入，上下文库在 -100 读取，据此跳过已经在群聊记录里的本群轮次
GS_WINDOW_KEY = "_groupsense_window"
# 记忆库写入：这一轮回复存进了哪个档案的 Message 表第几行，{"profile_id": ..., "row": ...}
MEMORY_ROW_KEY = "_pf_memory_row"
# /drh 轮次账里这一轮的行 ID，消息发完后据此记下发送时间
ROUND_KEY = "_pf_round"
