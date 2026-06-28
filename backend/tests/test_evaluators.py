"""
eval.evaluators 单测：确定性指标取值 + LLM 裁判解析/降级。

全程离线：裁判用 fake llm（async chat 返回固定 JSON），不触网。
"""

import agent.tools  # noqa: F401  # 触发工具注册，source_tool_used 需要 source 组工具名

from eval import evaluators as E


def _traj(names, *, iterations=None, duplicate_calls=0, tool_calls=None):
    return {
        "tool_calls": tool_calls or [{"name": n, "args": {}, "result": None, "error": False} for n in names],
        "tool_names": names,
        "tool_call_count": len(names),
        "iterations": iterations if iterations is not None else max(1, len(names)),
        "duplicate_calls": duplicate_calls,
    }


def _out(reply, names=()):
    return {"reply": reply, "trajectory": _traj(list(names))}


_EXP = {
    "root_cause_keywords": ["pcie_oob_mgmt_init", "CAPABILITY"],
    "expected_component": "pcie_device",
    "expected_citations": ["pcie_card.lua:49"],
    "expected_tools": ["gather_code_context"],
    "must_use_source": True,
    "reference_answer": "...",
}


def test_coerce_expected_legacy_string():
    exp = E.coerce_expected("oob 初始化失败")
    assert exp["root_cause_keywords"] == ["oob 初始化失败"]
    assert exp["must_use_source"] is False


def test_diagnosis_acc_hit_and_miss():
    hit = E.diagnosis_acc(input={}, output=_out("根因在 pcie_oob_mgmt_init"), expected_output=_EXP, metadata={})
    assert hit.value == 1.0
    miss = E.diagnosis_acc(input={}, output=_out("无关回答"), expected_output=_EXP, metadata={})
    assert miss.value == 0.0


def test_keyword_coverage_fraction():
    ev = E.keyword_coverage(
        input={}, output=_out("调用 pcie_oob_mgmt_init"), expected_output=_EXP, metadata={}
    )
    assert ev.value == 0.5  # 命中 1/2


def test_citation_present_and_match():
    out = _out("详见 pcie_card.lua:49 的实现")
    assert E.citation_present(input={}, output=out, expected_output=_EXP, metadata={}).value == 1.0
    assert E.citation_match(input={}, output=out, expected_output=_EXP, metadata={}).value == 1.0
    # 括号写法也认
    out2 = _out("见 pcie_card.lua(49)")
    assert E.citation_match(input={}, output=out2, expected_output=_EXP, metadata={}).value == 1.0
    # 无引用
    assert E.citation_present(input={}, output=_out("没有引用"), expected_output=_EXP, metadata={}).value == 0.0


def test_component_named():
    assert E.component_named(input={}, output=_out("pcie_device 出错"), expected_output=_EXP, metadata={}).value == 1.0
    assert E.component_named(input={}, output=_out("其它"), expected_output=_EXP, metadata={}).value == 0.0


def test_expected_tools_invoked():
    hit = E.expected_tools_invoked(
        input={}, output=_out("x", names=["gather_code_context"]), expected_output=_EXP, metadata={}
    )
    assert hit.value == 1.0
    miss = E.expected_tools_invoked(
        input={}, output=_out("x", names=["search_logs"]), expected_output=_EXP, metadata={}
    )
    assert miss.value == 0.0


def test_source_tool_used_and_skip():
    # 调了源码工具
    used = E.source_tool_used(
        input={}, output=_out("x", names=["gather_code_context"]), expected_output=_EXP, metadata={}
    )
    assert used.value == 1.0
    # 没调任何源码工具
    notused = E.source_tool_used(
        input={}, output=_out("x", names=["search_logs"]), expected_output=_EXP, metadata={}
    )
    assert notused.value == 0.0
    # must_use_source=False → 跳过(None)
    exp_no = dict(_EXP, must_use_source=False)
    skip = E.source_tool_used(input={}, output=_out("x"), expected_output=exp_no, metadata={})
    assert skip.value is None


def test_iteration_efficiency():
    out = {"reply": "x", "trajectory": _traj(["a", "b"], iterations=2)}
    ev = E.iteration_efficiency(input={}, output=out, expected_output=_EXP, metadata={"expected_max_iterations": 4})
    assert ev.value == 1.0  # 2 <= 4 → 1.0
    out2 = {"reply": "x", "trajectory": _traj(["a"] * 8, iterations=8)}
    ev2 = E.iteration_efficiency(input={}, output=out2, expected_output=_EXP, metadata={"expected_max_iterations": 4})
    assert ev2.value == 0.5  # 4/8


def test_no_redundant_calls():
    out = {"reply": "x", "trajectory": _traj(["a", "a", "b"], duplicate_calls=1)}
    ev = E.no_redundant_calls(input={}, output=out, expected_output=_EXP, metadata={})
    assert abs(ev.value - (1 - 1 / 3)) < 1e-9


def test_tool_call_count():
    ev = E.tool_call_count(input={}, output=_out("x", names=["a", "b"]), expected_output=_EXP, metadata={})
    assert ev.value == 2.0


# ---------- LLM 裁判 ----------

class _FakeJudge:
    def __init__(self, content):
        self._content = content

    async def chat(self, messages, tools=None):
        return {"role": "assistant", "content": self._content, "tool_calls": None}


def test_llm_judge_parses_score():
    judge = _FakeJudge('好的，评分如下：\n{"score": 0.8, "reason": "根因正确"}')
    evals = E.make_llm_evaluators(judge)
    names = {e.__name__ for e in evals}
    assert {"rootcause_correctness", "faithfulness", "helpfulness"} <= names
    rc = [e for e in evals if e.__name__ == "rootcause_correctness"][0]
    ev = rc(input={"question": "q"}, output=_out("答"), expected_output=_EXP, metadata={})
    assert ev.value == 0.8
    assert "根因正确" in ev.comment


def test_llm_judge_clamps_and_degrades_on_garbage():
    # 越界分数被裁剪到 [0,1]
    judge_hi = _FakeJudge('{"score": 5, "reason": "x"}')
    rc = [e for e in E.make_llm_evaluators(judge_hi) if e.__name__ == "rootcause_correctness"][0]
    assert rc(input={"question": "q"}, output=_out("答"), expected_output=_EXP, metadata={}).value == 1.0

    # 非 JSON → 降级：经 na_to_none 包装后返回 Python None（不产出 score）
    judge_bad = _FakeJudge("我无法评分")
    rc_bad = [e for e in E.make_llm_evaluators(judge_bad) if e.__name__ == "rootcause_correctness"][0]
    assert rc_bad(input={"question": "q"}, output=_out("答"), expected_output=_EXP, metadata={}) is None


def test_llm_judge_none_llm_degrades():
    # judge_llm 缺失：每个裁判 evaluator 经 na_to_none 返回 Python None
    evals = E.make_llm_evaluators(None)
    for e in evals:
        assert e(input={"question": "q"}, output=_out("答"), expected_output=_EXP, metadata={}) is None


def test_na_to_none_wraps_deterministic_skip():
    """DETERMINISTIC_EVALUATORS 里的 source_tool_used 在 must_use_source=False 时返回 None。"""
    src = [e for e in E.DETERMINISTIC_EVALUATORS if e.__name__ == "source_tool_used"][0]
    exp_no = dict(_EXP, must_use_source=False)
    assert src(input={}, output=_out("x"), expected_output=exp_no, metadata={}) is None
    # 适用时仍返回 Evaluation
    ev = src(input={}, output=_out("x", names=["gather_code_context"]), expected_output=_EXP, metadata={})
    assert ev is not None and ev.value == 1.0
