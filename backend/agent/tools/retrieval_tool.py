"""
retrieve_evidence 工具（group="retrieval"）—— 检索子 Agent 的工具化入口。

主 Agent 在 AGENT_SOURCE_MODE=subagent 下只暴露 log 组 + 本工具：源码/wiki
的多轮检索全部发生在子 Agent 内部，主 Agent 上下文里只落一条摘要+出处。

子 Agent 需要 LLM 适配器，而工具函数签名固定为 (dataset, **kwargs)——因此由
应用启动时调用 configure_retrieval(llm, ...) 注入（main.py / eval.run_eval 与
create_llm 同处装配）。未配置时工具返回结构化 error，不抛异常。
"""

import logging

from agent.tools.registry import ToolRegistry
from agent.dataset import LogDataset

logger = logging.getLogger(__name__)

# 模块级单例：由 configure_retrieval 装配（与 main.py 的模块级 agent 同风格）
_subagent = None


def configure_retrieval(
    llm, max_iterations: int = 6, summary_max_chars: int = 2000
) -> None:
    """
    装配检索子 Agent（幂等，可重复调用以替换 LLM/参数）。

    llm 传 None 表示清除配置（工具降级为报错返回）。
    """
    global _subagent
    if llm is None:
        _subagent = None
        return
    from agent.subagent import RetrievalSubAgent

    _subagent = RetrievalSubAgent(
        llm=llm,
        max_iterations=max_iterations,
        summary_max_chars=summary_max_chars,
    )
    logger.info(
        "Retrieval sub-agent configured: max_iterations=%d, summary_max_chars=%d",
        max_iterations,
        summary_max_chars,
    )


@ToolRegistry.register(
    name="retrieve_evidence",
    description=(
        "派出检索子 Agent，去已索引的组件源码和 openUBMC wiki 里查证一个问题，"
        "带回紧凑摘要 + 出处（file:line / wiki 页）。当需要源码级根因证据"
        "（某个报错位置的真实代码、函数行为、调用链）或架构/设计/接口知识时调用。"
        "把已知线索全部写进 query：组件名、file:line、报错原文、想确认什么——"
        "子 Agent 看不到本会话，query 必须自包含。一次查一个焦点问题；"
        "若结果指向更深的函数/接口，再发起一次新的调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "要查证什么，一句话说全（自包含）：组件名 + file:line + "
                    "报错原文 + 具体想确认的问题"
                ),
            },
        },
        "required": ["query"],
    },
    group="retrieval",
)
async def retrieve_evidence(dataset: LogDataset, query: str) -> dict:
    """执行一次取证：转交检索子 Agent，返回摘要+出处+健康统计。"""
    if _subagent is None:
        return {
            "error": "检索子 Agent 未配置（LLM 不可用）",
            "hint": "服务端未装配 retrieval 子 Agent；请检查 LLM 配置后重启。",
        }
    if not (query or "").strip():
        return {
            "error": "缺少 query",
            "hint": "请用一句自包含的话说明要查证什么（组件、file:line、报错原文、问题）。",
        }
    result = await _subagent.run(query.strip(), dataset)
    result["guidance"] = (
        "以上是检索子 Agent 带回的摘要与出处。citations 里的 file:line / wiki 页"
        "是它真实读过的位置，可直接在最终回答中引用；不要臆造摘要之外的代码细节。"
        "若还有未查清的点，可再发起一次 retrieve_evidence。"
    )
    return result
