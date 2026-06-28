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
from agent.prompts.system import build_system_prompt, _format_indexed_components

logger = logging.getLogger(__name__)


def _accumulate_usage(totals: dict, usage: dict | None) -> None:
    """
    把单次 LLM 调用的 usage 累加进 totals。

    键：input / output / total，外加 DeepSeek 等供应商上报的前缀缓存命中/未命中
    （cache_hit / cache_miss，二者之和应等于 input）。缓存命中部分计费通常为
    未命中的 1/10~1/50，是衡量「ReAct 重发历史是否被缓存吃下」的关键指标。

    usage 为 None 或缺键时按 0 处理，保证适配器/测试桩未带 usage 时不报错。
    """
    if not usage:
        return
    totals["input"] += usage.get("input", 0) or 0
    totals["output"] += usage.get("output", 0) or 0
    totals["total"] += usage.get("total", 0) or 0
    totals["cache_hit"] += usage.get("cache_hit", 0) or 0
    totals["cache_miss"] += usage.get("cache_miss", 0) or 0


# 最后一轮的收尾指令：该轮禁用工具，逼模型用已有信息直接作答，
# 避免撞 max_iterations 后只回一句兜底话术、把已收集的素材全丢弃。
_FINAL_STEP_DIRECTIVE = (
    "（这是本轮分析的最后一步，已无法再调用任何工具。请基于以上已获取的"
    "全部信息，直接给出尽可能完整、有用的最终回答——包括已确认的结论、合理"
    "推断，以及仍存在的信息缺口；不要再请求更多数据。）"
)

# 同一 (工具, 参数) 重复返回无效信息的告警阈值：达到该次数即提示 LLM 换策略。
_REPEAT_UNPRODUCTIVE_LIMIT = 2


