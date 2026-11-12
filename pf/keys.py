"""各模块之间通过事件 extra 传递数据时用的键名。"""

# 独立上下文库已接管本次请求时为 True；群聊感知据此跳过对 AstrBot 对话历史的裁剪和接回
CTX_TAKEOVER_KEY = "_pf_ctx_takeover"
# 本次请求在上下文库里的归属信息（池、来源、请求对象等）
CTX_INFO_KEY = "_pf_ctx"
