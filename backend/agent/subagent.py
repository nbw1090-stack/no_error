"""
检索子 Agent —— 代码 + wiki 统一取证入口（AGENT_SOURCE_MODE=subagent）。

架构定位（docs/proposals/agent-arch-v3.html，方案 A「子 Agent 即工具」）：
- 主 Agent 负责用日志工具定位问题、综合下结论；"读得多"的源码/wiki 检索
  隔离到本子 Agent，避免撑爆主 Agent 上下文。
- 子 Agent **无状态**：每次 retrieve_evidence 调用新起一轮，查完即散，
  中间过程（完整函数体、整页 wiki）不带回，只带回 ≤summary_max_chars 的
  摘要 + 出处清单。
- 内部是一个封顶 max_iterations 轮的小 ReAct 循环，复用 ToolRegistry 里的
  source / wiki 工具组与 core.LoopGuard 熔断器；标准查法写在提示词里
  （agent/prompts/retrieval.py），不写死在代码里——评测不理想时可原地换
  实现，主 Agent 无感。

取证契约（主 Agent ↔ 取证实现之间的固定接口）：
    输入：query（一句话说清"查什么"，自带全部线索）
    输出：{summary, citations, stats{rounds, tool_calls, tools_used,
          empty_handed}, usage}
"""

import asyncio
import json
import logging

from agent.dataset import LogDataset
from agent.llm.base import BaseLLMAdapter
from agent.tools.registry import ToolRegistry
from agent.core import (
    LoopGuard,
    _accumulate_usage,
    _is_unproductive_result,
    _load_source_components,
    _wiki_available,
)
from agent.prompts.retrieval import SUBAGENT_SYSTEM_PROMPT, render_sources_desc
from agent.prompts.system import _format_indexed_components

logger = logging.getLogger(__name__)

# 最后一轮的收尾指令：禁工具，逼子 Agent 把已有素材写成摘要交差。
_SUBAGENT_FINAL_DIRECTIVE = (
    "（检索轮次已用完，不能再调用工具。请立即基于以上已获取的信息输出最终摘要："
    "紧凑结论 + 「出处：」一行列出全部 file:line / wiki 页；没查到就如实说明。）"
)


