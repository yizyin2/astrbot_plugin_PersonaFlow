# AstrBot Plugin: PersonaFlow (人格关系流)

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-violet)](https://github.com/Soulter/AstrBot)
[![Version](https://img.shields.io/badge/version-0.7.1(Beta)-blue)](https://github.com/Soulter/AstrBot)

**PersonaFlow** 是一个为 [AstrBot](https://github.com/Soulter/AstrBot) 设计的记忆插件。它通过 AI 自动总结用户与 Bot 之间的对话历史，生成动态的人物关系和印象，并将其注入到模型的人格设定中。

这意味着 Bot 能够“记住”每一个与它聊过天的人，知晓他们之间的关系（如朋友、死党、师生）以及对该用户的具体印象（如傲娇、博学、幽默），并在不同的群聊或会话中保持这种记忆。

## ✨ 主要功能

* **自动印象总结**：根据设定的对话轮数，定期触发 LLM 分析用户历史，提炼关系与印象。
* **动态人格注入**：支持 `{Impression}` 占位符，将最新的用户印象实时嵌入 System Prompt。
* **全异步架构**：基于 `aiosqlite`，数据库操作不阻塞主线程，高并发更稳定。
* **跨会话记忆**：基于 User ID (QQ号) 建立索引，实现跨群聊的统一记忆。
* **管理指令 (New)**：支持通过指令查看所有已存储的印象或删除特定用户的记忆。
* **格式优化 (New)**：优化了存入数据库的聊天记录格式，使 AI 总结更精准。

## 📦 安装方法

1.  将`astrbot_plugin_infinite_dialogue`文件夹放置在 AstrBot 的`data/plugins/`目录下。
2.  重启AstrBot。
3.  在控制台或 WebUI 中启用插件。

## ⚙️ 配置说明 (Configuration)

在 AstrBot 的管理面板或配置文件中，你需要设置以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `personas_name` | String | `""` | **(必填)** 需要启用记忆功能的**人格ID**（System Prompt ID）。插件将基于此人格生成动态版本。 |
| `summary_trigger_threshold` | Int | `5` | **触发阈值**。用户每进行多少次对话后，触发一次印象总结。 |
| `summary_history_count` | Int | `20` | **历史回溯**。触发总结时，读取最近多少条聊天记录发给 LLM 进行分析。 |
| `apply_to_group_chat` | List | `[]` | **生效群组**。填入群号列表。如果为空 `[]`，则默认对所有群聊/私聊生效（取决于插件加载逻辑）。 |
| `database_path` | String | `./data/OSNpermemory.db` | 插件专用数据库的存储路径。 |
| `summary_max_retries` | Int | `3` | LLM 总结失败时的最大重试次数。 |

## 🎮 指令系统 (v0.7 新增)

插件注册了 `osn` 指令组，用于管理记忆数据。

| 指令 | 参数 | 说明 | 示例 |
| --- | --- | --- | --- |
| `/osn check` | 无 | 查看数据库中存储的所有用户印象、关系及对话统计。 | `/osn check` |
| `/osn del` | `<User_ID>` | **彻底删除**指定用户的印象数据和聊天记录。 | `/osn del 123456` |

> **注意**：删除操作不可逆，执行后需使用`/new`或`/reset`指令以重置会话记忆。

## ⚠️ 核心用法：占位符设置

为了让 Bot 能够“说出”或“表现出”它对用户的印象，你必须在**原有人格（System Prompt）**中添加 `{Impression}` 占位符。

### 步骤：

1.  找到你在 人格设定`personas_id` 中配置的人格。
2.  编辑该人格的 System Prompt（系统提示词）。
3.  在合适的位置加入 `{Impression}`。
4.  修改本插件的插件配置，填写`生效的人格设定(system prompt)`,例：`小周周`。填写生效群聊：`12345678`

### 示例 System Prompt：

```text
你是一个叫“小周周”的AI助手，性格活泼可爱。

小周周认识的人:
{Impression}

请根据上面的印象和关系，用符合你人设的语气回答用户的问题。
```

**插件工作原理：**
插件会自动将 `{Impression}` 替换为类似以下的内容：
> `用户昵称(qq号),关系:朋友,印象:非常幽默，喜欢开玩笑。`

**注意：** 如果你的 System Prompt 中没有 `{Impression}`，插件会自动将印象追加到提示词的**末尾**，但这可能不如手动指定位置效果好。

## 🛠️ 技术细节

1.  **数据库**：插件会自动创建 `./data/OSNpermemory.db`，用于存储用户印象表 (`Impression`)、聊天记录表 (`Message`) 和动态人格表 (`dynamic_personas`)。
2. **数据流向**：
* **读**：通过 `self.context.provider_manager.personas` 直接从 AstrBot 内存中读取基础人格模板（安全、快速）。
* **写**：用户印象存储在独立的 `./data/OSNpermemory.db` 中，不污染 AstrBot 核心数据 (`data_v4.db`)。
3.  **Hook 机制**：
    *   `on_llm_request`: 拦截请求，将带有印象的动态 System Prompt 注入模型。
    *   `on_llm_response`: 记录对话，触发总结逻辑。
4. **并发安全**：
* 使用 `asyncio.Lock` 保证数据库写入操作的原子性，防止竞争条件。
* 数据库开启 `WAL (Write-Ahead Logging)` 模式，显著提升并发读写性能。

## 📝 版本历史
* **v0.7.1(beta)**
* 更改llm_summary为异步


* **v0.7 (Beta)**
    *   新增 `/osn check` 和 `/osn del` 管理指令。
    *   优化聊天记录存储格式，提高总结准确度。
    *   使用 `StarTools` 规范化数据存储路径。
    *   修复了部分逻辑错误。


* **v0.6 (Beta)**
    *   **重构**：迁移至 `aiosqlite`，实现全异步数据库操作。
    *   **优化**：改为从 `provider_manager` 内存读取人格模板，修复文件锁冲突问题。
    *   **性能**：增加 System Prompt 内存缓存与数据库 WAL 模式。


*   **v0.5.2(Beta)**:
    *   使用 Ruff 格式化代码。
    *   优化数据库操作，增加动态人格表。
    *   修复总结逻辑和 JSON 解析。

## 👨‍💻 作者

*   **Plugin Author**: yizyin
*   **Original Repo**: [AstrBot](https://github.com/Soulter/AstrBot)

## 📄 License


MIT License

