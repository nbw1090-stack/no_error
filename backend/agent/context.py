"""
对话上下文管理

负责构建发送给 LLM 的消息列表，包括系统提示词注入、历史裁剪与
「陈旧工具结果省略」。

省略策略（核心，用于压制 ReAct 二次方膨胀）：
- ReAct 每轮都把全量历史重发给 LLM，源码/日志工具的单条结果可达 10~18KB，
  会被后续每一次调用反复重发。最近的工具结果是当前推理链的证据，必须完整；
  但**更早**的工具结果，模型早已读过并转写进 assistant 总结，再原样重发纯属浪费。
- 因此当历史 token 估算超过预算时，把「最近 N 条之外」的工具结果 content 替换为
  短占位符（保留 role/tool_call_id，结构合法、可追溯——原文仍在 session 存档里）。
- 仅在**超预算时**触发，平时保持前缀字节稳定，最大化 DeepSeek 自动前缀缓存命中。
"""

import logging

logger = logging.getLogger(__name__)

# 被省略的旧工具结果在发给 LLM 时的占位文本（原文仍存于 session 文件，可追溯）。
ELIDED_TOOL_PLACEHOLDER = (
    "[旧工具结果已省略以节省上下文——其要点已并入后续分析；"
    "如确需原始数据，请用更精确的参数重新调用对应工具]"
)


def estimate_tokens(obj) -> int:
    """
    粗略估算一段内容的 token 数。

    混合中英文 + JSON 经验值约 3 字符/token；只用于相对比较与预算控制，
    不要求与计费 token 精确一致（精确值以 API usage 为准）。
    """
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj) // 3
    # dict / list：按其 JSON 文本长度估算
    import json

    try:
        return len(json.dumps(obj, ensure_ascii=False)) // 3
    except (TypeError, ValueError):
        return len(str(obj)) // 3


def _message_tokens(msg: dict) -> int:
    """估算单条消息（含 content 与 tool_calls）的 token 数。"""
    total = estimate_tokens(msg.get("content"))
    if msg.get("tool_calls"):
        total += estimate_tokens(msg["tool_calls"])
    return total


class ConversationContext:
    """
    对话上下文构建器。

    职责：
    1. 注入系统提示词
    2. 按条数裁剪历史（防御性上限）
    3. 超 token 预算时省略陈旧工具结果
    4. 组装完整的消息列表供 LLM 消费
    """

    def __init__(
        self,
        system_prompt: str,
        max_history: int = 40,
        token_budget: int = 24000,
        recent_tools_keep: int = 3,
    ):
        """
        Args:
            system_prompt: 系统提示词
            max_history: 最大保留的历史消息数（防御性上限，默认 40）
            token_budget: 历史 token 软预算；超过才省略陈旧工具结果。
                <=0 表示禁用按 token 省略（仅保留条数上限）。
            recent_tools_keep: 省略时保留多少条最近的工具结果不动。
        """
        self.system_prompt = system_prompt
        self.max_history = max_history
        self.token_budget = token_budget
        self.recent_tools_keep = max(0, recent_tools_keep)

    def build_messages(
        self,
        history: list[dict],
        user_message: str | None = None,
    ) -> list[dict]:
        """
        构建完整的消息列表。

        顺序：系统提示词 → 裁剪并（必要时）省略后的历史 → 当前用户消息（如有）

        Args:
            history: 会话中的历史消息列表
            user_message: 当前用户消息（首次调用时提供，后续 ReAct 循环中为 None）

        Returns:
            标准化的消息列表（system 在首位，工具/助手配对结构保持合法）
        """
        # 1) 按最大条数裁剪历史（保留最近的），作为防御性上限
        if len(history) > self.max_history:
            logger.debug(
                "Trimming history from %d to %d messages",
                len(history),
                self.max_history,
            )
            trimmed = history[-self.max_history :]
        else:
            trimmed = history

        # 2) 超 token 预算时，省略陈旧工具结果（保留最近 N 条工具结果完整）
        trimmed = self._elide_stale_tool_results(trimmed)

        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(trimmed)

        # 3) 添加当前用户消息（仅首轮）
        if user_message:
            messages.append({"role": "user", "content": user_message})

        return messages

    def _elide_stale_tool_results(self, history: list[dict]) -> list[dict]:
        """
        当历史 token 估算超过预算时，把「最近 recent_tools_keep 条之外」的工具
        结果 content 替换为短占位符。

        - 仅作用于 role == "tool" 的消息；保留 tool_call_id（结构合法）。
        - 从最旧的工具结果开始省略，直到降到预算内或只剩最近 N 条。
        - 不删除任何消息，因此 assistant(tool_calls) ↔ tool 的配对始终成立。
        - 预算 <=0 或未超预算时原样返回（保持前缀稳定，吃满前缀缓存）。
        """
        if self.token_budget <= 0:
            return history

        total = sum(_message_tokens(m) for m in history)
        if total <= self.token_budget:
            return history

        # 找出所有工具结果消息的下标（按出现顺序）
        tool_idxs = [
            i for i, m in enumerate(history) if m.get("role") == "tool"
        ]
        # 最近 N 条工具结果保留完整，只省略更早的（最旧优先）
        elidable = tool_idxs[: len(tool_idxs) - self.recent_tools_keep]
        if not elidable:
            return history

        placeholder_tokens = estimate_tokens(ELIDED_TOOL_PLACEHOLDER)
        result = list(history)
        elided_count = 0
        for i in elidable:
            if total <= self.token_budget:
                break
            original = result[i].get("content")
            saved = estimate_tokens(original) - placeholder_tokens
            if saved <= 0:
                continue  # 本就很短，省不出 token，跳过
            new_msg = dict(result[i])
            new_msg["content"] = ELIDED_TOOL_PLACEHOLDER
            result[i] = new_msg
            total -= saved
            elided_count += 1

        if elided_count:
            logger.debug(
                "Elided %d stale tool results; est tokens now ~%d (budget %d)",
                elided_count,
                total,
                self.token_budget,
            )
        return result
