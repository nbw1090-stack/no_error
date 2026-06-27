"""
Agent 核心 —— ReAct 循环

编排 LLM、工具和会话，实现 Reasoning + Acting 模式：
1. LLM 分析用户问题，决定调用哪些工具
2. 执行工具，将结果反馈给 LLM
3. LLM 基于工具结果生成最终回复

Langfuse v4 可观测性：通过 OTEL 上下文自动嵌套，
无需手动传递 trace/span 对象。
"""

import asyncio
import json
import logging
from typing import AsyncGenerator, Optional

from observability import get_client
from agent.llm.base import BaseLLMAdapter
from agent.tools.registry import ToolRegistry
from agent.context import ConversationContext
from agent.dataset import LogDataset
from agent.session import SessionManager
from agent.prompts.system import build_system_prompt

logger = logging.getLogger(__name__)


def _accumulate_usage(totals: dict, usage: dict | None) -> None:
    """
    把单次 LLM 调用的 usage 累加进 totals（键：input / output / total）。

    usage 为 None 或缺键时按 0 处理，保证适配器/测试桩未带 usage 时不报错。
    """
    if not usage:
        return
    totals["input"] += usage.get("input", 0) or 0
    totals["output"] += usage.get("output", 0) or 0
    totals["total"] += usage.get("total", 0) or 0


