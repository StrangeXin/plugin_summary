# encoding:utf-8

import json
import os,re
import time

from urllib.parse import urlparse
import requests

from bot import bot_factory
from bridge.bridge import Bridge
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import check_contain, check_prefix
from channel.chat_message import ChatMessage
from config import conf
import plugins
from plugins import *
from common.log import logger
from common import const
import sqlite3
from chatgpt_tool_hub.chains.llm import LLMChain
from chatgpt_tool_hub.models import build_model_params
from chatgpt_tool_hub.models.model_factory import ModelFactory
from chatgpt_tool_hub.prompts import PromptTemplate
TRANSLATE_PROMPT = '''
You are now the following python function: 
```# {{translate text to commands}}"
        def translate_text(text: str) -> str:
```
Only respond with your `return` value, Don't reply anything else.

Commands:
{{Summary chat logs}}: "summary", args: {{("duration_in_seconds"): <integer>, ("count"): <integer>}}
{{Do Nothing}}:"do_nothing",  args:  {{}}

argument in brackets means optional argument.

You should only respond in JSON format as described below.
Response Format: 
{{
    "name": "command name", 
    "args": {{"arg name": "value"}}
}}
Ensure the response can be parsed by Python json.loads.

Input: {input}
'''
def find_json(json_string):
    json_pattern = re.compile(r"\{[\s\S]*\}")
    json_match = json_pattern.search(json_string)
    if json_match:
        json_string = json_match.group(0)
    else:
        json_string = ""
    return json_string
