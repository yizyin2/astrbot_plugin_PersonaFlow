import ast
import asyncio
import json
import os
import re
from datetime import datetime

import aiosqlite

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

"""
版本1.1 2026-10-04
全文总结总开关统一控制记忆生成、人格注入，以及向人物印象总结提供全文记忆；关闭时保留已有数据。
未填写 {Memory} 占位符时，自动将已有全文记忆追加到人格提示词末尾。
开放 Memory 自动压缩阈值和每批条数，默认超过30条时将最旧的20条合并为1条。
压缩结果继承最旧记录的时间，可再次参与压缩；压缩失败时保留原记录。
人格注入仅替换匹配的原始人格段，保留框架、技能及工具等其他系统提示。
每次请求使用最新人格模板重新组装已有记忆，编辑并保存人格后无需重载插件，内容未变化时不重复写库。
消息保存与计数读取使用同一事务，修复并发下漏触发或重复触发总结的问题。
压缩任务串行执行并校验待替换批次，避免并发重复写入记忆。
群聊白名单改用群号匹配，兼容独立会话模式。
数据库建表完成后才公开连接，初始化失败或取消时清理连接，卸载时先停止启动同步任务。
"""


@register(
    "astrbot_plugin_PersonaFlow",
    "yizyin",
    "由ai自动总结人物关系到数据库，实现在不同群聊记住同一个人之间与ai的关系和印象，即使new对话也能继承之前的关系印象",
    "1.1",
)
class PersonaFlow(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        data_dir = StarTools.get_data_dir("astrbot_plugin_PersonaFlow")
        default_path = str(data_dir / "OSNpermemory.db")  # 转换为字符串
        self.db_path = self.config.get("database_path") or default_path
        self.db = None  # 数据库连接对象初始化为None
        self._db_lock = asyncio.Lock()
        self._memory_compaction_lock = asyncio.Lock()
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        # 更新人格模板到数据库的异步任务，避免阻塞插件启动
        self._startup_task = asyncio.create_task(self._sync_persona_on_startup())

        logger.info("人格关系流(PersonaFlow)加载成功! 数据库路径：" + self.db_path)

    async def _sync_persona_on_startup(self):
        """插件启动时同步人格模板到数据库"""
        try:
            json_persona_id = self.config.get("personas_name", "")
            if not json_persona_id:
                logger.warning("未配置 personas_name，跳过人格同步")
                return

            logger.info(f"正在检查人格模板更新: {json_persona_id}")

            # 获取当前数据库中的印象数据
            current_impression = await self.get_sql_relationship_impression()

            # 使用统一的写入逻辑，会自动处理 {Impression} 和 {Memory}
            await self.write_astrbot_persona_prompt(json_persona_id, current_impression)

            logger.info(f"✅ 人格模板同步完成: {json_persona_id}动态")

        except Exception as e:
            logger.error(f"人格模板同步失败: {e}", exc_info=True)

    def _get_int_config(self, key: str, default: int, min_value: int | None = None):
        """读取整数配置，非法值回退到默认值。"""
        raw_value = self.config.get(key, default)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            logger.warning(
                f"配置 {key}={raw_value!r} 不是有效整数，使用默认值 {default}"
            )
            return default

        if min_value is not None and value < min_value:
            logger.warning(
                f"配置 {key}={value} 小于最小值 {min_value}，使用默认值 {default}"
            )
            return default
        return value

    def _get_bool_config(self, key: str, default: bool):
        """读取布尔配置，兼容手写字符串。"""
        raw_value = self.config.get(key, default)
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in (
                "true",
                "1",
                "yes",
                "on",
                "enable",
                "enabled",
                "开启",
                "启用",
            ):
                return True
            if normalized in (
                "false",
                "0",
                "no",
                "off",
                "disable",
                "disabled",
                "关闭",
                "禁用",
            ):
                return False
        if raw_value is None:
            return default
        return bool(raw_value)

    # ************数据库操作函数**********
    async def _get_db(self):
        """Return a connection only after schema initialization succeeds.

        Returns:
            The initialized SQLite connection.

        Raises:
            Exception: If connecting or initializing the schema fails.
        """
        if self.db is None:
            async with self._db_lock:
                if self.db is None:
                    db = None
                    try:
                        db = await aiosqlite.connect(
                            self.db_path, check_same_thread=False
                        )
                        await db.execute("PRAGMA journal_mode=WAL;")
                        await self._init_tables(db)
                        self.db = db
                        logger.info("PersonaFlow database initialized")
                    except (Exception, asyncio.CancelledError):
                        if db is not None:
                            await db.close()
                        raise
        return self.db

    async def _init_tables(self, db):
        """Create the plugin tables and propagate initialization errors.

        Args:
            db: The unpublished database connection to initialize.

        Raises:
            Exception: If table creation or committing the schema fails.
        """
        try:
            # 使用 execute 的上下文管理器，自动关闭 cursor
            await db.execute("""
                CREATE TABLE IF NOT EXISTS Impression (
                    qq_number TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    relationship TEXT,
                    impression TEXT,
                    dialogue_count INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS Message (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_number TEXT not null,
                    message TEXT,
                    chat_time datetime DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS dynamic_personas (
                    id INTEGER NOT NULL,
                    persona_id VARCHAR(255) NOT NULL,
                    system_prompt TEXT NOT NULL,
                    begin_dialogs JSON,
                    tools JSON,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    CONSTRAINT uix_persona_id UNIQUE (persona_id)
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS Memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()
        except Exception as e:
            logger.error(f"Failed to initialize PersonaFlow tables: {e}")
            await db.rollback()
            raise

    async def insert_user(self, qq_number, user_name):
        """插入用户信息到数据库"""
        db = await self._get_db()
        async with self._db_lock:  # 写操作加锁
            try:
                sql = "INSERT INTO Impression (qq_number, name) VALUES (?, ?)"
                await db.execute(sql, (qq_number, user_name))
                await db.commit()
                logger.info(f"用户 {user_name} ({qq_number}) 插入数据库")
            except Exception as e:
                logger.error(f"插入用户失败: {e}")
                await db.rollback()

    async def select_dialogue_count(self, qq_number):
        """查询对话次数"""
        db = await self._get_db()
        try:
            sql = "SELECT dialogue_count FROM Impression WHERE qq_number = ?"
            async with db.execute(sql, (qq_number,)) as cursor:
                result = await cursor.fetchone()
                return result[0] if result and result[0] is not None else 0
        except Exception as e:
            logger.error(f"查询对话次数失败: {e}")
            return 0

    async def increment_dialogue_count(self, qq_number):
        """对话次数+1"""
        db = await self._get_db()
        async with self._db_lock:
            try:
                sql = "UPDATE Impression SET dialogue_count = dialogue_count + 1 WHERE qq_number = ?"
                await db.execute(sql, (qq_number,))
                await db.commit()
            except Exception as e:
                logger.error(f"更新对话次数失败: {e}")
                await db.rollback()

    async def set_sql_relationship_impression(
        self, qq_number, relationship, impression
    ):
        """更新关系与印象"""
        db = await self._get_db()
        async with self._db_lock:
            try:
                sql = "UPDATE Impression SET relationship = ?, impression = ? WHERE qq_number = ?"
                await db.execute(sql, (relationship, impression, qq_number))
                await db.commit()
                logger.info("关系与印象更新成功")
            except Exception as e:
                logger.error(f"更新关系与印象失败: {e}")
                await db.rollback()

    async def get_sql_relationship_impression(self):
        """获取全部关系与印象"""
        db = await self._get_db()
        try:
            # 1. 查询需要的四个字段
            sql = "SELECT qq_number, name, relationship, impression FROM Impression"
            async with db.execute(sql) as cursor:
                # 2. 获取所有结果 (fetchall)
                results = await cursor.fetchall()

            if not results:
                logger.info("数据库中暂无印象记录")
                return "暂无已知的关系与印象记录。"

            info_list = []

            # 3. 循环处理每一行数据
            for row in results:
                # 按照 SQL 顺序提取字段，并处理 None 的情况
                r_qq = row[0]
                r_name = row[1] if row[1] is not None else "未知昵称"
                r_rel = row[2] if row[2] is not None else "无"
                r_imp = row[3] if row[3] is not None else "无"

                # 4. 单条记录拼接
                line = f"{r_name}({r_qq})，关系：{r_rel}，印象：{r_imp}。"
                info_list.append(line)

            # 5. 将所有人的记录用换行符拼接
            final_prompt = "已知的人物关系如下：\n" + "\n".join(info_list)

            # logger.info(f"成功获取 {len(info_list)} 条关系记录")
            return final_prompt

        except Exception as e:
            logger.error(f"获取全部关系与印象失败: {e}")
            return "获取关系数据出错。"

    async def add_persona_chat_history(self, qq_number, message):
        """添加用户的聊天记录到数据库"""
        db = await self._get_db()
        async with self._db_lock:
            try:
                sql = "INSERT INTO Message (qq_number, message) VALUES (?, ?)"
                await db.execute(sql, (qq_number, message))
                await db.commit()
            except Exception as e:
                logger.error(f"插入聊天记录失败: {e}")
                await db.rollback()

    async def get_recent_chat_history(self, qq_number, n):
        """获取用户的最近n条聊天记录，qq_number为None或"*"时获取全部"""
        db = await self._get_db()
        try:
            if qq_number in (None, "*", "all") and n == 0:
                # 获取全部记录
                sql = "SELECT message FROM Message ORDER BY chat_time DESC, id DESC"
                params = ()
            elif qq_number in (None, "*", "all"):
                # 获取所有用户的记录
                sql = "SELECT message FROM Message ORDER BY chat_time DESC, id DESC LIMIT ?"
                params = (n,)
            else:
                # 获取指定用户的记录
                sql = "SELECT message FROM Message WHERE qq_number = ? ORDER BY chat_time DESC, id DESC LIMIT ?"
                params = (qq_number, n)

            async with db.execute(sql, params) as cursor:
                results = await cursor.fetchall()
            messages = [row[0] for row in results]
            logger.info(f"成功获取最近 {n} 条聊天记录")
            return messages[::-1]
        except Exception as e:
            logger.error(f"获取聊天记录失败: {e}")
            return []

    async def get_dynamic_persona(self, p_id: str):
        """获取动态人格 Prompt"""
        db = await self._get_db()
        try:
            sql = "SELECT system_prompt FROM dynamic_personas WHERE persona_id = ?"
            async with db.execute(sql, (p_id,)) as cursor:
                result = await cursor.fetchone()

            if result:
                logger.info(f"成功获取人格: {p_id}")
                return result[0]
            else:
                logger.debug(f"未找到人格 ID: {p_id}")
                return None
        except Exception as e:
            logger.error(f"数据库查询失败: {e}")
            return None

    async def update_user_name_only(self, qq_number, name):
        """更新用户名"""
        db = await self._get_db()
        async with self._db_lock:
            try:
                sql = "UPDATE Impression SET name = ? WHERE qq_number = ?"
                await db.execute(sql, (name, qq_number))
                await db.commit()
                logger.info(f"更新用户 {qq_number} 昵称为: {name}")
            except Exception as e:
                logger.error(f"更新user_name失败: {e}")
                await db.rollback()

    async def add_memory(self, memory_text):
        """添加记忆到 Memory 表"""
        db = await self._get_db()
        async with self._db_lock:
            try:
                sql = "INSERT INTO Memory (memory) VALUES (?)"
                await db.execute(sql, (memory_text,))
                await db.commit()
                logger.info("成功添加记忆到数据库")
            except Exception as e:
                logger.error(f"添加记忆失败: {e}")
                await db.rollback()

    async def get_recent_memory(self):
        """获取全部记忆"""
        db = await self._get_db()
        try:
            sql = "SELECT memory FROM Memory ORDER BY created_at DESC, id DESC"
            async with db.execute(sql) as cursor:
                results = await cursor.fetchall()
            memories = [row[0] for row in results]
            logger.info(f"成功获取全部记忆，共 {len(memories)} 条")
            return memories[::-1]  # 返回从旧到新的顺序
        except Exception as e:
            logger.error(f"获取记忆失败: {e}")
            return []

    async def get_memory_count(self):
        """获取 Memory 表记录数"""
        db = await self._get_db()
        try:
            sql = "SELECT COUNT(*) FROM Memory"
            async with db.execute(sql) as cursor:
                result = await cursor.fetchone()
            return result[0] if result else 0
        except Exception as e:
            logger.error(f"获取记忆数量失败: {e}")
            return 0

    async def get_oldest_memory_records(self, limit=10):
        """获取最旧的 Memory 记录，用于压缩。"""
        db = await self._get_db()
        try:
            sql = """
                SELECT id, memory, created_at
                FROM Memory
                ORDER BY created_at ASC, id ASC
                LIMIT ?
            """
            async with db.execute(sql, (limit,)) as cursor:
                return await cursor.fetchall()
        except Exception as e:
            logger.error(f"获取待压缩记忆失败: {e}")
            return []

    async def replace_memory_records(self, memory_ids, summary_text, created_at):
        """Replace a complete, still-existing batch of memories atomically.

        Args:
            memory_ids: IDs of the records used to generate the summary.
            summary_text: The condensed memory to store.
            created_at: Timestamp of the oldest record in the batch.
        """
        if not memory_ids:
            return

        db = await self._get_db()
        async with self._db_lock:
            try:
                placeholders = ",".join("?" for _ in memory_ids)
                async with db.execute(
                    f"DELETE FROM Memory WHERE id IN ({placeholders})",
                    tuple(memory_ids),
                ) as cursor:
                    if cursor.rowcount != len(memory_ids):
                        await db.rollback()
                        logger.warning(
                            "Memory compaction batch is stale; skipping replacement"
                        )
                        return
                await db.execute(
                    "INSERT INTO Memory (memory, created_at) VALUES (?, ?)",
                    (summary_text, created_at),
                )
                await db.commit()
                logger.info(f"已将 {len(memory_ids)} 条 Memory 压缩为 1 条")
            except asyncio.CancelledError:
                await db.rollback()
                raise
            except Exception as e:
                logger.error(f"替换压缩记忆失败: {e}")
                await db.rollback()

    async def get_all_dialogue_count(self):
        """获取总对话次数"""
        db = await self._get_db()
        try:
            sql = "SELECT qq_number, dialogue_count FROM Impression"
            async with db.execute(sql) as cursor:
                results = await cursor.fetchall()
            dialogue_counts = {
                row[0]: (row[1] if row[1] is not None else 0) for row in results
            }
            total_count = sum(dialogue_counts.values())
            logger.info(f"总对话次数: {total_count}")
            return total_count
        except Exception as e:
            logger.error(f"获取对话次数失败: {e}")
            return 0

    async def get_all_memory(self):
        """获取全部记忆（等同于 get_recent_memory）"""
        return await self.get_recent_memory()

    # ************ 事件处理函数 **********

    @filter.on_llm_request()
    async def inject_dynamic_persona(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """替换配置指定的人格段，保留其他提示并防止重复注入。

        参数：
            event: 当前消息事件，用于检查群聊白名单。
            req: 即将发送的模型请求。
        """
        current_group_id = str(event.get_group_id() or "")

        active_group_ids = [str(x) for x in self.config.get("apply_to_group_chat", [])]

        if not active_group_ids or (
            current_group_id and current_group_id in active_group_ids
        ):
            # 获取配置文件中的基础人格ID
            json_persona_id = self.config.get("personas_name", "")
            if not json_persona_id:
                logger.warning("人格配置缺失")
                return

            raw_prompt, _, _ = self.get_persona_template(json_persona_id)
            if not raw_prompt:
                return

            # 使用当前模板重新组装记忆，避免旧的动态人格覆盖刚保存的人格修改。
            dynamic_prompt = await self.get_dynamic_persona_prompt(json_persona_id)

            if dynamic_prompt:
                current_prompt = req.system_prompt or ""
                previous_prompt = getattr(req, "_personaflow_injected_prompt", None)
                if previous_prompt and previous_prompt in current_prompt:
                    # 同一请求保持已注入的版本，新记忆从下一条请求开始使用。
                    return
                if self.get_persona_template(json_persona_id)[0] != raw_prompt:
                    # 等待读库时模板发生变化，保留当前请求，避免混用新旧模板。
                    logger.debug("人格模板在组装期间发生变化，跳过本次注入。")
                    return
                if not current_prompt:
                    req.system_prompt = dynamic_prompt
                elif dynamic_prompt in current_prompt:
                    pass
                elif raw_prompt and raw_prompt in current_prompt:
                    # 仅替换人格段，保留框架和其他插件的提示。
                    req.system_prompt = current_prompt.replace(
                        raw_prompt, dynamic_prompt, 1
                    )
                else:
                    logger.debug("请求中没有匹配的人格模板，跳过注入。")
                    return
                req._personaflow_injected_prompt = dynamic_prompt

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """Store a response and trigger summaries from its atomic counter snapshot.

        Args:
            event: The originating user message.
            resp: The model response to record.
        """
        current_group_id = str(event.get_group_id() or "")

        active_group_ids = [str(x) for x in self.config.get("apply_to_group_chat", [])]

        if not active_group_ids or (
            current_group_id and current_group_id in active_group_ids
        ):
            # 提前定义变量，防止try块外引用报错
            new_name = "未知用户"
            qq_number = "0"

            try:
                new_name = event.get_sender_name()
                qq_number = event.get_sender_id()
                user_message = event.get_message_str()
                # 检查是否为错误响应
                if resp.role == "err":
                    logger.warning(
                        f"LLM 返回错误响应，跳过存储。用户: {new_name}({qq_number})"
                    )
                    return
                # 防止空消息报错
                if not user_message or not resp.completion_text:
                    logger.debug(
                        f"消息内容为空，跳过存储。用户: {new_name}({qq_number})"
                    )
                    return

                message = self.merge_AI_and_user_message(
                    user_message, resp.completion_text, new_name
                )

                # Capture both counters in the same transaction as this message.
                # Each threshold value then belongs to exactly one response.
                db = await self._get_db()
                async with self._db_lock:
                    try:
                        await db.execute(
                            "INSERT INTO Message (qq_number, message) VALUES (?, ?)",
                            (qq_number, message),
                        )
                        await db.execute(
                            """
                            INSERT INTO Impression (qq_number, name, dialogue_count)
                            VALUES (?, ?, 1)
                            ON CONFLICT(qq_number) DO UPDATE SET
                                name = excluded.name,
                                dialogue_count = COALESCE(Impression.dialogue_count, 0) + 1
                            """,
                            (qq_number, new_name),
                        )
                        async with db.execute(
                            "SELECT dialogue_count FROM Impression WHERE qq_number = ?",
                            (qq_number,),
                        ) as cursor:
                            dialogue_count = (await cursor.fetchone())[0]
                        async with db.execute(
                            "SELECT COALESCE(SUM(dialogue_count), 0) FROM Impression"
                        ) as cursor:
                            total_dialogue_count = (await cursor.fetchone())[0]
                        await db.commit()
                    except (Exception, asyncio.CancelledError):
                        await db.rollback()
                        raise

            except Exception as e:
                logger.error(f"处理用户数据失败: {e}", exc_info=True)
                return

            # 获取json生效人格设定
            json_persona_id = self.config.get("personas_name", "")
            # logger.info(f"json_persona_id：{json_persona_id}")
            # 总结触发逻辑
            try:
                summary_trigger_threshold = self._get_int_config(
                    "summary_trigger_threshold", 5, min_value=1
                )
                if (
                    dialogue_count > 0
                    and dialogue_count % summary_trigger_threshold == 0
                ):
                    # 执行 LLM 总结
                    summary_result = await self.llm_summary(
                        event, new_name, qq_number, json_persona_id
                    )

                    # 如果总结成功（返回了字符串），则更新 System Prompt
                    if summary_result:
                        # 重新获取最新的完整印象列表（包含刚更新的）
                        new_full_impression = (
                            await self.get_sql_relationship_impression()
                        )
                        await self.write_astrbot_persona_prompt(
                            json_persona_id, new_full_impression
                        )
            except Exception as e:
                logger.error(f"总结触发流程失败: {e}")

            # 记忆总结触发逻辑
            try:
                if not self._get_bool_config("enable_memory_summary", True):
                    logger.debug("全文总结功能已关闭，跳过 Memory 总结。")
                else:
                    summary_memory_trigger_threshold = self._get_int_config(
                        "summary_memory_trigger_threshold", 20, min_value=1
                    )
                    logger.info(
                        f"当前总对话数: {total_dialogue_count}, 记忆总结触发阈值: {summary_memory_trigger_threshold}"
                    )

                    if (
                        total_dialogue_count > 0
                        and total_dialogue_count % summary_memory_trigger_threshold == 0
                    ):
                        logger.info(
                            f"触发记忆总结，当前总对话数: {total_dialogue_count}"
                        )
                        await self.memory_summary(event, json_persona_id)
                        await self.write_astrbot_persona_prompt(
                            json_persona_id,
                            await self.get_sql_relationship_impression(),
                        )
            except Exception as e:
                logger.error(f"记忆总结流程失败: {e}")
        else:
            logger.info("当前会话不在设置，未执行代码")
            pass

    async def llm_summary(
        self, event: AstrMessageEvent, user, qq_number, json_persona_id
    ):
        """调用LLM进行总结印象和关系"""
        logger.info(f"开始调用大模型进行总结，用户: {user}")

        # 最大总结重试次数
        max_retries = self._get_int_config("summary_max_retries", 3, min_value=1)

        # 总结时获取对应用户聊天记录条数
        summary_history_count = self._get_int_config(
            "summary_history_count", 20, min_value=1
        )

        user_message_history = await self.get_recent_chat_history(
            event.get_sender_id(), n=summary_history_count
        )
        # logger.info(f"对话用户聊天记录:{user_Message_history}")

        # 获取数据库中的关系和印象
        pre_impression = await self.get_sql_relationship_impression()

        # 获取当前的(动态)系统提示词
        dynamic_persona_prompt = await self.get_dynamic_persona_prompt(json_persona_id)

        memory_list = (
            await self.get_all_memory()
            if self._get_bool_config("enable_memory_summary", True)
            else []
        )

        prompt = f"""
            请总结用户{user}与你(AI)的关系:\n
            总结过的全部记忆：\n
            \n
            {memory_list}\n
            仅与该用户的对话历史：\n
            {user_message_history}\n
            \n
            之前的印象：\n
            {pre_impression}\n
            要求：\n
            1. 关系：判断是陌生人、朋友、死党、师生等。\n
            2. 印象：简短描述（如：傲娇、博学、喜欢开玩笑）。\n
            3. 请严格按照 JSON 格式输出！！！，不要包含任何 Markdown 标记！！！。\n
            格式示例：\n
            {{"relationship": "朋友", "impression": "非常幽默"}}
            """
        logger.debug(f"总结提示词: {prompt}")

        # 获取当前会话使用的聊天模型 ID
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"正在进行第{attempt + 1}次重试...")
                    await asyncio.sleep(1)

                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                # logger.info(f"总结前的提示词: {prompt}")
                # 调用大模型
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=dynamic_persona_prompt,  # 让机器人用当前人设去思考印象
                    prompt=prompt,
                )
                llm_output = llm_resp.completion_text
                logger.info(f"总结输出: {llm_output}")

                parse_result = self.parse_llm_json(llm_output)
                if parse_result and "relationship" in parse_result:
                    rel = parse_result["relationship"]
                    imp = parse_result["impression"]

                    # 存入数据库
                    await self.set_sql_relationship_impression(qq_number, rel, imp)

                    # 返回格式化后的字符串，用于插入到 Persona Prompt 中
                    return f"{user}({rel}){qq_number}印象:{imp}。"
                else:
                    logger.warning(
                        f"总结JSON解析失败，重试 {attempt + 1}/{max_retries}"
                    )
            except Exception as e:
                logger.error(f"第 {attempt + 1} 次调用大模型出错: {e}")

        logger.error(f"连续 {max_retries} 次总结均失败，跳过本次更新。")
        return None

    async def memory_summary(self, event: AstrMessageEvent, json_persona_id):
        """调用LLM进行全文总结"""
        if not self._get_bool_config("enable_memory_summary", True):
            return

        # 获取配置文件中指定的历史消息条数
        memory_summary_history_count = self._get_int_config(
            "summary_memory_history_count", 30, min_value=1
        )
        message_history = await self.get_recent_chat_history(
            qq_number=None, n=memory_summary_history_count
        )

        # 获取已有的记忆总结
        existing_memory_summary = await self.get_all_memory()

        # 获取当前的(动态)系统提示词
        dynamic_persona_prompt = await self.get_dynamic_persona_prompt(json_persona_id)

        # 最大总结重试次数
        max_retries = self._get_int_config("summary_max_retries", 3, min_value=1)
        prompt = f"""
            这是你已有的记忆：\n
            {existing_memory_summary}\n
            \n
            请总结以下对话历史，你的输出将添加到已有的记忆末尾:\n
            {message_history}\n
            要求：\n
            1. 总结成一段话，描述主要内容。\n
            2. 严格按照纯文本输出，不要包含任何 Markdown 标记。\n
            3. 输出内容将作为对话背景信息，帮助你更好地记忆。
            """
        # 获取当前会话使用的聊天模型 ID
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"正在进行第{attempt + 1}次重试...")
                    await asyncio.sleep(1)

                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                # 调用大模型
                logger.info(f"调用大模型进行记忆总结，提示词: {prompt}")
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=dynamic_persona_prompt,  # 让机器人用当前人设去思考印象
                    prompt=prompt,
                )
                llm_output = llm_resp.completion_text
                logger.info(f"记忆总结输出: {llm_output}")
                # 检查输出是否为空
                if not llm_output or not llm_output.strip():
                    logger.warning("记忆总结输出为空，跳过存储。")
                    break
                # 将总结结果存入 Memory 表
                await self.add_memory(llm_output.strip())
                await self.compact_memory_if_needed(event, json_persona_id)
                break

            except Exception as e:
                logger.error(f"第 {attempt + 1} 次调用大模型出错: {e}")
        else:
            logger.error(f"连续 {max_retries} 次总结均失败，不更新记忆。")

    async def compact_memory_if_needed(self, event: AstrMessageEvent, json_persona_id):
        """Compact the configured batch when memory exceeds the configured threshold.

        Args:
            event: The event selecting the model provider.
            json_persona_id: The configured base persona ID.
        """
        if not self._get_bool_config("enable_memory_compaction", True):
            logger.debug("Memory 自动压缩已关闭。")
            return

        # Hold a separate lock across selection, LLM generation, and replacement.
        # Ordinary database writes can continue while the model is running.
        async with self._memory_compaction_lock:
            threshold = self._get_int_config(
                "memory_compaction_threshold", 30, min_value=2
            )
            batch_size = self._get_int_config(
                "memory_compaction_batch_size", 20, min_value=2
            )
            if batch_size > threshold:
                logger.warning(
                    f"Memory compaction batch size {batch_size} exceeds threshold "
                    f"{threshold}; limiting the batch to {threshold}"
                )
                batch_size = threshold
            memory_count = await self.get_memory_count()
            if memory_count <= threshold:
                return

            records = await self.get_oldest_memory_records(batch_size)
            if len(records) < batch_size:
                return

            summary_text = await self.summarize_memory_records(
                event, json_persona_id, records
            )
            if not summary_text:
                logger.warning("Memory compaction failed; keeping original records")
                return

            memory_ids = [row[0] for row in records]
            created_at = records[0][2]
            await self.replace_memory_records(memory_ids, summary_text, created_at)

    async def summarize_memory_records(
        self, event: AstrMessageEvent, json_persona_id, records
    ):
        """调用 LLM 将多条 Memory 合并为一条。"""
        memory_text = "\n".join(
            f"{idx + 1}. {row[1]}" for idx, row in enumerate(records) if row[1]
        )
        if not memory_text:
            return None

        dynamic_persona_prompt = await self.get_dynamic_persona_prompt(json_persona_id)
        max_retries = self._get_int_config("summary_max_retries", 3, min_value=1)
        prompt = f"""
            请将以下 {len(records)} 条长期记忆合并压缩为 1 条长期记忆：\n
            {memory_text}\n
            要求：\n
            1. 保留关键事实、人物关系、偏好、承诺和重要上下文。\n
            2. 删除重复、寒暄和无长期价值的内容。\n
            3. 不要添加原文中没有的信息。\n
            4. 严格输出一段纯文本，不要包含 Markdown 标记。
            """

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"正在进行第{attempt + 1}次 Memory 压缩重试...")
                    await asyncio.sleep(1)

                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=dynamic_persona_prompt,
                    prompt=prompt,
                )
                llm_output = llm_resp.completion_text
                if llm_output and llm_output.strip():
                    logger.info("Memory 压缩总结成功")
                    return llm_output.strip()

                logger.warning(f"Memory 压缩输出为空，重试 {attempt + 1}/{max_retries}")
            except Exception as e:
                logger.error(f"第 {attempt + 1} 次 Memory 压缩调用大模型出错: {e}")

        return None

    def merge_AI_and_user_message(self, user_messages, ai_messages, user_name):
        """合并用户和AI的消息记录"""
        ai_personas = self.config.get("personas_name", "AI助手")
        merged_messages = f"""
        {user_name}: \"{user_messages}\" {ai_personas}: \"{ai_messages}\"\n
        """
        return merged_messages.strip()

    # 解析LLM返回的JSON
    def parse_llm_json(self, text):
        """解析 JSON 工具函数"""
        try:
            # 尝试直接解析
            return json.loads(text)
        except Exception:
            pass

        try:
            match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
        except Exception:
            pass

        try:
            match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
            if match:
                return ast.literal_eval(match.group(0))
        except Exception:
            pass

        return None

    # ************* astrbot人格提示词操作函数 **********

    def get_persona_template(self, base_persona_id):
        """从 AstrBot 内存中直接获取人格模板"""
        try:
            # 1. 获取 personas 列表
            all_personas = self.context.provider_manager.personas

            target_persona = None

            for p in all_personas:
                # 获取当前遍历对象的 ID 和 Name
                # p_id = str(p.get("id")) if p.get("id") is not None else "None"
                p_name = str(p.get("name")) if p.get("name") is not None else "None"
                target = str(base_persona_id)

                # if p_id == target or p_name == target:
                if p_name == target:
                    target_persona = p
                    break

            if target_persona:
                logger.info(f"从内存中成功获取人格: {base_persona_id}")

                p_config = target_persona.get("persona_config", {})

                sys_prompt = target_persona.get("prompt")

                # 获取其他属性
                begin_dialogs = p_config.get("begin_dialogs") or target_persona.get(
                    "begin_dialogs", []
                )
                tools = p_config.get("tools") or target_persona.get("tools", [])

                return sys_prompt, begin_dialogs, tools

            else:
                logger.warning(f"内存中未找到名称或 ID 为 '{base_persona_id}' 的人格。")
                return None, None, None

        except Exception as e:
            logger.error(f"获取内存人格数据失败: {e}", exc_info=True)
            return None, None, None

    async def update_dynamic_persona(self, base_persona_id, new_system_prompt):
        """更新或创建astrbot'动态'人格"""
        db = await self._get_db()
        target_dynamic_id = base_persona_id + "动态"

        async with self._db_lock:
            try:
                current_time = datetime.now()

                # 1. 尝试更新
                update_sql = "UPDATE dynamic_personas SET system_prompt = ?, updated_at = ? WHERE persona_id = ?"
                async with db.execute(
                    update_sql, (new_system_prompt, current_time, target_dynamic_id)
                ) as cursor:
                    rowcount = cursor.rowcount

                # 2. 如果不存在则插入
                if rowcount in (0, -1):
                    logger.info(f"动态人格 {target_dynamic_id} 不存在，正在初始化...")

                    # 这里调用同步的内存获取函数
                    template_prompt, template_dialogs, template_tools = (
                        self.get_persona_template(base_persona_id)
                    )

                    if template_prompt is None:
                        await db.rollback()
                        logger.error(
                            f"无法获取基础人格 {base_persona_id}，已回滚动态人格更新事务。"
                        )
                        return

                    # 将 Python 对象 (List/Dict) 序列化为 JSON 字符串
                    if isinstance(template_dialogs, list | dict):
                        template_dialogs = json.dumps(
                            template_dialogs, ensure_ascii=False
                        )
                    if isinstance(template_tools, list | dict):
                        template_tools = json.dumps(template_tools, ensure_ascii=False)

                    insert_sql = """
                    INSERT INTO dynamic_personas
                    (persona_id, system_prompt, begin_dialogs, tools, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """
                    await db.execute(
                        insert_sql,
                        (
                            target_dynamic_id,
                            new_system_prompt,
                            template_dialogs,
                            template_tools,
                            current_time,
                            current_time,
                        ),
                    )

                await db.commit()
                logger.info(f"成功更新 ID 为 {target_dynamic_id} 的人格提示词。")

            except Exception as e:
                logger.error(f"设置动态人格提示词失败: {e}")
                await db.rollback()

    async def write_astrbot_persona_prompt(self, base_persona_id, summary_text):
        """从最新模板组装动态人格，仅在内容变化时保存。

        参数：
            base_persona_id: 需要使用的人格名称。
            summary_text: 当前完整的人物关系与印象。

        返回：
            本次组装的提示词；模板不存在或组装失败时返回 None。
        """
        try:
            # 1. 获取带有 {Impression} 的原始模板
            raw_prompt, _, _ = self.get_persona_template(base_persona_id)

            if not raw_prompt:
                logger.error("无法获取模板，停止更新。")
                return

            # 2. 执行替换逻辑
            if "{Impression}" in raw_prompt:
                formatted_prompt = raw_prompt.replace("{Impression}", str(summary_text))
                # logger.info(f"占位符替换成功,替换后:{formatted_prompt}")

            else:
                # 兜底：如果没有占位符，追加到末尾
                logger.debug("模板中未找到 {Impression} 占位符，将追加到末尾。")
                formatted_prompt = raw_prompt + f"\n\n关于用户的印象：{summary_text}"

            if self._get_bool_config("enable_memory_summary", True):
                recent_memory = await self.get_recent_memory()
                memory_summary_text = (
                    "\n".join(recent_memory) if recent_memory else "暂无记忆总结。"
                )
                if "{Memory}" in formatted_prompt:
                    formatted_prompt = formatted_prompt.replace(
                        "{Memory}", memory_summary_text
                    )
                elif recent_memory:
                    formatted_prompt += f"\n\n历史对话记忆：\n{memory_summary_text}"
            else:
                # Keep stored memories while removing the disabled prompt slot.
                formatted_prompt = formatted_prompt.replace("{Memory}", "")

            # 内容未变化时只读取；返回本次结果，不再读取可能被其他请求覆盖的旧副本。
            stored_prompt = await self.get_dynamic_persona(base_persona_id + "动态")
            if stored_prompt != formatted_prompt:
                await self.update_dynamic_persona(base_persona_id, formatted_prompt)
            return formatted_prompt

        except Exception as e:
            logger.error(f"替换人格提示词流程失败: {e}")

    async def get_dynamic_persona_prompt(self, persona_id):
        """获取基于当前人格模板和已有记忆组装的提示词。

        参数：
            persona_id: 当前需要使用的人格名称。

        返回：
            最新提示词；人格不存在或组装失败时返回空字符串，不使用历史副本。
        """
        current_impression = await self.get_sql_relationship_impression()
        prompt = await self.write_astrbot_persona_prompt(persona_id, current_impression)
        return prompt or ""

    async def terminate(self):
        """Stop startup synchronization before closing the database."""
        if not self._startup_task.done():
            self._startup_task.cancel()
        try:
            await self._startup_task
        except asyncio.CancelledError:
            pass
        async with self._db_lock:
            if self.db:
                try:
                    await self.db.close()
                    logger.info("PersonaFlow database connection closed")
                except Exception as e:
                    logger.error(f"Failed to close PersonaFlow database: {e}")
                finally:
                    self.db = None

        # ************* 指令部分 **********

    @filter.command_group("osn")
    def osn(self):
        pass

    @osn.command("check")
    async def check_impression(self, event: AstrMessageEvent):
        """
        查看数据库中所有已保存的人物印象
        """
        db = await self._get_db()
        try:
            sql = "SELECT qq_number, name, relationship, impression, dialogue_count FROM Impression"
            async with db.execute(sql) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                yield event.plain_result("📂 数据库中暂无任何印象记录。")
                return

            msg_list = ["📂 当前已存储的人物印象：", "=" * 20]

            for row in rows:
                uid = row[0]
                name = row[1] if row[1] else "未知"
                rel = row[2] if row[2] else "暂无"
                imp = row[3] if row[3] else "暂无"
                count = row[4] if row[4] is not None else 0

                info = (
                    f"👤 用户: {name} ({uid})\n"
                    f"🔗 关系: {rel}\n"
                    f"🧠 印象: {imp}\n"
                    f"💬 统计: {count}次对话"
                )
                msg_list.append(info)
                msg_list.append("-" * 20)

            # 避免消息过长，简单合并
            result_text = "\n".join(msg_list)
            yield event.plain_result(result_text)

        except Exception as e:
            logger.error(f"查询数据库失败: {e}")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @osn.command("del")
    async def delete_memory(self, event: AstrMessageEvent, target_id: str):
        """
        删除指定用户的关系与记忆
        用法: /osn del <user_id>
        """
        if not target_id:
            yield event.plain_result("❌ 请输入要删除的用户ID。例如: /osn del 123456")
            return

        # 获取配置文件中的基础人格ID (用于后续更新 Prompt)
        json_persona_id = self.config.get("personas_name", "")
        if not json_persona_id:
            yield event.plain_result(
                "⚠️ 警告：配置文件中未设置 personas_name，仅删除数据，无法刷新动态人格。"
            )

        db = await self._get_db()
        user_name = "未知用户"

        # 执行数据库删除操作 (在一个事务锁中完成)
        async with self._db_lock:
            try:
                # 检查用户是否存在
                async with db.execute(
                    "SELECT name FROM Impression WHERE qq_number = ?", (target_id,)
                ) as cursor:
                    res = await cursor.fetchone()

                if not res:
                    yield event.plain_result(f"⚠️ 未找到 ID 为 {target_id} 的记录。")
                    return

                user_name = res[0]

                # 删除印象表记录
                await db.execute(
                    "DELETE FROM Impression WHERE qq_number = ?", (target_id,)
                )

                # 删除聊天记录表记录
                await db.execute(
                    "DELETE FROM Message WHERE qq_number = ?", (target_id,)
                )

                await db.commit()
                logger.info(f"已从数据库删除用户 {user_name}({target_id}) 的所有数据")

            except Exception as e:
                await db.rollback()
                logger.error(f"删除数据失败: {e}")
                yield event.plain_result(f"❌ 删除失败: {e}")
                return

        # 只有配置了人格ID才执行更新
        if json_persona_id:
            try:
                yield event.plain_result(f"🗑️ 已删除 [{user_name}]，正在重构全员记忆...")

                # A. 获取删除该用户后，剩余所有人的印象文本
                new_full_impression = await self.get_sql_relationship_impression()

                # B. 调用写入逻辑，这会自动：
                #    1. 读取原始模板
                #    2. 替换 {Impression}
                #    3. 更新数据库 dynamic_personas 表
                await self.write_astrbot_persona_prompt(
                    json_persona_id, new_full_impression
                )

                yield event.plain_result(
                    f"✅ 成功！[{user_name}] ({target_id}) 已被遗忘，当前人格记忆已刷新。"
                )

            except Exception as e:
                logger.error(f"刷新动态人格失败: {e}")
                yield event.plain_result(f"⚠️ 数据已删除，但在刷新人格记忆时出错: {e}")
        else:
            yield event.plain_result(
                "✅ 数据已删除，但因未配置 personas_name，未刷新当前人格。"
            )

    @osn.command("checkmem")
    async def check_memory(self, event: AstrMessageEvent):
        """
        查看memory表中所有记忆内容
        """
        db = await self._get_db()
        try:
            sql = "SELECT memory, created_at FROM Memory ORDER BY created_at ASC"
            async with db.execute(sql) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                yield event.plain_result("📂 数据库中暂无任何记忆记录。")
                return

            msg_list = ["📂 当前已存储的记忆记录：", "=" * 20]

            for row in rows:
                mem = row[0] if row[0] else "无内容"
                time = row[1] if row[1] else "未知时间"

                info = f"🕒 时间: {time}\n🧠 记忆内容: {mem}\n"
                msg_list.append(info)
                msg_list.append("-" * 20)

            result_text = "\n".join(msg_list)
            yield event.plain_result(result_text)

        except Exception as e:
            logger.error(f"查询数据库失败: {e}")
            yield event.plain_result(f"❌ 查询失败: {e}")