def _tool_signature(func_name: str, func_args: dict) -> str:
    """把一次工具调用规约成可比对的签名（工具名 + 规范化参数）。"""
    try:
        canonical = json.dumps(func_args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(func_args)
    return f"{func_name}|{canonical}"


def _is_unproductive_result(result_str: str) -> bool:
    """
    判断工具结果是否"未检索到有效信息"：携带 error，或检索类结果为空。

    仅用于在**重复**同一调用时给 LLM 提示——单次空结果是正常的（可能本身
    就是答案），不视为异常。
    """
    try:
        data = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("error"):
        return True
    # 常见检索类工具的"空结果"计数字段：存在且为 0/空即视为无有效信息
    empty_keys = (
        "matches_count",
        "count",
        "total_matches",
        "returned_groups",
        "files_count",
    )
    for k in empty_keys:
        if k in data and not data[k]:
            return True
    if "results" in data and not data["results"]:
        return True
    return False


def _repeat_guidance(func_name: str, count: int) -> str:
    """生成"同一调用重复无效"的提示语，附加到工具结果末尾交给 LLM。"""
    return (
        f"\n\n⚠️ 调用策略提示：你已用完全相同的参数调用 `{func_name}` {count} 次，"
        "且每次都未检索到有效信息。请勿再用相同参数重复调用本工具——"
        "改变策略：换用不同的参数或其它工具，或基于已掌握的信息直接作答。"
    )


def _note_tool_result(
    counters: dict, func_name: str, func_args: dict, result_str: str
) -> str:
    """
    记录一次工具调用结果；若同一 (工具,参数) 重复返回无效信息达到阈值，
    在结果末尾追加换策略提示，避免 LLM 对同一工具死循环调用。

    Returns:
        可能被追加了提示的结果字符串（未触发时原样返回）。
    """
    if not _is_unproductive_result(result_str):
        return result_str
    sig = _tool_signature(func_name, func_args)
    counters[sig] = counters.get(sig, 0) + 1
    if counters[sig] >= _REPEAT_UNPRODUCTIVE_LIMIT:
        return result_str + _repeat_guidance(func_name, counters[sig])
    return result_str


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


def _prompt_vars(summary: dict, source_components) -> dict:
    """
    把 dataset summary + 已索引组件构造成 system prompt 模板变量。
    key 与本地 DATA_CONTEXT_PROMPT / SOURCE_GUIDANCE_PROMPT 的 ${var} 同名，
    也与 Langfuse 托管 prompt 的 {{var}} 一一对应。
    """
    components = summary.get("components", [])
    time_range = summary.get("timeRange", {})
    return {
        "total_lines": summary.get("totalLines", 0),
        "error_count": summary.get("errorCount", 0),
        "warning_count": summary.get("warningCount", 0),
        "notice_count": summary.get("noticeCount", 0),
        "launch_count": summary.get("launchCount", 0),
        "component_count": len(components),
        "components": ", ".join(components[:15]),
        "time_start": time_range.get("start", "unknown"),
        "time_end": time_range.get("end", "unknown"),
        "indexed_components": _format_indexed_components(source_components),
    }


def _resolve_system_prompt(summary, source_components) -> str:
    """
    解析 system prompt：数据分析模式（summary 非空）优先用 Langfuse 托管版本，
    拉取失败 / 非数据分析模式回退到本地 build_system_prompt。

    任何异常都回退到本地、绝不抛——保证 prompt 拉取问题不会阻断 LLM 调用。
    非数据分析模式（无 summary 的 QA / source-QA）直接走本地，不经 Langfuse 托管。
    """
    if summary is None:
        return build_system_prompt(None, source_components)

    from config import AppConfig

    lf = AppConfig.from_env().langfuse
    prompt = get_client().get_prompt(lf.prompt_name, lf.prompt_label)
    if prompt is not None:
        try:
            return prompt.compile(**_prompt_vars(summary, source_components))
        except Exception as e:
            logger.warning(
                "Langfuse prompt compile failed, fallback to local: %s", e
            )
    return build_system_prompt(summary, source_components)


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
        context_token_budget: int = 24000,
        recent_tools_keep: int = 3,
    ):
        self.llm = llm
        self.session_manager = session_manager
        self.max_iterations = max_iterations
        self.max_history = max_history
        self.context_token_budget = context_token_budget
        self.recent_tools_keep = recent_tools_keep

    def _make_context(self, system_prompt: str) -> ConversationContext:
        """构造对话上下文（统一注入 token 预算 / 工具结果省略策略）。"""
        return ConversationContext(
            system_prompt=system_prompt,
            max_history=self.max_history,
            token_budget=self.context_token_budget,
            recent_tools_keep=self.recent_tools_keep,
        )

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
        system_prompt = _resolve_system_prompt(
            dataset.summary if dataset is not None else None, source_components
        )
        ctx = self._make_context(system_prompt)

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
        # 整轮 token 累计（含前缀缓存命中/未命中，便于估算真实计费）
        totals = {
            "input": 0,
            "output": 0,
            "total": 0,
            "cache_hit": 0,
            "cache_miss": 0,
        }
        # 同一 (工具,参数) 无效调用计数，用于检测对工具的死循环调用
        unproductive_calls: dict[str, int] = {}

        for iteration in range(self.max_iterations):
            session = self.session_manager.get(session_id)
            if not session:
                raise ValueError(f"Session lost during run: {session_id}")

            # 最后一轮：禁用工具 + 注入收尾指令，强制 LLM 用已有信息直接作答
            is_last = iteration == self.max_iterations - 1

            # 统一通过上下文构建器组装消息：system + 历史，并在超 token 预算时
            # 省略陈旧工具结果（压制 ReAct 重发历史的二次方膨胀）。iteration 0 时
            # session.messages 末尾即刚加入的用户消息，与旧的 [:-1]+user_message
            # 等价，故两轮统一处理——关键是迭代 ≥1 也走省略，而非重发全量。
            messages = ctx.build_messages(history=session.messages)
            if is_last and exposed_groups:
                messages.append(
                    {"role": "system", "content": _FINAL_STEP_DIRECTIVE}
                )

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
                # 最后一轮不暴露工具，逼模型产出文本而非又一轮 tool_calls。
                offered_tools = [] if is_last else tool_schemas
                response = await self.llm.chat(messages, offered_tools)

                # 累计本轮 token 消耗（适配器未带 usage 时按 0）
                _accumulate_usage(totals, response.get("usage"))
                # usage 是调用级元数据，不进会话历史（避免回放给 LLM 时混入多余字段）
                response.pop("usage", None)

                # 保存助手消息到会话
                self.session_manager.add_message(session_id, response)

                # ---- 检查是否有工具调用（无数据集 / 最后一轮不执行工具）----
                if response.get("tool_calls") and exposed_groups and not is_last:
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

                        # 同一调用反复无效时，给结果追加换策略提示
                        result_str = _note_tool_result(
                            unproductive_calls, func_name, func_args, result_str
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
            "Agent done: session=%s input_tokens=%d output_tokens=%d "
            "total_tokens=%d cache_hit=%d cache_miss=%d",
            session_id,
            totals["input"],
            totals["output"],
            totals["total"],
            totals["cache_hit"],
            totals["cache_miss"],
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
        system_prompt = _resolve_system_prompt(
            dataset.summary if dataset is not None else None, source_components
        )
        ctx = self._make_context(system_prompt)

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
        # 整轮 token 累计（含前缀缓存命中/未命中，便于估算真实计费）
        totals = {
            "input": 0,
            "output": 0,
            "total": 0,
            "cache_hit": 0,
            "cache_miss": 0,
        }
        # 同一 (工具,参数) 无效调用计数，用于检测对工具的死循环调用
        unproductive_calls: dict[str, int] = {}

        for iteration in range(self.max_iterations):
            session = self.session_manager.get(session_id)
            if not session:
                raise ValueError(f"Session lost during run: {session_id}")

            # 最后一轮：禁用工具 + 注入收尾指令，强制 LLM 用已有信息直接作答
            is_last = iteration == self.max_iterations - 1

            # 统一通过上下文构建器组装消息：system + 历史，并在超 token 预算时
            # 省略陈旧工具结果（压制 ReAct 重发历史的二次方膨胀）。iteration 0 时
            # session.messages 末尾即刚加入的用户消息，与旧的 [:-1]+user_message
            # 等价，故两轮统一处理——关键是迭代 ≥1 也走省略，而非重发全量。
            messages = ctx.build_messages(history=session.messages)
            if is_last and exposed_groups:
                messages.append(
                    {"role": "system", "content": _FINAL_STEP_DIRECTIVE}
                )

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

                # 最后一轮不暴露工具，逼模型产出文本而非又一轮 tool_calls。
                offered_tools = [] if is_last else tool_schemas
                async for evt in self.llm.chat_stream(messages, offered_tools):
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
                # 无数据集 / 最后一轮不执行工具。
                if tool_calls and exposed_groups and not is_last:
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

                        # 同一调用反复无效时，给结果追加换策略提示
                        result_str = _note_tool_result(
                            unproductive_calls, func_name, func_args, result_str
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
            "Agent done [stream]: session=%s input_tokens=%d output_tokens=%d "
            "total_tokens=%d cache_hit=%d cache_miss=%d",
            session_id,
            totals["input"],
            totals["output"],
            totals["total"],
            totals["cache_hit"],
            totals["cache_miss"],
        )
        yield {
            "type": "usage",
            "input": totals["input"],
            "output": totals["output"],
            "total": totals["total"],
            "cache_hit": totals["cache_hit"],
            "cache_miss": totals["cache_miss"],
        }

        # ---- 流结束 ----
        yield {"type": "done"}