class RetrievalSubAgent:
    """
    无状态检索子 Agent：一次 run() = 一次完整取证。

    与主 Agent（core.Agent）的区别：不读写 session、不流式、消息列表只活在
    本次调用内；轮次预算独立（默认 6，远小于主 Agent 的 10）。
    """

    def __init__(
        self,
        llm: BaseLLMAdapter,
        max_iterations: int = 6,
        summary_max_chars: int = 2000,
    ):
        self.llm = llm
        self.max_iterations = max(1, max_iterations)
        self.summary_max_chars = max(200, summary_max_chars)

    async def run(self, query: str, dataset: LogDataset) -> dict:
        """
        执行一次取证：小 ReAct 循环（source/wiki 工具）→ 摘要 + 出处。

        永不抛异常给调用方（工具执行器会把异常包成 error JSON，但这里再兜一层
        语义化返回），保证主 Agent 拿到的始终是结构化结果。
        """
        # ---- 1. 判定可用证据源（与 core 的暴露逻辑同源） ----
        source_components = await _load_source_components(dataset.user_id)
        wiki_ok = await _wiki_available()

        groups: set[str] = set()
        if source_components:
            groups.add("source")
        if wiki_ok:
            groups.add("wiki")
        if not groups:
            return {
                "summary": (
                    "未找到：当前既没有已构建索引的组件源码，wiki 知识库也未编译，"
                    "无法取证。请先在「组件管理」构建源码索引或编译 wiki。"
                ),
                "citations": [],
                "stats": {
                    "rounds": 0,
                    "tool_calls": 0,
                    "tools_used": [],
                    "empty_handed": True,
                },
                "usage": _zero_usage(),
            }

        tool_schemas = ToolRegistry.get_schemas(groups=groups)
        system_prompt = SUBAGENT_SYSTEM_PROMPT.render(
            sources_desc=render_sources_desc(
                source_components, wiki_ok, _format_indexed_components
            ),
            max_rounds=self.max_iterations,
            summary_budget=self.summary_max_chars // 2,  # 字数预算 ≈ 字符数一半（中文为主）
        )

        # ---- 2. 小 ReAct 循环（消息只活在本次调用内） ----
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]
        totals = _zero_usage()
        loop_guard = LoopGuard()
        force_finalize = False
        summary = ""
        citations: list[str] = []
        tools_used: list[str] = []
        tool_call_count = 0
        productive = False
        rounds = 0

        for iteration in range(self.max_iterations):
            is_last = iteration == self.max_iterations - 1 or force_finalize
            send = list(messages)
            if is_last:
                send.append(
                    {"role": "system", "content": _SUBAGENT_FINAL_DIRECTIVE}
                )
            try:
                response = await self.llm.chat(
                    send, [] if is_last else tool_schemas
                )
            except Exception as e:  # LLM 故障：如实带回，不让工具层抛裸异常
                logger.exception("RetrievalSubAgent LLM call failed")
                summary = f"未找到：检索过程中 LLM 调用失败（{e}），本次取证中断。"
                break
            rounds += 1
            _accumulate_usage(totals, response.get("usage"))
            response.pop("usage", None)
            messages.append(response)

            tool_calls = response.get("tool_calls")
            if tool_calls and not is_last:
                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        func_args = {}
                    logger.info(
                        "SubAgent tool call [%d]: %s(%s)",
                        iteration,
                        func_name,
                        json.dumps(func_args, ensure_ascii=False),
                    )
                    intercept = loop_guard.inspect(func_name, func_args)
                    if intercept is not None:
                        result_str = intercept
                    else:
                        result_str = await ToolRegistry.execute(
                            func_name, func_args, dataset
                        )
                        tool_call_count += 1
                        tools_used.append(func_name)
                        if not _is_unproductive_result(result_str):
                            productive = True
                        _collect_citations(
                            citations, func_name, func_args, result_str
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_str,
                        }
                    )
                if loop_guard.should_finalize:
                    force_finalize = True
                continue

            summary = (response.get("content") or "").strip()
            if summary:
                break
        else:
            logger.warning("RetrievalSubAgent hit max_iterations without a summary")

        if not summary:
            summary = (
                "未找到：检索子 Agent 在轮次预算内没有产出可用摘要。"
                "已尝试的工具：" + (", ".join(tools_used) or "（无）")
            )

        # ---- 3. 收口：截断超长摘要（契约：带回的是摘要不是原文） ----
        if len(summary) > self.summary_max_chars:
            summary = (
                summary[: self.summary_max_chars]
                + "\n（摘要超长已截断——如需更多细节，请针对具体问题再取证一次）"
            )

        stats = {
            "rounds": rounds,
            "tool_calls": tool_call_count,
            "tools_used": tools_used,
            "empty_handed": not productive,
        }
        logger.info(
            "SubAgent done: rounds=%d tool_calls=%d empty_handed=%s tokens=%d",
            rounds,
            tool_call_count,
            stats["empty_handed"],
            totals["total"],
        )
        return {
            "summary": summary,
            "citations": citations,
            "stats": stats,
            "usage": totals,
        }


def _zero_usage() -> dict:
    return {"input": 0, "output": 0, "total": 0, "cache_hit": 0, "cache_miss": 0}


# 单次取证带回的出处上限（防御：正常一次取证远到不了这个数）
_MAX_CITATIONS = 20


def _collect_citations(
    citations: list[str], func_name: str, func_args: dict, result_str: str
) -> None:
    """
    从一次工具结果里抽出处，追加到 citations（去重、封顶）。

    出处 = 子 Agent **真实到访过**的位置，比让 LLM 自报更可信：
    - 源码类结果：递归找 {rel_path, start_line|line} 结构 → "component/rel_path:line"
    - wiki 整页：read_wiki_page 的 slug → "wiki:slug"
    """
    try:
        data = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(data, dict) or data.get("error"):
        return

    component = str(func_args.get("component") or data.get("component") or "")

    def _add(entry: str) -> None:
        if entry and entry not in citations and len(citations) < _MAX_CITATIONS:
            citations.append(entry)

    if func_name == "read_wiki_page":
        _add(f"wiki:{data.get('slug', '')}")
        return

    def _walk(node) -> None:
        if isinstance(node, dict):
            rel = node.get("rel_path")
            if isinstance(rel, str) and rel:
                line = node.get("start_line") or node.get("line")
                prefix = f"{component}/" if component else ""
                _add(f"{prefix}{rel}:{line}" if line else f"{prefix}{rel}")
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
