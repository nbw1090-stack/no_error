"""
工具注册器

提供装饰器风格的注册模式，支持工具发现和 OpenAI 格式 schema 导出。
工具在导入时自动注册到类级别的 _tools 字典中。
"""

import inspect
import json
import logging
from typing import Callable

from agent.tools.base import ToolDefinition
from agent.dataset import LogDataset

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    工具注册器 —— 类级别单例，管理所有可用工具。

    使用方式：
        @ToolRegistry.register(
            name="my_tool",
            description="Does something useful",
            parameters={...},
            required=["param1"],
        )
        async def my_tool(dataset, param1: str, param2: int = 10):
            ...
    """

    _tools: dict[str, ToolDefinition] = {}

    @classmethod
    def register(
        cls,
        name: str,
        description: str,
        parameters: dict,
        required: list[str] | None = None,
        group: str = "log",
    ):
        """
        装饰器：将一个 async 函数注册为工具。

        Args:
            name: 工具唯一名称
            description: 工具功能描述（LLM 据此判断何时调用）
            parameters: JSON Schema 格式的参数定义
            required: 必需参数列表
        """

        def decorator(func: Callable):
            cls._tools[name] = ToolDefinition(
                name=name,
                description=description,
                parameters=parameters,
                func=func,
                required=required or [],
                group=group,
            )
            logger.info("Tool registered: %s", name)
            return func

        return decorator

    @classmethod
    def get_schemas(cls, groups: set[str] | None = None) -> list[dict]:
        """
        返回 OpenAI function-calling 格式的工具 schema 列表。

        用于传递给 LLM 的 tools 参数。传入 groups 时只返回指定分组的工具
        （None = 全部），供 Agent 按需暴露日志工具 / 源码工具。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": td.name,
                    "description": td.description,
                    "parameters": td.parameters,
                },
            }
            for td in cls._tools.values()
            if groups is None or td.group in groups
        ]

    @classmethod
    def get_names(cls, groups: set[str] | None = None) -> list[str]:
        """返回已注册工具的名称列表（传入 groups 时只返回指定分组，None=全部）。"""
        return [
            name
            for name, td in cls._tools.items()
            if groups is None or td.group in groups
        ]

    @classmethod
    async def execute(cls, name: str, arguments: dict, dataset: LogDataset) -> str:
        """
        执行指定工具并返回 JSON 字符串结果。

        Args:
            name: 工具名称
            arguments: 工具参数字典
            dataset: 日志数据集

        Returns:
            JSON 字符串格式的执行结果
        """
        tool = cls._tools.get(name)
        if not tool:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # LLM 偶尔会臆造工具没有的参数（如把 max_lines 写成 lines），直接 **传入
        # 会触发 TypeError 报错、白白浪费一轮 ReAct。这里按函数签名过滤掉未知参数，
        # 容忍幻觉参数继续执行（被丢弃的参数记 warning，便于发现 prompt/schema 偏差）。
        safe_args = cls._filter_kwargs(tool.func, arguments)

        try:
            result = await tool.func(dataset, **safe_args)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            logger.exception("Tool execution failed: %s", name)
            return json.dumps({"error": str(e)})

    @staticmethod
    def _filter_kwargs(func: Callable, arguments: dict) -> dict:
        """
        按函数签名过滤参数，丢弃工具不接受的未知键（防 LLM 幻觉参数导致 TypeError）。

        函数若声明了 **kwargs 则原样放行（不丢弃任何参数）。被丢弃的键记 warning。
        """
        if not isinstance(arguments, dict):
            return {}
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            return arguments
        params = sig.parameters.values()
        # 有 **kwargs：函数能消化任意键，原样放行
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return arguments
        accepted = {
            p.name
            for p in params
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and p.name != "dataset"  # 首位 dataset 由执行器注入，非 LLM 参数
        }
        filtered = {k: v for k, v in arguments.items() if k in accepted}
        dropped = set(arguments) - set(filtered)
        if dropped:
            logger.warning(
                "Dropping unknown args for tool %s: %s",
                getattr(func, "__name__", func),
                sorted(dropped),
            )
        return filtered
