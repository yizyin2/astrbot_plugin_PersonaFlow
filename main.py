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
from astrbot.api.star import Context, Star, register

"""
版本0.6
使用self.context.provider_manager.personas方法获取astrbot人格system prompt
从sqlite3变更为aiosqlite
"""


@register(
    "astrbot_plugin_PersonaFlow",
    "yizyin",
    "由ai自动总结人物关系到数据库，实现在不同群聊记住同一个人之间与ai的关系和印象。",
    "0.6(Beta)",
)
class PersonaFlow(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.db_path = self.config.get("database_path", "./data/OSNpermemory.db")
        self.db = None  # 数据库连接对象初始化为None
        self._db_lock = asyncio.Lock()  # 1. 添加锁解决并发初始化问题
        self.cached_dynamic_prompt = None  # 2. 添加内存缓存，避免每次对话读库

        # 4. 确保目录存在
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        logger.info("人格关系流(PersonaFlow) v0.6加载成功!")

    # ************数据库操作函数**********
    async def _get_db(self):
        """懒加载获取数据库连接"""
        if self.db is None:
            async with self._db_lock:  # 双重检查锁定
                if self.db is None:
                    try:
                        self.db = await aiosqlite.connect(
                            self.db_path, check_same_thread=False
                        )
                        # 开启 WAL 模式以获得更好的并发性能
                        await self.db.execute("PRAGMA journal_mode=WAL;")
                        await self._init_tables(self.db)
                        logger.info("数据库连接并初始化成功")
                    except Exception as e:
                        logger.error(f"数据库连接失败: {e}")
                        if self.db:
                            await self.db.close()
                        self.db = None
                        raise e
        return self.db

    async def _init_tables(self, db):
        """初始化表格"""
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
            await db.commit()
        except Exception as e:
            logger.error(f"建表失败: {e}")
            await db.rollback()

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

    async def select_Dcount(self, qq_number):
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

    async def get_sql_relationship_impression(self, qq_number, user):
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

    async def get_n_Message_chat_history(self, qq_number, n):
        """获取用户的最近n条聊天记录"""
        db = await self._get_db()
        try:
            sql = "SELECT message FROM Message WHERE qq_number = ? ORDER BY chat_time DESC LIMIT ?"
            async with db.execute(sql, (qq_number, n)) as cursor:
                results = await cursor.fetchall()
            messages = [row[0] for row in results]
            logger.info(f"成功获取用户 {qq_number} 的最近 {n} 条聊天记录")
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

    # ************ 事件处理函数 **********

    @filter.on_llm_request()
    async def inject_dynamic_persona(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        current_session_id = str(event.get_session_id())  # 6. 强转字符串

        # 6. 配置项强转字符串进行比对
        active_session_ids = [
            str(x) for x in self.config.get("apply_to_group_chat", [])
        ]

        if not active_session_ids or current_session_id in active_session_ids:
            # 获取配置文件中的基础人格ID
            json_persona_id = self.config.get("personas_name", "")
            if not json_persona_id:
                return

            # 2. 优先使用缓存
            if self.cached_dynamic_prompt:
                req.system_prompt = self.cached_dynamic_prompt
                # logger.debug(f"已应用缓存的动态人格")
                return

            # 缓存未命中，查库
            target_dynamic_id = json_persona_id + "动态"
            dynamic_prompt = await self.get_dynamic_persona(target_dynamic_id)
            # logger.info(f"使用的system prompt:{dynamic_prompt}")

            if dynamic_prompt:
                self.cached_dynamic_prompt = dynamic_prompt  # 更新缓存
                req.system_prompt = dynamic_prompt
                # logger.debug(f"已应用动态人格: {target_dynamic_id}")
            else:
                # 第一次运行时可能没有动态人格，此时不做操作，让AstrBot使用默认加载的
                pass

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # 获取当前会话id
        current_session_id = str(event.get_session_id())

        # 获取json会话id
        active_session_ids = [
            str(x) for x in self.config.get("apply_to_group_chat", [])
        ]

        # logger.info(f"json会话id:{active_session_id}")

        # 判断当前对话是否属于配置文件中设定的对话
        if not active_session_ids or current_session_id in active_session_ids:
            # 提前定义变量，防止try块外引用报错
            new_name = "未知用户"
            qq_number = "0"

            try:
                new_name = event.get_sender_name()
                qq_number = event.get_sender_id()
                user_message = event.get_message_str()

                # 防止空消息报错
                if not user_message or not resp.completion_text:
                    return

                message = self.merge_AI_and_user_message(
                    user_message, resp.completion_text, new_name
                )

                # 确保数据库已连接
                await self._get_db()

                # 1. 先存聊天记录
                await self.add_persona_chat_history(qq_number, message)

                # 2. 检查用户是否存在
                user_exists = False
                db_name = None

                # 直接操作 db 避免反复获取连接
                db = await self._get_db()
                sql = "SELECT name FROM Impression WHERE qq_number = ?"
                async with db.execute(sql, (qq_number,)) as cursor:
                    result = await cursor.fetchone()
                    if result:
                        user_exists = True
                        db_name = result[0]

                # 3. 读写分离逻辑
                if user_exists:
                    # 用户存在，检查是否改名
                    if new_name != db_name:
                        await self.update_user_name_only(qq_number, new_name)
                else:
                    # 用户不存在，插入
                    await self.insert_user(qq_number, new_name)

                # 4. 增加对话次数
                await self.increment_dialogue_count(qq_number)

            except Exception as e:
                logger.error(f"处理用户数据失败: {e}", exc_info=True)
                return

            # 获取json生效人格设定
            json_persona_id = self.config.get("personas_name", "")
            # logger.info(f"json_persona_id：{json_persona_id}")
            # 总结触发逻辑
            try:
                summary_trigger_threshold = self.config.get(
                    "summary_trigger_threshold", 5
                )
                qq_number = event.get_sender_id()
                dialogue_count = await self.select_Dcount(qq_number)

                if (
                    dialogue_count > 0
                    and dialogue_count % summary_trigger_threshold == 0
                ):
                    # 获取之前的印象文本
                    await self.get_sql_relationship_impression(
                        qq_number, new_name
                    )

                    # 执行 LLM 总结
                    summary_result = await self.llm_summary(
                        event, new_name, qq_number, json_persona_id
                    )

                    # 如果总结成功（返回了字符串），则更新 System Prompt
                    if summary_result:
                        # 重新获取最新的完整印象列表（包含刚更新的）
                        new_full_impression = (
                            await self.get_sql_relationship_impression(
                                qq_number, new_name
                            )
                        )
                        await self.write_astrbot_persona_prompt(
                            json_persona_id, new_full_impression
                        )

            except Exception as e:
                logger.error(f"总结触发流程失败: {e}")
        else:
            logger.info("当前会话不在设置，未执行代码")
            pass

    async def llm_summary(
        self, event: AstrMessageEvent, user, qq_number, json_persona_id
    ):
        """调用LLM进行总结印象和关系"""
        logger.info(f"开始调用大模型进行总结，用户: {user}")

        # 最大总结次数
        max_retries = self.config.get("summary_max_retries", 3)

        # 总结时获取对应用户聊天记录条数
        summary_history_count = self.config.get("summary_history_count", 20)

        user_Message_history = await self.get_n_Message_chat_history(
            event.get_sender_id(), n=summary_history_count
        )

        # 获取数据库中的关系和印象
        pre_impression = await self.get_sql_relationship_impression(qq_number, user)

        # 获取当前的(动态)系统提示词
        dynamic_persona_prompt = await self.get_dynamic_persona_prompt(json_persona_id)

        prompt = f"""
            对话历史：\n
            {user_Message_history}\n
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

    def merge_AI_and_user_message(self, user_messages, ai_messages, user_name):
        """合并用户和AI的消息记录"""
        ai_personas = self.config.get("personas_name", "AI助手")
        merged_messages = f"""
            用户({user_name}): {user_messages}
            {ai_personas}: {ai_messages}
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
                if rowcount == 0:
                    logger.info(f"动态人格 {target_dynamic_id} 不存在，正在初始化...")

                    # 这里调用同步的内存获取函数
                    template_prompt, template_dialogs, template_tools = (
                        self.get_persona_template(base_persona_id)
                    )

                    if template_prompt is None:
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
                self.cached_dynamic_prompt = new_system_prompt
                logger.info(f"成功更新 ID 为 {target_dynamic_id} 的人格提示词。")

            except Exception as e:
                logger.error(f"设置动态人格提示词失败: {e}")
                await db.rollback()

    async def write_astrbot_persona_prompt(self, base_persona_id, summary_text):
        """逻辑整合函数"""
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
                logger.warning("模板中未找到 {Impression} 占位符，将追加到末尾。")
                formatted_prompt = raw_prompt + f"\n\n关于用户的印象：{summary_text}"

            # 3. 保存到动态 ID 数据库中
            await self.update_dynamic_persona(base_persona_id, formatted_prompt)

        except Exception as e:
            logger.error(f"替换人格提示词流程失败: {e}")

    async def get_dynamic_persona_prompt(self, persona_id):
        """获取Prompt"""
        dynamic_id = persona_id + "动态"
        # 优先读缓存
        if self.cached_dynamic_prompt:
            return self.cached_dynamic_prompt

        local_prompt = await self.get_dynamic_persona(dynamic_id)

        if local_prompt:
            return local_prompt
        else:
            # 如果本地没有，去主数据库读取原始模板作为兜底
            logger.warning(f"动态人格 {dynamic_id} 尚未生成，降级读取原始人格。")
            prompt, _, _ = self.get_persona_template(persona_id)
            return prompt if prompt else ""

    async def terminate(self):
        """插件卸载时关闭连接"""
        if self.db:
            try:
                await self.db.close()
                logger.info("PersonaFlow 数据库连接已关闭。")
            except Exception as e:
                logger.error(f"关闭数据库连接失败: {e}")
