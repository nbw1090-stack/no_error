"""
工具注册器

提供装饰器风格的注册模式，支持工具发现和 OpenAI 格式 schema 导出。
工具在导入时自动注册到类级别的 _tools 字典中。
"""

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
            )
            logger.info("Tool registered: %s", name)
            return func

        return decorator

    @classmethod
    def get_schemas(cls) -> list[dict]:
        """
        返回 OpenAI function-calling 格式的工具 schema 列表。

        用于传递给 LLM 的 tools 参数。
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
        ]

    @classmethod
    def get_names(cls) -> list[str]:
        """返回所有已注册工具的名称列表"""
        return list(cls._tools.keys())

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

        try:
            result = await tool.func(dataset, **arguments)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            logger.exception("Tool execution failed: %s", name)
            return json.dumps({"error": str(e)})
