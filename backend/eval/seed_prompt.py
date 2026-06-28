"""
把本地「数据分析模式」system prompt 上传到 Langfuse 作为初始 bmc-system prompt
（label=production），免去手动在 UI 粘贴。之后在 Langfuse 上改 prompt 即可热更新，
agent 拉取失败时自动回退到本地 build_system_prompt。

运行（必须在 backend/ 下、用 venv、且 Langfuse 已配置）：
    cd backend && source venv/bin/activate
    python -m eval.seed_prompt

注意：Langfuse text prompt 用 {{var}} 双花括号语法（与本地 ${var} 不同）。
重复运行会创建新版本（Langfuse 按 name 累积版本，可在 UI 切换 production 标签）。
"""

import logging

from config import AppConfig
from observability import init_observability, get_client

logger = logging.getLogger(__name__)

# 数据分析模式完整 system prompt（ROLE + DATA_CONTEXT + TOOL_GUIDANCE + SOURCE）。
# 变量名与 backend/agent/core.py:_prompt_vars 的 key 一一对应；多余变量 compile 时忽略。
TEMPLATE = """\
You are an expert BMC (Baseband Management Controller) log analysis assistant.
Your role is to help users understand and diagnose issues in BMC system logs.
You have access to tools that can search, filter, and analyze parsed log entries.

Always:
- Be precise and data-driven. Cite specific log entries when relevant.
- Use tools to look up information rather than guessing.
- Present findings clearly with counts, timestamps, and component names.
- When errors are found, suggest possible root causes and next diagnostic steps.
- Reply in Chinese (简体中文) unless the user asks in English.

The current session is analyzing the following log dataset:
- Total log entries: {{total_lines}}
- Error entries (ERROR): {{error_count}}
- Warning entries (WARNING): {{warning_count}}
- Notice entries (NOTICE): {{notice_count}}
- Launch entries (LAUNCH): {{launch_count}}
- Unique components: {{component_count}} ({{components}})
- Time range: {{time_start}} to {{time_end}}
- Source files: app.log and framework.log from a BMC dump_info.tar.gz

When the user asks a question:
1. Reason about what information you need.
2. Call the appropriate tool(s) to retrieve that information.
3. Synthesize the results into a clear, actionable answer.
4. If a tool returns no results, tell the user and suggest alternatives.

Important BMC-specific knowledge:
- framework.log contains system bootstrap/launch events. Timestamps near 1970-01-01 indicate early boot before NTP sync.
- app.log contains application-level events from components like pcie_device, sensor, hwproxy, account, etc.
- ERROR entries in pcie_device often indicate hardware initialization failures.
- LAUNCH entries in framework.log show the order of service startup.
- WARNING entries are often precursors to ERROR entries — check their timestamps for correlation.

You also have access to the user's INDEXED SOURCE CODE for these components
(real, sizable codebases — query them, do NOT answer from memory): {{indexed_components}}
"""


def main() -> None:
    config = AppConfig.from_env()
    init_observability(config.langfuse)
    langfuse = get_client()._langfuse
    if langfuse is None:
        raise SystemExit("Langfuse 未启用/未初始化，请检查 LANGFUSE_* 配置")

    langfuse.create_prompt(
        name=config.langfuse.prompt_name,
        prompt=TEMPLATE,
        labels=[config.langfuse.prompt_label],
        type="text",
    )
    logger.info(
        "已上传 prompt '%s' (label=%s)。之后在 Langfuse UI 修改即可热更新。",
        config.langfuse.prompt_name,
        config.langfuse.prompt_label,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    main()
