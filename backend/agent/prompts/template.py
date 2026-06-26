"""
提示词模板引擎

基于 Python string.Template，支持 ${variable} 风格的变量替换。
选择 string.Template 而非 Jinja2 以保持零额外依赖。
"""

from string import Template


class PromptTemplate:
    """
    提示词模板 —— 支持 ${variable} 替换。

    safe_substitute 确保未提供变量时不会抛出异常，
    未替换的占位符会原样保留在输出中。
    """

    def __init__(self, template_str: str):
        self._template = Template(template_str)

    def render(self, **kwargs) -> str:
        """
        用给定变量渲染模板。

        Args:
            **kwargs: 模板变量的键值对

        Returns:
            渲染后的字符串
        """
        return self._template.safe_substitute(**kwargs)
