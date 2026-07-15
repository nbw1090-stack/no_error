"""
工具模块 —— 导入即注册

导入各工具模块会触发所有 @ToolRegistry.register() 装饰器执行。
"""

from agent.tools.log_tools import *  # noqa: F401, F403
from agent.tools.source_tools import *  # noqa: F401, F403
from agent.tools.wiki_tools import *  # noqa: F401, F403
from agent.tools.retrieval_tool import *  # noqa: F401, F403