async def _load_source_components(user_id: int | None) -> list[dict]:
    """
    查询用户已构建源码索引的组件列表（含文件/符号规模，用于注入 system prompt）。

    返回 [{name, file_count, symbol_count}]：让 LLM 明确这些是真实的、有规模
    的可查代码库，从而更愿意主动调用源码工具。无登录（user_id 为 None）时返回
    空列表；同步的 sqlite 查询经 asyncio.to_thread 调度，避免阻塞事件循环。
    """
    if user_id is None:
        return []
    from ast_analysis import db as ast_db

    comps = await asyncio.to_thread(ast_db.list_user_components, user_id)
    return [
        {
            "name": c["component"],
            "file_count": c["file_count"],
            "symbol_count": c["symbol_count"],
        }
        for c in comps
    ]


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
        max_history: int = 40,
    ):
        self.llm = llm
        self.session_manager = session_manager
        self.max_iterations = max_iterations
        self.max_history = max_history

    async def run(
        self,
        session_id: str,
        user_message: str,
        dataset: Optional[LogDataset] = None,
        *,
        usage: Optional[dict] = None,
    ) -> str:
        """
        执行一次 Agent 对话轮次。

        Langfuse 追踪自动嵌套：调用方用 tracer.observation("chat")
        包裹此方法，内部的 ReAct 迭代和 LLM 调用会自动成为其子节点。

        Args:
            session_id: 会话 ID
            user_message: 用户消息
            dataset: 当前分析的日志数据集；为 None 表示尚未上传日志的
                纯对话会话（通用 BMC 问答，不暴露任何工具）。
            usage: 可选的累加器 dict；传入时，方法会把整轮 ReAct 的
                input/output/total token 总量写回 {input, output, total}。
                每个请求应新建独立 dict（agent 是单例，勿暂存实例属性）。

        Returns:
            Agent 的最终文本回复
        """
        tracer = get_client()

        # ---- 1. 加载会话 ----
        session = self.session_manager.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        # ---- 2. 构建系统提示词 ----
        # 源码索引以 session.user_id 为准（无日志会话也能基于源码问答）；
        # 数据上下文仅在有日志时注入。
        uid = getattr(session, "user_id", 0)
        source_components = await _load_source_components(uid)
        system_prompt = build_system_prompt(
            dataset.summary if dataset is not None else None, source_components
        )
        ctx = ConversationContext(
            system_prompt=system_prompt, max_history=self.max_history
        )

        # ---- 3. 将用户消息加入会话 ----
        self.session_manager.add_message(
            session_id, {"role": "user", "content": user_message}
        )

        # ---- 4. 获取工具 schema ----
        # 日志工具需 dataset；源码工具仅需已索引组件（无日志也能用）。
        exposed_groups: set[str] = set()
        if dataset is not None:
            exposed_groups.add("log")
        if source_components:
            exposed_groups.add("source")
        tool_schemas = (
            ToolRegistry.get_schemas(groups=exposed_groups) if exposed_groups else []
        )
        tool_names = (
            ToolRegistry.get_names(groups=exposed_groups) if exposed_groups else []
        )
        logger.info(
            "Agent starting: session=%s, tools=%s", session_id, tool_names
        )
        # 无日志会话下，源码工具需要一个带 user_id 的 dataset 才能执行
        effective_dataset = (
            dataset
            if dataset is not None
            else LogDataset(entries=[], summary={}, user_id=(uid or None))
        )

        # ---- 5. ReAct 循环 ----
        final_reply = ""
        totals = {"input": 0, "output": 0, "total": 0}  # 整轮 token 累计

        for iteration in range(self.max_iterations):
            session = self.session_manager.get(session_id)
            if not session:
                raise ValueError(f"Session lost during run: {session_id}")

            if iteration == 0:
                messages = ctx.build_messages(
                    history=session.messages[:-1],
                    user_message=user_message,
                )
            else:
                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(session.messages)

            logger.debug(
                "ReAct iteration %d: %d messages",
                iteration,
                len(messages),
            )

            # ---- Langfuse: 迭代 span（自动嵌套到外层 chat trace 下） ----
            with tracer.observation(
                name=f"react-iteration-{iteration}",
                input={
                    "iteration": iteration,
                    "messages_count": len(messages),
                },
            ) as iter_span:

                # ---- 调用 LLM（generation 自动嵌套到 iter_span 下） ----
                response = await self.llm.chat(messages, tool_schemas)

                # 累计本轮 token 消耗（适配器未带 usage 时按 0）
                _accumulate_usage(totals, response.get("usage"))
                # usage 是调用级元数据，不进会话历史（避免回放给 LLM 时混入多余字段）
                response.pop("usage", None)

                # 保存助手消息到会话
                self.session_manager.add_message(session_id, response)

                # ---- 检查是否有工具调用（无数据集时不执行工具）----
                if response.get("tool_calls") and exposed_groups:
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

                        # ---- Langfuse: 工具 span（自动嵌套到 iter_span 下） ----
                        with tracer.observation(
                            name=f"tool-{func_name}",
                            input={
                                "tool_name": func_name,
                                "arguments": func_args,
                            },
                        ) as tool_span:
                            result_str = await ToolRegistry.execute(
                                func_name, func_args, effective_dataset
                            )
                            tool_span.update(
                                output={"result": result_str[:2000]},
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

                    iter_span.update(
                        output={
                            "tool_calls_count": len(response["tool_calls"])
                        }
                    )
                    continue

                # ---- 无工具调用 → 最终回复 ----
                final_reply = response.get("content") or ""

                iter_span.update(
                    output={
                        "final_reply": final_reply[:500],
                        "is_final": True,
                    }
                )

                if final_reply.strip():
                    break

                logger.warning(
                    "Empty response with no tool calls, retrying..."
                )

        else:
            logger.warning(
                "Max iterations (%d) reached for session %s",
                self.max_iterations,
                session_id,
            )
            final_reply = (
                "分析过程较为复杂，已超出当前处理轮次限制。"
                "请尝试提出更具体的问题，以便我能更高效地帮助你。"
            )

        # 整轮 token 消耗写回累加器 + 服务端日志（用于 Langfuse trace metadata）
        if usage is not None:
            usage.update(totals)
        logger.info(
            "Agent done: session=%s input_tokens=%d output_tokens=%d total_tokens=%d",
            session_id,
            totals["input"],
            totals["output"],
            totals["total"],
        )

        return final_reply

    async def run_stream(
        self,
        session_id: str,
        user_message: str,
        dataset: Optional[LogDataset] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        执行一次 Agent 对话轮次（流式输出版本）。

        与 run() 使用相同的 ReAct 循环逻辑，但通过 async generator
        实时 yield SSE 事件，让前端可以逐词渲染回复文本。

        Yields:
            {"type": "status", "text": "..."}           - 状态消息
            {"type": "tool_progress", "tool": "...", "status": "start"|"done"}
            {"type": "delta", "text": "..."}             - 回复文本片段
            {"type": "done"}                             - 流结束
        """
        tracer = get_client()

        # ---- 1. 加载会话 ----
        session = self.session_manager.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        # ---- 2. 构建系统提示词 ----
        # 源码索引以 session.user_id 为准（无日志会话也能基于源码问答）；
        # 数据上下文仅在有日志时注入。
        uid = getattr(session, "user_id", 0)
        source_components = await _load_source_components(uid)
        system_prompt = build_system_prompt(
            dataset.summary if dataset is not None else None, source_components
        )
        ctx = ConversationContext(
            system_prompt=system_prompt, max_history=self.max_history
        )

        # ---- 3. 将用户消息加入会话 ----
        self.session_manager.add_message(
            session_id, {"role": "user", "content": user_message}
        )

        # ---- 4. 获取工具 schema ----
        # 日志工具需 dataset；源码工具仅需已索引组件（无日志也能用）。
        exposed_groups: set[str] = set()
        if dataset is not None:
            exposed_groups.add("log")
        if source_components:
            exposed_groups.add("source")
        tool_schemas = (
            ToolRegistry.get_schemas(groups=exposed_groups) if exposed_groups else []
        )
        tool_names = (
            ToolRegistry.get_names(groups=exposed_groups) if exposed_groups else []
        )
        logger.info(
            "Agent streaming: session=%s, tools=%s", session_id, tool_names
        )
        # 无日志会话下，源码工具需要一个带 user_id 的 dataset 才能执行
        effective_dataset = (
            dataset
            if dataset is not None
            else LogDataset(entries=[], summary={}, user_id=(uid or None))
        )

        # ---- 5. ReAct 循环 ----
        final_reply = ""
        totals = {"input": 0, "output": 0, "total": 0}  # 整轮 token 累计

        for iteration in range(self.max_iterations):
            session = self.session_manager.get(session_id)
            if not session:
                raise ValueError(f"Session lost during run: {session_id}")

            if iteration == 0:
                messages = ctx.build_messages(
                    history=session.messages[:-1],
                    user_message=user_message,
                )
            else:
                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(session.messages)

            logger.debug(
                "ReAct iteration %d [stream]: %d messages",
                iteration,
                len(messages),
            )

            with tracer.observation(
                name=f"react-iteration-{iteration}",
                input={
                    "iteration": iteration,
                    "messages_count": len(messages),
                },
            ) as iter_span:

                # ---- 流式调用 LLM，实时转发文本增量（真流式） ----
                full_content = ""
                tool_calls = None
                finish_reason = "stop"

                async for evt in self.llm.chat_stream(messages, tool_schemas):
                    if evt["type"] == "content_delta":
                        # 文本增量立即转发给前端
                        yield {"type": "delta", "text": evt["text"]}
                    elif evt["type"] == "done":
                        full_content = evt.get("content") or ""
                        tool_calls = evt.get("tool_calls")
                        finish_reason = evt.get("finish_reason", "stop")
                        # 累计本轮 token 消耗（适配器未带 usage 时按 0）
                        _accumulate_usage(totals, evt.get("usage"))

                iter_span.update(
                    output={
                        "finish_reason": finish_reason,
                        "tool_calls_count": len(tool_calls) if tool_calls else 0,
                        "content_preview": full_content[:500],
                    }
                )

                # ---- 工具调用轮：保存助手消息（含 tool_calls，只存一次）+ 执行 ----
                # 与 run() 保持一致：按 tool_calls 是否存在判断，而非依赖
                # finish_reason（某些 OpenAI 兼容服务在有 tool_calls 时
                # 仍返回非 "tool_calls" 的 finish_reason，依赖它会导致工具永不执行）。
                # 无数据集时不执行工具（此时未向 LLM 暴露任何工具）。
                if tool_calls and exposed_groups:
                    self.session_manager.add_message(
                        session_id,
                        {
                            "role": "assistant",
                            "content": full_content or None,
                            "tool_calls": tool_calls,
                        },
                    )

                    for tc in tool_calls:
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

                        # ---- 通知前端工具调用开始 ----
                        yield {
                            "type": "tool_progress",
                            "tool": func_name,
                            "status": "start",
                        }

                        with tracer.observation(
                            name=f"tool-{func_name}",
                            input={
                                "tool_name": func_name,
                                "arguments": func_args,
                            },
                        ) as tool_span:
                            result_str = await ToolRegistry.execute(
                                func_name, func_args, effective_dataset
                            )
                            tool_span.update(
                                output={"result": result_str[:2000]},
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

                        # ---- 通知前端工具调用完成 ----
                        yield {
                            "type": "tool_progress",
                            "tool": func_name,
                            "status": "done",
                        }

                    continue

                # ---- 最终回复轮：文本已流式转发，这里收尾并存会话（一次）----
                final_reply = full_content
                if final_reply.strip():
                    self.session_manager.add_message(
                        session_id,
                        {"role": "assistant", "content": final_reply},
                    )
                    break

                logger.warning(
                    "Empty response with no tool calls, retrying..."
                )

        else:
            logger.warning(
                "Max iterations (%d) reached for session %s",
                self.max_iterations,
                session_id,
            )
            final_reply = (
                "分析过程较为复杂，已超出当前处理轮次限制。"
                "请尝试提出更具体的问题，以便我能更高效地帮助你。"
            )
            # 逐词输出超限消息
            words = final_reply.split(" ")
            for i, word in enumerate(words):
                separator = " " if i < len(words) - 1 else ""
                yield {"type": "delta", "text": word + separator}
                await asyncio.sleep(0.01)

        # ---- 整轮 token 消耗：先回传 usage 事件，再结束流 ----
        logger.info(
            "Agent done [stream]: session=%s input_tokens=%d output_tokens=%d total_tokens=%d",
            session_id,
            totals["input"],
            totals["output"],
            totals["total"],
        )
        yield {
            "type": "usage",
            "input": totals["input"],
            "output": totals["output"],
            "total": totals["total"],
        }

        # ---- 流结束 ----
        yield {"type": "done"}
