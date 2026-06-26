"""
对话上下文管理

负责构建发送给 LLM 的消息列表，包括系统提示词注入和历史裁剪。
"""

import logging

logger = logging.getLogger(__name__)


class ConversationContext:
    """
    对话上下文构建器。

    职责：
    1. 注入系统提示词
    2. 裁剪历史消息到指定数量（防止超出 token 限制）
    3. 组装完整的消息列表供 LLM 消费
    """

    def __init__(self, system_prompt: str, max_history: int = 40):
        """
        Args:
            system_prompt: 系统提示词
            max_history: 最大保留的历史消息数（默认 40 条 ≈ 20 轮对话）
        """
        self.system_prompt = system_prompt
        self.max_history = max_history

    def build_messages(
        self,
        history: list[dict],
        user_message: str | None = None,
    ) -> list[dict]:
        """
        构建完整的消息列表。

        顺序：系统提示词 → 裁剪后的历史 → 当前用户消息（如有）

        Args:
            history: 会话中的历史消息列表
            user_message: 当前用户消息（首次调用时提供，后续 ReAct 循环中为 None）

        Returns:
            标准化的消息列表
        """
        messages = [{"role": "system", "content": self.system_prompt}]

        # 按最大条数裁剪历史（保留最近的）
        if len(history) > self.max_history:
            logger.debug(
                "Trimming history from %d to %d messages",
                len(history),
                self.max_history,
            )
            trimmed = history[-self.max_history :]
        else:
            trimmed = history

        messages.extend(trimmed)

        # 添加当前用户消息
        if user_message:
            messages.append({"role": "user", "content": user_message})

        return messages
