# PersonaFlow（人格关系流）2.0

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-violet)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/version-2.0.0-blue)](https://github.com/yizyin2/astrbot_plugin_PersonaFlow)

给 AstrBot 的角色提供三样东西，并用一个管理面板统一管理：

- **人格记忆**：人物印象、全文记忆、自动压缩。多个 Bot、多个人格各用独立数据库。
- **群聊感知**（QQ / OneBot v11）：判断小模型决定角色要不要在群里开口，主模型能看到完整的群聊记录、群友的群身份和群事件。
- **独立上下文库**：让不同的群和私聊共用一份对话上下文，数据存在插件自己的库里。

需要 AstrBot ≥ 4.29。

## 安装与升级

1. 把插件目录放进 `data/plugins/astrbot_plugin_PersonaFlow/`，或在插件管理里安装，然后重载。
2. 打开插件详情页里的 **PersonaFlow 管理面板**。所有设置都在面板里修改，保存即生效。
3. 从 1.x 或独立的 GroupSense 插件升级时，首次启动会自动导入旧配置和数据，结果显示在面板「总览」里。**升级后请停用或卸载旧的 GroupSense 插件和心流插件**，否则会重复接管；旧 GroupSense 还在运行时，内置的群聊感知不会启动。

AstrBot 自带的插件配置页只保留一个总开关。

## 管理面板

| 页面 | 用途 |
| --- | --- |
| 总览 | 状态检查（冲突的设置、未生效的档案等）、迁移报告、各档案概况、最近活动 |
| 路由 | 档案的增删改；Bot → 人格 → 档案的关系图；每个群和私聊走到哪个档案、哪份上下文、是否受管；路由测试 |
| 人格记忆 | 每个档案的人物印象、全文记忆、对话记录、最终人格提示词；手动总结和压缩 |
| 群聊感知 | 每个群的聊天记录（含群事件和身份标记）、成员身份、判断历史、本群单独设置 |
| 上下文库 | 每个池的对话时间线、断点、下次请求会带上的范围；编辑、删除、导入、导出 |
| 日志 | 结构化事件和文本日志，按级别、类别、会话筛选，可实时滚动 |
| 设置 | 人格记忆、群聊感知、上下文库、日志的全部参数 |

## 档案

一个档案 = **Bot + 机器人账号（可选）+ 人格**，带生效范围（哪些群、是否包含私聊）。它决定：

- 用哪个记忆库：范围内的会话共享人物印象和全文记忆；
- 上下文怎么保存：不接管 / 共享池 / 独立。

会话必须实际选用档案里的人格才会命中。多个账号共用一个 Bot 适配器时，给每个账号各建一个档案并填上账号 ID。「任意 Bot」档案按人格匹配，用于兼容旧版单库模式。

有冲突的档案（重复、账号选择重叠、共用同一个数据库文件）不会生效，面板里会标出原因，其他档案不受影响。

## 人格记忆

在人格提示词里可以指定记忆的插入位置：

```text
你是一个叫“小周周”的 AI 助手。

你认识的人：
{Impression}

以前的聊天记忆：
{Memory}
```

没写占位符时，对应内容追加到人格末尾。插件每次都从 AstrBot 当前的人格模板重新组装，修改人格后下一条请求就生效，也不会修改 AstrBot 里的原始人格。

- **人物印象**：每位用户对话满一定轮数后，让模型总结与他的关系和印象。
- **全文记忆**：范围内累计满一定轮数后，把最近的对话总结成一条记忆；条数超过阈值时把最旧的一批合并成一条。
- 可以在设置里指定单独的「总结模型」。

## 群聊感知

- 受管群里的每条消息（包括 @ 机器人的）都先交给判断模型，由它按角色人格和对群友的印象决定回不回、回哪条。
- 主模型每次都能看到最近的群聊记录和最近几张图片；这些内容只在请求时注入，不写进对话历史。
- 主模型可以用 `quote_message` 引用消息、`mention_user` @ 群友、`poke_user` 戳群友。
- **群身份**：群主、管理员在群聊记录里带身份标记，模型也知道机器人自己的身份。
- **群事件**：禁言 / 解禁、戳一戳、撤回、进群 / 退群 / 被踢、管理员变动、改群名片都会记入群聊记录，可在设置里逐类开关。
  - 机器人被禁言期间只记录、不调用判断模型，解禁后恢复。
  - 戳机器人和 @ 一样交给判断模型；同一人连续戳会合并，并有触发冷却。
  - 还没判断就被撤回的消息不会再被回应。

请关闭 AstrBot 内置的「群聊消息记录注入上下文」「主动回复」「回复时 @ 发送者 / 引用消息」，面板的状态检查会提示。活跃的群建议调高 AstrBot 的速率限制。

已知限制：机器人自己发出的消息拿不到 QQ 消息 ID，不能被 `quote_message` 引用；只支持 aiocqhttp（NapCat 等 OneBot v11 实现）。

## 独立上下文库

在档案里把「上下文保存方式」改成：

- **共享池**：范围内的群和私聊共用一份上下文。模型在任何一个会话里都能看到其他会话最近的对话，每一轮都标了来源。
- **独立**：每个会话各一份，但存在插件自己的库里。
- **不接管**（默认）：对话历史仍由 AstrBot 保存。

个别会话可以在路由表里单独指定，也可以建多个共享池。接管后，AstrBot 的对话数据里不再有这些会话的内容（只保留一条空记录用于选择人格）。

需要知道的两点：

- 共享之后私聊内容会进入群聊请求，模型可能在群里提到。可以在档案里关掉「私聊内容对群聊可见」。
- 每次请求会多带跨会话的历史，token 用量会增加。窗口大小在「设置 → 独立上下文库」里调。更早的内容由全文记忆承担。

`/reset`、`/new` 和 `/ctx reset` 在被接管的会话里只打一个断点：之后的请求从这里重新开始，旧内容还在，面板里可以撤销。共享池和群聊的重置只允许 AstrBot 管理员操作。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/osn profile` | 当前 Bot、账号、人格命中的档案和数据库 |
| `/osn check [页码]` | 查看人物印象 |
| `/osn checkmem [页码]` | 查看全文记忆 |
| `/osn del <用户ID>` | 删除某人的印象和对话记录 |
| `/osn clearmem [条数]` | 删除最近几条或全部全文记忆 |
| `/gs` | 本群的接管状态、机器人身份、禁言状态、最近一次判断 |
| `/gs clear` | 清除本群在插件里保存的聊天记录 |
| `/ctx` | 当前会话使用的上下文池和窗口大小 |
| `/ctx reset` | 在上下文库里打断点，从这里重新开始 |

`/osn`、`/gs`、`/ctx` 查看类指令仅 AstrBot 管理员可用。

## 供其他插件调用的接口

```python
metadata = self.context.get_registered_star("astrbot_plugin_PersonaFlow")
personaflow = metadata.star_cls if metadata and metadata.activated else None
if personaflow is not None and getattr(personaflow, "API_VERSION", 0) >= 1:
    impressions = await personaflow.get_impressions(event, ["123456"])
    profile = await personaflow.resolve_profile(event)
```

| 接口 | 说明 |
| --- | --- |
| `API_VERSION` | 当前为 `2`，v1 的两个接口保持不变 |
| `await get_impressions(event, user_ids)` | 只读获取指定用户的关系、印象和对话次数；不在范围或出错时返回空字典 |
| `await resolve_profile(event)` | 返回该事件对应的记忆配置；不在范围时返回 `None` |

## 数据

都在 `data/plugin_data/astrbot_plugin_PersonaFlow/`：

| 文件 | 内容 |
| --- | --- |
| `settings.json` | 全部设置和档案 |
| `profiles/*.db` | 各档案的记忆库（人物印象、对话记录、全文记忆） |
| `sense/groupsense.db`、`sense/media/` | 群聊记录、成员身份、图片缓存 |
| `context.db` | 独立上下文库 |
| `logs.db` | 日志 |
| `backups/` | 面板修改记忆库前自动留的备份（每个库保留最近 5 份） |

## 开发与测试

测试需要 AstrBot 的运行环境，在容器里跑：

```bash
docker cp astrbot_plugin_PersonaFlow astrbot:/tmp/pf2 && docker exec -w /tmp/pf2 astrbot python -m pytest -q
```

版本记录见 [CHANGELOG.md](CHANGELOG.md)。

## 作者与许可证

- 作者：yizyin
- 仓库：[yizyin2/astrbot_plugin_PersonaFlow](https://github.com/yizyin2/astrbot_plugin_PersonaFlow)
- 许可证：GNU AGPL v3，详见 [LICENSE](LICENSE)。
