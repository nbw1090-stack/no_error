"""PromptTemplate.render 单元测试"""

from agent.prompts.template import PromptTemplate


def test_render_substitutes_vars():
    t = PromptTemplate("Hello ${name}, count=${n}")
    assert t.render(name="world", n=3) == "Hello world, count=3"


def test_safe_substitute_leaves_unknown_literal():
    # 未提供 y：safe_substitute 原样保留 ${y}，不抛异常
    t = PromptTemplate("${x} and ${y}")
    assert t.render(x="1") == "1 and ${y}"


def test_no_kwargs_returns_template():
    t = PromptTemplate("static ${x}")
    assert t.render() == "static ${x}"