@plugins.register(
    name="summary",
    desire_priority=100,
    hidden=True,
    desc="A simple plugin to summary messages",
    version="0.3.3",
    author="lanvent"
)
class Summary(Plugin):
    def __init__(self):
        super().__init__()
        
        curdir = os.path.dirname(__file__)
        db_path = os.path.join(curdir, "chat.db")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS chat_records
                    (sessionid TEXT, msgid INTEGER, user TEXT, content TEXT, type TEXT, timestamp INTEGER, is_triggered INTEGER,
                    PRIMARY KEY (sessionid, msgid))''')
        
        # 后期增加了is_triggered字段，这里做个过渡，这段代码某天会删除
        c = c.execute("PRAGMA table_info(chat_records);")
        column_exists = False
        for column in c.fetchall():
            logger.debug("[Summary] column: {}" .format(column))
            if column[1] == 'is_triggered':
                column_exists = True
                break
        if not column_exists:
            self.conn.execute("ALTER TABLE chat_records ADD COLUMN is_triggered INTEGER DEFAULT 0;")
            self.conn.execute("UPDATE chat_records SET is_triggered = 0;")

        self.conn.commit()

        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
        self.open_ai_api_base = conf().get("open_ai_api_base", "https://api.openai.com/v1")
        self.open_ai_api_key = conf().get("open_ai_api_key", "")
        self.open_ai_model = conf().get("open_ai_model", "gpt-3.5-turbo")
        logger.info("[Summary] inited")

    def _insert_record(self, session_id, msg_id, user, content, msg_type, timestamp, is_triggered = 0):
        c = self.conn.cursor()
        logger.debug("[Summary] insert record: {} {} {} {} {} {} {}" .format(session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        c.execute("INSERT OR REPLACE INTO chat_records VALUES (?,?,?,?,?,?,?)", (session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        self.conn.commit()
    
    def _get_records(self, session_id, start_timestamp=0, limit=9999):
        c = self.conn.cursor()
        c.execute("SELECT * FROM chat_records WHERE sessionid=? and timestamp>? ORDER BY timestamp DESC LIMIT ?", (session_id, start_timestamp, limit))
        return c.fetchall()

    def on_receive_message(self, e_context: EventContext):
        context = e_context['context']
        cmsg : ChatMessage = e_context['context']['msg']
        username = None
        session_id = cmsg.from_user_id
        if conf().get('channel_type', 'wx') == 'wx' and cmsg.from_user_nickname is not None:
            session_id = cmsg.from_user_nickname # itchat channel id会变动，只好用群名作为session id

        if context.get("isgroup", False):
            username = cmsg.actual_user_nickname
            if username is None:
                username = cmsg.actual_user_id
        else:
            username = cmsg.from_user_nickname
            if username is None:
                username = cmsg.from_user_id

        is_triggered = False
        content = context.content
        if context.get("isgroup", False): # 群聊
            # 校验关键字
            match_prefix = check_prefix(content, conf().get('group_chat_prefix'))
            match_contain = check_contain(content, conf().get('group_chat_keyword'))
            if match_prefix is not None or match_contain is not None:
                is_triggered = True
            if context['msg'].is_at and not conf().get("group_at_off", False):
                is_triggered = True
        else: # 单聊
            match_prefix = check_prefix(content, conf().get('single_chat_prefix',['']))
            if match_prefix is not None:
                is_triggered = True

        self._insert_record(session_id, cmsg.msg_id, username, context.content, str(context.type), cmsg.create_time, int(is_triggered))
        # logger.debug("[Summary] {}:{} ({})" .format(username, context.content, session_id))

    def _translate_text_to_commands(self, text):
        llm = ModelFactory().create_llm_model(**build_model_params({
            "openai_api_key": conf().get("open_ai_api_key", ""),
            "proxy": conf().get("proxy", ""),
        }))

        prompt = PromptTemplate(
            input_variables=["input"],
            template=TRANSLATE_PROMPT,
        )
        bot = LLMChain(llm=llm, prompt=prompt)
        content = bot.run(text)
        return content

    def _get_openai_chat_url(self):
        return self.open_ai_api_base + "/chat/completions"
    
    def _get_openai_headers(self):
        return {
            'Authorization': f"Bearer {self.open_ai_api_key}",
            'Host': urlparse(self.open_ai_api_base).netloc
        }
    
    def _get_openai_payload(self, prompt):
        messages = [{"role": "user", "content": prompt}]
        payload = {
            'model': self.open_ai_model,
            'messages': messages
        }
        return payload

    def on_handle_context(self, e_context: EventContext):

        if e_context['context'].type != ContextType.TEXT:
            return
        
        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        clist = content.split()
        if clist[0].startswith(trigger_prefix):
            limit = 99
            duration = -1

            if "总结" in clist[0]:
                flag = False
                if clist[0] == trigger_prefix+"总结":
                    flag = True
                    if len(clist) > 1:
                        try:
                            limit = int(clist[1])
                            logger.debug("[Summary] limit: %d" % limit)
                        except Exception as e:
                            flag = False
                if not flag:
                    text = content.split(trigger_prefix,maxsplit=1)[1]
                    try:
                        command_json = find_json(self._translate_text_to_commands(text))
                        command = json.loads(command_json)
                        name = command["name"]
                        if name.lower() == "summary":
                            limit = int(command["args"].get("count", 99))
                            if limit < 0:
                                limit = 299
                            duration = int(command["args"].get("duration_in_seconds", -1))
                            logger.debug("[Summary] limit: %d, duration: %d seconds" % (limit, duration))
                    except Exception as e:
                        logger.error("[Summary] translate failed: %s" % e)
                        return
            else:
                return

            start_time = int(time.time())
            if duration > 0:
                start_time = start_time - duration
            else:
                start_time = 0

            msg:ChatMessage = e_context['context']['msg']
            session_id = msg.from_user_id
            if conf().get('channel_type', 'wx') == 'wx' and msg.from_user_nickname is not None:
                session_id = msg.from_user_nickname # itchat channel id会变动，只好用名字作为session id
            records = self._get_records(session_id, start_time, limit)
            for i in range(len(records)):
                record=list(records[i])
                content = record[3]
                clist = re.split(r'\n- - - - - - - - -.*?\n', content)
                if len(clist) > 1:
                    record[3] = clist[1]
                    records[i] = tuple(record)
            if len(records) <= 1:
                reply = Reply(ReplyType.INFO, "无聊天记录可供总结")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            summary_prompt = "请生成以下聊天记录的总结：\n"
            for record in records:
                summary_prompt += f"{record[2]}: {record[3]}\n"

            logger.debug(f"[Summary] summary_prompt: {summary_prompt}")
            openai_chat_url = self._get_openai_chat_url()
            headers = self._get_openai_headers()
            payload = self._get_openai_payload(summary_prompt)
            try:
                response = requests.post(openai_chat_url, headers=headers, json=payload, timeout=60)
                response.raise_for_status()
                result = response.json()['choices'][0]['message']['content']
                reply = Reply(ReplyType.TEXT, f"本次总结了{len(records)}条消息。\n\n{result}")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
            except Exception as e:
                logger.error(f"[Summary] 请求OpenAI接口失败: {str(e)}")
                reply = Reply(ReplyType.ERROR, "总结聊天记录失败，请稍后再试")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose = False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        help_text += f"使用方法:输入\"{trigger_prefix}总结 最近消息数量\"，我会帮助你总结聊天记录。\n例如：\"{trigger_prefix}总结 100\"，我会总结最近100条消息。\n\n你也可以直接输入\"{trigger_prefix}总结前99条信息\"或\"{trigger_prefix}总结3小时内的最近10条消息\"\n我会尽可能理解你的指令。"
        return help_text
