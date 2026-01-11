import ast
import asyncio
import json
import re
import sqlite3
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

"""
版本0.5.2使用Ruff格式化代码
"""


@register(
    "astrbot_plugin_PersonaFlow",
    "yizyin",
    "由ai自动总结人物关系到数据库，实现在不同群聊记住同一个人之间与ai的关系和印象。",
    "0.5.2(Beta)",
)
class PersonaFlow(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        database_path = self.config.get("database_path", "./data/OSNpermemory.db")
        self.database(database_path)
        # cursor = self.db.cursor()
        logger.info("人格关系流(PersonaFlow) v0.01 加载成功!")

    # ************数据库操作函数**********
    def database(self, db_path):
        """ "初始化数据库连接"""

        try:
            self.db = sqlite3.connect(db_path, check_same_thread=False)
            cursor = self.db.cursor()
            # 创建表格（如果不存在）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS Impression (
                    qq_number TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    relationship TEXT,
                    impression TEXT,
                    dialogue_count INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                create table if not exists Message (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_number TEXT not null,
                    message TEXT,
                    chat_time datetime DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
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
            self.db.commit()
            cursor.close()
            logger.info("数据库连接成功")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            if self.db:
                self.db.rollback()

    def insert_user(self, qq_number, user_name):
        """插入用户信息到数据库"""
        cursor = self.db.cursor()
        try:
            sql = "INSERT INTO Impression (qq_number, name) VALUES (?, ?)"
            cursor.execute(sql, (qq_number, user_name))
            self.db.commit()
            logger.info(f"用户 {user_name}与 {qq_number} 成功插入数据库")
        except Exception as e:
            logger.error(f"插入用户失败: {e}")
            self.db.rollback()
        finally:
            cursor.close()

    def select_Dcount(self, qq_number):
        """查询对话次数"""
        cursor = self.db.cursor()
        try:
            sql = "SELECT D_COUNT FROM Impression WHERE qq_number = ?"
            cursor.execute(sql, (qq_number,))
            result = cursor.fetchone()
            count = result[0] if result[0] is not None else "无"
            # logger.info(f"对话次数: {count}")
            return count
        except Exception as e:
            logger.error(f"查询对话次数失败: {e}")
            return 0
        finally:
            cursor.close()

    def increment_dialogue_count(self, qq_number):
        """对话次数+1"""
        cursor = self.db.cursor()
        try:
            sql = "UPDATE Impression SET dialogue_count = dialogue_count + 1 WHERE qq_number = ?"
            cursor.execute(sql, (qq_number,))
            self.db.commit()
            # logger.info("对话次数更新成功")
        except Exception as e:
            logger.error(f"更新对话次数失败: {e}")
            self.db.rollback()
        finally:
            cursor.close()

    def set_sql_relationship_impression(self, qq_number, relationship, impression):
        """更新关系与印象"""
        cursor = self.db.cursor()
        try:
            sql = "UPDATE Impression SET relationship = ?, impression = ? WHERE qq_number = ?"
            cursor.execute(sql, (relationship, impression, qq_number))
            self.db.commit()
            logger.info("关系与印象更新成功")
        except Exception as e:
            logger.error(f"更新关系与印象失败: {e}")
            self.db.rollback()
        finally:
            cursor.close()

    def get_sql_relationship_impression(self, qq_number, user):
        """获取全部关系与印象"""
        cursor = self.db.cursor()
        try:
            # 1. 查询需要的四个字段
            sql = "SELECT qq_number, name, relationship, impression FROM Impression"
            cursor.execute(sql)

            # 2. 获取所有结果 (fetchall)
            results = cursor.fetchall()

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
        finally:
            cursor.close()

    def add_persona_chat_history(self, qq_number, message):
        """添加用户的聊天记录到数据库"""
        cursor = self.db.cursor()
        try:
            sql = "INSERT INTO Message (qq_number, message) VALUES (?, ?)"
            cursor.execute(sql, (qq_number, message))
            self.db.commit()
            # logger.info(f"用户 {qq_number} 的聊天记录成功插入数据库")
        except Exception as e:
            logger.error(f"插入聊天记录失败: {e}")
            self.db.rollback()
        finally:
            cursor.close()

    def get_n_Message_chat_history(self, qq_number, n):
        """获取用户的最近n条聊天记录"""
        cursor = self.db.cursor()
        try:
            sql = "SELECT message FROM Message WHERE qq_number = ? ORDER BY chat_time DESC LIMIT ?"
            cursor.execute(sql, (qq_number, n))
            results = cursor.fetchall()
            messages = [row[0] for row in results]
            logger.info(f"成功获取用户 {qq_number} 的最近 {n} 条聊天记录")
            return messages
        except Exception as e:
            logger.error(f"获取聊天记录失败: {e}")
            return []
        finally:
            cursor.close()

    def get_dynamic_persona(self, p_id: str):
        cursor = self.db.cursor()
        try:
            sql = "SELECT system_prompt FROM dynamic_personas WHERE persona_id = ?"
            cursor.execute(sql, (p_id,))
            result = cursor.fetchone()

            if result:
                logger.info(f"成功获取人格: {p_id}")

                return result[0]  # 返回字符串
            else:
                logger.warning(f"未找到人格 ID: {p_id}")
                return None
        except Exception as e:
            logger.error(f"数据库查询失败: {e}")
            return None
        finally:
            cursor.close()

    def update_user_name_only(self, qq_number, name):
        cursor = self.db.cursor()
        try:
            # 【修正1】标准的 SQL Update 语法: UPDATE 表名 SET 字段=值 WHERE 条件
            sql = "UPDATE Impression SET name = ? WHERE qq_number = ?"
            cursor.execute(sql, (name, qq_number))

            # 【修正2】必须提交事务，否则不会保存
            self.db.commit()

            logger.info(f"更新用户 {qq_number} 昵称为: {name}")
        except Exception as e:
            logger.error(f"更新user_name失败: {e}")
            self.db.rollback()  # 建议出错回滚
        finally:
            cursor.close()

    # ************事件处理函数**********

    @filter.on_llm_request()
    async def my_custom_hook_1(self, event: AstrMessageEvent, req: ProviderRequest):
        current_session_id = event.get_session_id()
        active_session_ids = self.config.get("apply_to_group_chat", [])

        # 如果列表为空，默认全部开启；或者判断是否在列表中
        if not active_session_ids or current_session_id in active_session_ids:
            # 获取配置文件中的基础人格ID
            json_persona_id = self.config.get("personas_name", "")
            if not json_persona_id:
                return  # 没配置人格，不做操作

            target_dynamic_id = json_persona_id + "动态"
            dynamic_prompt = self.get_dynamic_persona(target_dynamic_id)
            # logger.info(f"使用的system prompt:{dynamic_prompt}")

            if dynamic_prompt:
                # 覆盖请求中的 System Prompt
                req.system_prompt = dynamic_prompt
                # logger.debug(f"已应用动态人格: {target_dynamic_id}") # 避免刷屏，改用debug
            else:
                logger.warning(f"动态人格 [{target_dynamic_id}] 尚未生成，将使用默认。")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # 获取当前会话id
        current_session_id = event.get_session_id()

        # 获取json会话id
        active_session_id = self.config.get("apply_to_group_chat", [])

        # logger.info(f"json会话id:{active_session_id}")

        # 判断当前对话是否属于配置文件中设定的对话
        if current_session_id in active_session_id:
            try:
                new_name = event.get_sender_name()
                qq_number = event.get_sender_id()
                user_message = event.get_message_str()
                message = self.merge_AI_and_user_message(
                    user_message, resp.completion_text, new_name
                )

                # 1. 先存聊天记录
                self.add_persona_chat_history(qq_number, message)

                # 2. 检查用户是否存在 (先读取，读完立刻关 cursor)
                user_exists = False
                db_name = None

                cursor = self.db.cursor()
                try:
                    sql = "SELECT name FROM Impression WHERE qq_number = ?"
                    cursor.execute(sql, (qq_number,))
                    result = cursor.fetchone()
                    if result:
                        user_exists = True
                        db_name = result[0]
                finally:
                    cursor.close()  # 读完立刻关闭

                # 3. 根据读取结果进行写操作
                if user_exists:
                    # 用户存在，检查是否改名
                    if new_name != db_name:
                        # 这里直接开一个新的 cursor 进行更新，或者封装一个 update_user_name 函数
                        self.update_user_name_only(
                            qq_number, new_name
                        )  # 建议封装成小函数，或者直接在这里写 update 逻辑
                else:
                    # 用户不存在，插入
                    self.insert_user(qq_number, new_name)

                # 4. 增加对话次数
                self.increment_dialogue_count(qq_number)

            except Exception as e:
                logger.error(f"处理用户数据失败: {e}")

            # 获取json生效人格设定
            json_persona_id = self.config.get("personas_name", "")
            # logger.info(f"json_persona_id：{json_persona_id}")
            # 总结触发逻辑
            try:
                summary_trigger_threshold = self.config.get(
                    "summary_trigger_threshold", 5
                )
                qq_number = event.get_sender_id()
                dialogue_count = self.select_Dcount(qq_number)
                if dialogue_count % summary_trigger_threshold == 0:
                    await self.llm_summary(event, new_name, qq_number, json_persona_id)
                    pre_prompt = self.get_sql_relationship_impression(
                        qq_number, new_name
                    )
                    self.write_astrbot_persona_prompt(json_persona_id, pre_prompt)
            except Exception as e:
                logger.error(f"获取对话次数失败: {e}")
        else:
            logger.info("当前会话不在设置，未执行代码")

    async def llm_summary(
        self, event: AstrMessageEvent, user, qq_number, json_persona_id
    ):
        """调用LLM进行总结印象和关系"""
        user = user
        logger.info(f"开始调用大模型进行总结，用户: {user}")

        # 最大总结次数
        max_retries = self.config.get("summary_max_retries", 3)

        # 总结时获取对应用户聊天记录条数
        summary_history_count = self.config.get("summary_history_count", 20)
        user_Message_history = self.get_n_Message_chat_history(
            event.get_sender_id(), n=summary_history_count
        )

        # 获取数据库中的关系和印象
        pre_impression = self.get_sql_relationship_impression(qq_number, user)

        # 获取当前的(动态)系统提示词
        dynamic_persona_prompt = self.get_dynamic_persona_prompt(json_persona_id)

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
            4. 请严格按照 JSON 格式输出！！！\n
            5. 请严格按照 JSON 格式输出！！！\n
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
                logger.info(f"当前会话使用的聊天模型 ID: {provider_id}")

                # logger.info(f"总结前的提示词: {prompt}")
                # 调用大模型
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,  # 聊天模型 ID
                    system_prompt=dynamic_persona_prompt,
                    prompt=prompt,
                )
                llm_output = llm_resp.completion_text
                logger.info(f"总结后: {llm_output}")  # 获取返回的文本
                # 解析json
                parse_result = self.parse_llm_json(llm_output)
                if parse_result and "relationship" in parse_result:
                    rel = parse_result["relationship"]
                    imp = parse_result["impression"]

                    # 存入数据库
                    self.set_sql_relationship_impression(qq_number, rel, imp)

                    # 返回格式化后的字符串，用于插入到 Persona Prompt 中
                    return f"{user}({rel}){qq_number}印象:{imp}。"
                else:
                    logger.warning(
                        f"第 {attempt + 1} 次总结解析失败: 未找到有效 JSON 字段。返回内容: {llm_output[:50]}..."
                    )
                    return None
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

    # *************astrbot人格提示词操作函数**********

    def get_persona_template(self, base_persona_id):
        """
        用于获取原始的、带有 {Impression} 占位符的模板。
        return system_prompt, begin_dialogs, tools
        """
        astrbot_db = None
        try:
            astrbot_db = sqlite3.connect("./data/data_v4.db")
            astrbot_cursor = astrbot_db.cursor()

            # 只查询原始 ID，确保获取到的是带有占位符的模板
            sql_select = "SELECT system_prompt, begin_dialogs, tools FROM personas WHERE persona_id = ?"
            astrbot_cursor.execute(sql_select, (base_persona_id,))
            result = astrbot_cursor.fetchone()

            if not result:
                logger.warning(f"未找到原始 ID 为 {base_persona_id} 的人格模板。")
                return None, None, None  # 返回空以示失败

            # 返回模板数据，供后续生成动态人格使用
            return result[0], result[1], result[2]

        except Exception as e:
            logger.error(f"获取人格模板失败: {e}")
            return None, None, None
        finally:
            if astrbot_db:
                astrbot_db.close()

    def update_dynamic_persona(self, base_persona_id, new_system_prompt):
        """
        更新或创建astrbot'动态'人格。
        使用 UPDATE 而不是 REPLACE，以防丢失 tools 等数据。
        如果不存在动态人格，则先从 base 复制一份。
        """
        cursor = self.db.cursor()

        target_dynamic_id = base_persona_id + "动态"
        try:
            current_time = datetime.now()

            # 1. 先尝试更新
            update_sql = "UPDATE dynamic_personas SET system_prompt = ?, updated_at = ? WHERE persona_id = ?"
            cursor.execute(
                update_sql, (new_system_prompt, current_time, target_dynamic_id)
            )

            # 2. 如果影响行数为0，说明动态ID还不存在，需要初始化插入 (INSERT)
            if cursor.rowcount == 0:
                logger.info(f"动态人格 {target_dynamic_id} 不存在，正在初始化...")

                template_prompt, template_dialogs, template_tools = (
                    self.get_persona_template(base_persona_id)
                )

                if template_prompt is None:
                    return

                insert_sql = """
                INSERT INTO dynamic_personas
                (persona_id, system_prompt, begin_dialogs, tools, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """
                cursor.execute(
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

            self.db.commit()
            logger.info(f"成功更新 ID 为 {target_dynamic_id} 的人格提示词。")

        except Exception as e:
            logger.error(f"设置动态人格提示词失败: {e}")
            if self.db:
                self.db.rollback()
        finally:
            cursor.close()

    def write_astrbot_persona_prompt(self, base_persona_id, summary_text):
        """
        主入口：读取模板 -> 替换占位符 -> 保存为动态人格
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
                # 如果模板里没有占位符，直接追加到最后（做个兜底）
                logger.warning("模板中未找到 {Impression} 占位符，将追加到末尾。")
                formatted_prompt = raw_prompt + f"\n\n关于用户的印象：{summary_text}"

            # 3. 保存到动态 ID 数据库中
            self.update_dynamic_persona(base_persona_id, formatted_prompt)

        except Exception as e:
            logger.error(f"替换人格提示词流程失败: {e}")

    def get_dynamic_persona_prompt(self, persona_id):
        """
        1. 尝试从本地插件数据库获取 '动态' ID。
        2. 如果没有，从主数据库获取 '原始' ID。
        """
        # 1. 构造动态ID
        dynamic_id = persona_id + "动态"

        # 2. 尝试从本地数据库 (self.db) 获取
        local_prompt = self.get_dynamic_persona(dynamic_id)

        if local_prompt:
            return local_prompt
        else:
            # 3. 如果本地没有，去主数据库读取原始模板作为兜底
            logger.warning(f"动态人格 {dynamic_id} 尚未生成，降级读取原始人格。")
            prompt, _, _ = self.get_persona_template(persona_id)
            return prompt if prompt else ""

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        try:
            if self.db:
                self.db.close()
                logger.info("全局关系网数据库连接已关闭。")
        except Exception as e:
            logger.error(f"关闭数据库连接失败: {e}")
