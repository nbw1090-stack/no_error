"""
工具定义数据类

描述一个可在 Agent 中调用的工具。
"""

from dataclasses import dataclass, field
from typing import Callable, Any


@dataclass
class ToolDefinition:
    """工具的完整定义，包含名称、描述、参数 schema 和处理函数"""

    name: str
    description: str
    parameters: dict  # JSON Schema 格式的参数定义
    func: Callable  # async callable(dataset: LogDataset, **kwargs) -> Any
    required: list[str] = field(default_factory=list)
