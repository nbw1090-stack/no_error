"""
Agent 核心 —— ReAct 循环

编排 LLM、工具和会话，实现 Reasoning + Acting 模式：
1. LLM 分析用户问题，决定调用哪些工具
2. 执行工具，将结果反馈给 LLM
3. LLM 基于工具结果生成最终回复
"""

import json
import logging

from agent.llm.base import BaseLLMAdapter
from agent.tools.registry import ToolRegistry
from agent.context import ConversationContext
from agent.dataset import LogDataset
from agent.session import SessionManager
from agent.prompts.system import build_system_prompt

logger = logging.getLogger(__name__)


class Agent:
    """
    ReAct Agent —— BMC 日志分析的核心引擎。

    工作流程：
    1. 加载会话，构建系统提示词 + 对话上下文
    2. 向 LLM 发送消息和可用工具列表
    3. 如果 LLM 返回 tool_calls：执行工具 → 将结果加入上下文 → 回到步骤 2
    4. 如果 LLM 返回纯文本：作为最终回复返回
    5. 最多循环 max_iterations 次，防止无限循环
    """

    def __init__(
        self,
        llm: BaseLLMAdapter,
        session_manager: SessionManager,
        max_iterations: int = 10,
    ):
        self.llm = llm
        self.session_manager = session_manager
        self.max_iterations = max_iterations

    async def run(
        self,
        session_id: str,
        user_message: str,
        dataset: LogDataset,
    ) -> str:
        """
        执行一次 Agent 对话轮次。

        Args:
            session_id: 会话 ID
            user_message: 用户消息
            dataset: 当前分析的日志数据集

        Returns:
            Agent 的最终文本回复

        Raises:
            ValueError: 会话不存在
        """
        # ---- 1. 加载会话 ----
        session = self.session_manager.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        # ---- 2. 构建系统提示词 ----
        system_prompt = build_system_prompt(dataset.summary)
        ctx = ConversationContext(system_prompt=system_prompt)

        # ---- 3. 将用户消息加入会话 ----
        self.session_manager.add_message(
            session_id, {"role": "user", "content": user_message}
        )

        # ---- 4. 获取工具 schema ----
        tool_schemas = ToolRegistry.get_schemas()
        tool_names = ToolRegistry.get_names()
        logger.info(
            "Agent starting: session=%s, tools=%s", session_id, tool_names
        )

        # ---- 5. ReAct 循环 ----
        final_reply = ""

        for iteration in range(self.max_iterations):
            # 重新加载会话以获取最新消息（包括前几轮的工具调用结果）
            session = self.session_manager.get(session_id)
            if not session:
                raise ValueError(f"Session lost during run: {session_id}")

            # 构建消息列表
            # 第 0 轮：system + history + user_message
            # 后续轮：system + 完整会话消息（已包含 user、assistant tool_calls、tool results）
            if iteration == 0:
                messages = ctx.build_messages(
                    history=[],  # user_message 已存入 session.messages
                    user_message=user_message,
                )
                # 实际上应使用 session 中的完整历史
                messages = ctx.build_messages(
                    history=session.messages[:-1],  # 排除刚加入的 user message
                    user_message=user_message,
                )
            else:
                # 后续轮次：system + 全部会话消息
                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(session.messages)

            logger.debug(
                "ReAct iteration %d: %d messages",
                iteration,
                len(messages),
            )

            # ---- 调用 LLM ----
            response = await self.llm.chat(messages, tool_schemas)

            # 保存助手消息到会话
            self.session_manager.add_message(session_id, response)

            # ---- 检查是否有工具调用 ----
            if response.get("tool_calls"):
                for tc in response["tool_calls"]:
                    func_name = tc["function"]["name"]
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        func_args = {}

                    logger.info(
                        "Tool call [%d]: %s(%s)",
                        iteration,
                        func_name,
                        json.dumps(func_args, ensure_ascii=False),
                    )

                    # 执行工具
                    result_str = await ToolRegistry.execute(
                        func_name, func_args, dataset
                    )

                    # 将工具结果加入会话
                    self.session_manager.add_message(
                        session_id,
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_str,
                        },
                    )

                # 继续循环，让 LLM 处理工具结果
                continue

            # ---- 无工具调用 → 最终回复 ----
            final_reply = response.get("content") or ""
            if final_reply.strip():
                break

            # content 为空但没有工具调用 → 让 LLM 继续
            logger.warning("Empty response with no tool calls, retrying...")

        else:
            # 达到最大迭代次数
            logger.warning(
                "Max iterations (%d) reached for session %s",
                self.max_iterations,
                session_id,
            )
            final_reply = (
                "分析过程较为复杂，已超出当前处理轮次限制。"
                "请尝试提出更具体的问题，以便我能更高效地帮助你。"
            )

        return final_reply
