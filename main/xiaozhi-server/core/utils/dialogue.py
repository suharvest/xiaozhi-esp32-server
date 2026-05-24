import uuid
import re
from typing import List, Dict
from datetime import datetime


class Message:
    def __init__(
            self,
            role: str,
            content: str = None,
            uniq_id: str = None,
            tool_calls=None,
            tool_call_id=None,
            is_temporary=False,
    ):
        self.uniq_id = uniq_id if uniq_id is not None else str(uuid.uuid4())
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.is_temporary = is_temporary  # 标记临时消息（如工具调用提醒）


class Dialogue:
    def __init__(self):
        self.dialogue: List[Message] = []
        # 获取当前时间
        self.current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def put(self, message: Message):
        self.dialogue.append(message)

    def getMessages(self, m, dialogue):
        if m.tool_calls is not None:
            dialogue.append({"role": m.role, "tool_calls": m.tool_calls})
        elif m.role == "tool":
            dialogue.append(
                {
                    "role": m.role,
                    "tool_call_id": (
                        str(uuid.uuid4()) if m.tool_call_id is None else m.tool_call_id
                    ),
                    "content": m.content,
                }
            )
        else:
            dialogue.append({"role": m.role, "content": m.content})

    def get_llm_dialogue(self) -> List[Dict[str, str]]:
        # 直接调用get_llm_dialogue_with_memory，传入None作为memory_str
        # 这样确保说话人功能在所有调用路径下都生效
        return self.get_llm_dialogue_with_memory(None, None)

    def update_system_message(self, new_content: str):
        """更新或添加系统消息"""
        # 查找第一个系统消息
        system_msg = next((msg for msg in self.dialogue if msg.role == "system"), None)
        if system_msg:
            system_msg.content = new_content
        else:
            self.put(Message(role="system", content=new_content))

    def _ensure_tool_calls_complete(self, messages: List[Message]) -> List[Message]:
        """
        对称兜底 tool_calls / tool 响应配对，防止 OpenAI 兼容 API 报 400：
        - 悬空 assistant.tool_calls（无 tool 响应）→ 补一条 "interrupted" tool 响应
        - 孤儿 tool 响应（找不到任何 assistant.tool_calls 持有该 id）→ 丢弃
          常见于历史滑窗把上文 assistant 截掉、或历史本身已损坏
        """
        # 第一遍：收集所有 assistant 暴露过的 tool_call id（无序，仅判存在性）
        known_ids = set()
        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        known_ids.add(tc_id)

        # 第二遍：构造结果，过滤孤儿 tool 响应，跟踪悬空 tool_calls
        pending_tool_calls = set()
        result = []
        for msg in messages:
            if msg.role == "tool" and msg.tool_call_id and msg.tool_call_id not in known_ids:
                continue  # 孤儿 tool 响应，跳过
            result.append(msg)

            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        pending_tool_calls.add(tc_id)

            elif msg.role == "tool" and msg.tool_call_id:
                pending_tool_calls.discard(msg.tool_call_id)

        for missing_id in pending_tool_calls:
            dummy_tool_msg = Message(
                role="tool",
                content='{"status": "interrupted", "message": "动作已取消/被打断"}',
                tool_call_id=missing_id
            )
            result.append(dummy_tool_msg)

        return result

    def _apply_history_window(
            self, messages: List["Message"], max_turns: int
    ) -> List["Message"]:
        """Keep only the last ``max_turns`` user turns plus everything that
        follows each (assistant replies, tool_calls, tool responses).

        Slicing at a user-message boundary guarantees we never strand a tool
        response whose preceding assistant tool_calls got trimmed — which would
        otherwise be rejected by OpenAI-compatible APIs.

        Assumption: callers exclude few-shot/template messages (is_temporary=True)
        before passing here. If a future code path passes user-role few-shots
        through, they will inflate the turn count and silently shrink the real
        history window. Defensive cleanup of any orphan tool responses still
        happens downstream in _ensure_tool_calls_complete.
        """
        if max_turns is None or max_turns <= 0:
            return messages
        user_indices = [i for i, m in enumerate(messages) if m.role == "user"]
        if len(user_indices) <= max_turns:
            return messages
        start = user_indices[-max_turns]
        return messages[start:]

    def get_llm_dialogue_with_memory(
            self,
            memory_str: str = None,
            voiceprint_config: dict = None,
            max_history_turns: int = None,
    ) -> List[Dict[str, str]]:
        # 构建对话
        dialogue = []

        # 添加系统提示和记忆
        system_message = next(
            (msg for msg in self.dialogue if msg.role == "system"), None
        )

        if system_message:
            # 以 <context> 为分界点，拆分静态 system prompt 和动态上下文
            # 静态部分（规则、身份等）保持不变，可命中前缀缓存
            # 动态部分（时间、天气、记忆等）作为第二条 system 消息，保持 system 权威性
            full_prompt = system_message.content
            context_match = re.search(r"<context>", full_prompt)
            if context_match:
                static_part = full_prompt[:context_match.start()]
                dynamic_part = full_prompt[context_match.start():]
            else:
                static_part = full_prompt
                dynamic_part = ""

            # 第一段：静态 system prompt（前缀缓存可命中）
            dialogue.append({"role": "system", "content": static_part})

        # 第二段：few-shot 示例（会话内不变，也是缓存前缀的一部分）
        non_system_messages = [m for m in self.dialogue if m.role != "system"]
        fewshot_messages = [m for m in non_system_messages if m.is_temporary]
        complete_fewshot = self._ensure_tool_calls_complete(fewshot_messages)
        for m in complete_fewshot:
            self.getMessages(m, dialogue)

        # 第三段：动态上下文 system prompt（时间、记忆、说话人等）
        # 保持 system 角色以确保模型权威性，不降级为 user
        if system_message and dynamic_part:
            # 替换时间占位符
            dynamic_part = dynamic_part.replace(
                "{{current_time}}", datetime.now().strftime("%H:%M")
            )

            # 填充记忆
            if memory_str is not None:
                dynamic_part = re.sub(
                    r"<memory>.*?</memory>",
                    f"<memory>\n{memory_str}\n</memory>",
                    dynamic_part,
                    flags=re.DOTALL,
                )

            # 追加说话人信息
            try:
                speakers = voiceprint_config.get("speakers", [])
                if speakers:
                    dynamic_part += "\n<speakers_info>"
                    for speaker_str in speakers:
                        try:
                            parts = speaker_str.split(",", 2)
                            if len(parts) >= 2:
                                name = parts[1].strip()
                                description = (
                                    parts[2].strip() if len(parts) >= 3 else ""
                                )
                                dynamic_part += f"\n- {name}：{description}"
                        except:
                            pass
                    dynamic_part += "\n</speakers_info>"
            except:
                pass

            dialogue.append({"role": "system", "content": dynamic_part})

        # 第四段：实际对话历史（不含 few-shot），先按用户轮数滑窗
        actual_messages = [m for m in non_system_messages if not m.is_temporary]
        actual_messages = self._apply_history_window(actual_messages, max_history_turns)
        complete_actual = self._ensure_tool_calls_complete(actual_messages)
        for m in complete_actual:
            self.getMessages(m, dialogue)

        return dialogue
