"""
评测打分器（evaluators）。

两层：
- 确定性指标：零成本、可离线、可进 CI。只看回复文本 + 工具轨迹，规则判定。
- LLM-as-judge：复用项目自带的 OpenAI 兼容适配器，对"根因正确性/源码忠实度/
  可执行性"等语义质量打分；裁判不可用（无 key / 解析失败）时降级为 value=None，
  绝不让整轮评测崩。

所有 evaluator 统一签名（对齐 Langfuse run_experiment）：
    (*, input, output, expected_output, metadata, **kwargs) -> Evaluation
其中 output 由 run_eval.task 返回 {"reply": str, "trajectory": dict}（兼容旧版裸
字符串）；expected_output 为结构化 dict（兼容旧版裸关键词字符串）。

value=None 表示"本条不适用/裁判缺失"，scorecard 聚合时跳过、不计入均值。
"""

import json
import re

from langfuse import Evaluation

from agent.tools.registry import ToolRegistry
from eval.aio import run_coro_blocking

# 回复中的 file:line 引用：foo.lua:49 / app.log:1423 / bar.c(88) 三种写法都认。
_CITATION_RE = re.compile(r"\b([\w./-]+\.(?:lua|c|cpp|h|hpp|log))[:(](\d+)\)?")


# ============================================================
# 入参/出参归一化
# ============================================================

def _reply(output) -> str:
    """从 task 输出取回复文本（兼容旧版直接返回字符串）。"""
    if isinstance(output, dict):
        return output.get("reply") or ""
    return output or ""


def _traj(output) -> dict:
    """从 task 输出取工具轨迹（旧版无轨迹时给空结构）。"""
    if isinstance(output, dict) and isinstance(output.get("trajectory"), dict):
        return output["trajectory"]
    return {
        "tool_calls": [],
        "tool_names": [],
        "tool_call_count": 0,
        "iterations": 0,
        "duplicate_calls": 0,
    }


def coerce_expected(expected_output) -> dict:
    """
    把 expected_output 归一成结构化 dict。

    兼容旧版：裸字符串当作单个根因关键词。缺省字段补全，便于 evaluator 直接取用。
    """
    if isinstance(expected_output, str):
        exp: dict = {"root_cause_keywords": [expected_output]} if expected_output else {}
    elif isinstance(expected_output, dict):
        exp = dict(expected_output)
    else:
        exp = {}
    exp.setdefault("root_cause_keywords", [])
    exp.setdefault("expected_component", "")
    exp.setdefault("expected_components", [])
    exp.setdefault("expected_citations", [])
    exp.setdefault("expected_tools", [])
    exp.setdefault("must_use_source", False)
    exp.setdefault("reference_answer", "")
    # 组件归一：单值 expected_component 与列表 expected_components 合并成一个去重保序列表。
    # 跨组件流程用例可直接给 expected_components=[消费方, 依赖方, ...]；旧用例的单值照常生效。
    raw = []
    if isinstance(exp["expected_component"], str):
        raw.append(exp["expected_component"])
    elif isinstance(exp["expected_component"], list):
        raw.extend(exp["expected_component"])
    if isinstance(exp["expected_components"], list):
        raw.extend(exp["expected_components"])
    elif isinstance(exp["expected_components"], str):
        raw.append(exp["expected_components"])
    merged, seen = [], set()
    for c in raw:
        c = (c or "").strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            merged.append(c)
    exp["expected_components"] = merged
    return exp


def _citations_in(text: str) -> set[str]:
    """抽取回复里的 file:line 引用，归一成 'basename:line'（小写）便于比对。"""
    out = set()
    for fname, line in _CITATION_RE.findall(text or ""):
        base = fname.rsplit("/", 1)[-1].lower()
        out.add(f"{base}:{line}")
    return out


def _norm_citation(c: str) -> str:
    """期望引用归一：'src/foo.lua:49' / 'foo.lua(49)' → 'foo.lua:49'（小写）。"""
    c = c.strip().lower().replace("(", ":").replace(")", "")
    base = c.rsplit("/", 1)[-1]
    return base


# ============================================================
# 确定性 evaluators
# ============================================================

def diagnosis_acc(*, input, output, expected_output, metadata=None, **kwargs):
    """任一根因关键词出现在回复里（命中=1.0）。沿用旧名，向后兼容。"""
    reply = _reply(output).lower()
    kws = coerce_expected(expected_output)["root_cause_keywords"]
    hit = any(k.lower() in reply for k in kws) if kws else False
    return Evaluation(
        name="diagnosis_acc",
        value=1.0 if hit else 0.0,
        comment="命中期望根因关键词" if hit else "未命中期望根因关键词",
    )


def keyword_coverage(*, input, output, expected_output, metadata=None, **kwargs):
    """命中的期望关键词占比（比 diagnosis_acc 更细）。"""
    reply = _reply(output).lower()
    kws = coerce_expected(expected_output)["root_cause_keywords"]
    if not kws:
        return Evaluation(name="keyword_coverage", value=None, comment="无期望关键词")
    hits = sum(1 for k in kws if k.lower() in reply)
    return Evaluation(
        name="keyword_coverage",
        value=hits / len(kws),
        comment=f"命中 {hits}/{len(kws)} 关键词",
    )


def citation_present(*, input, output, expected_output, metadata=None, **kwargs):
    """回复是否带 file:line 引用（衡量是否落到具体代码/日志位置）。"""
    cited = _citations_in(_reply(output))
    return Evaluation(
        name="citation_present",
        value=1.0 if cited else 0.0,
        comment=f"引用 {len(cited)} 处 file:line" if cited else "无 file:line 引用",
    )


def citation_match(*, input, output, expected_output, metadata=None, **kwargs):
    """引用的 file:line 是否落在期望集合（比 present 更强）。"""
    exp = coerce_expected(expected_output)
    want = {_norm_citation(c) for c in exp["expected_citations"]}
    if not want:
        return Evaluation(name="citation_match", value=None, comment="无期望引用")
    got = _citations_in(_reply(output))
    matched = want & got
    return Evaluation(
        name="citation_match",
        value=len(matched) / len(want),
        comment=f"命中期望引用 {len(matched)}/{len(want)}",
    )


def component_named(*, input, output, expected_output, metadata=None, **kwargs):
    """是否点名期望组件，支持多组件（跨组件流程）：按命中比例给分。

    单组件用例退化为 1.0/0.0，与旧行为一致；跨组件用例要求把依赖链上的各组件
    （如 pcie_device + bios）都点出来，少点一个就扣相应比例。
    """
    exp = coerce_expected(expected_output)
    comps = exp["expected_components"]
    if not comps:
        return Evaluation(name="component_named", value=None, comment="无期望组件")
    reply = _reply(output).lower()
    hits = [c for c in comps if c.lower() in reply]
    return Evaluation(
        name="component_named",
        value=len(hits) / len(comps),
        comment=f"点名组件 {len(hits)}/{len(comps)}：{('、'.join(hits)) or '无'}",
    )


def expected_tools_invoked(*, input, output, expected_output, metadata=None, **kwargs):
    """期望工具实际被调用的占比。"""
    exp = coerce_expected(expected_output)
    want = set(exp["expected_tools"])
    if not want:
        return Evaluation(name="expected_tools_invoked", value=None, comment="无期望工具")
    called = set(_traj(output)["tool_names"])
    matched = want & called
    return Evaluation(
        name="expected_tools_invoked",
        value=len(matched) / len(want),
        comment=f"期望工具命中 {len(matched)}/{len(want)}",
    )


def source_tool_used(*, input, output, expected_output, metadata=None, **kwargs):
    """须 ground 到源码时（must_use_source），是否真的调用了 source 组工具。"""
    exp = coerce_expected(expected_output)
    if not exp["must_use_source"]:
        return Evaluation(name="source_tool_used", value=None, comment="本例无需源码")
    source_tools = set(ToolRegistry.get_names(groups={"source"}))
    called = set(_traj(output)["tool_names"])
    used = bool(source_tools & called)
    return Evaluation(
        name="source_tool_used",
        value=1.0 if used else 0.0,
        comment="调用了源码工具" if used else "未调用任何源码工具",
    )


def iteration_efficiency(*, input, output, expected_output, metadata=None, **kwargs):
    """是否在迭代预算内完成（metadata.expected_max_iterations，缺省 4）。"""
    budget = (metadata or {}).get("expected_max_iterations", 4)
    iters = _traj(output)["iterations"]
    if iters <= 0:
        return Evaluation(name="iteration_efficiency", value=None, comment="无迭代记录")
    value = min(1.0, budget / iters)
    return Evaluation(
        name="iteration_efficiency",
        value=value,
        comment=f"迭代 {iters} 轮 / 预算 {budget}",
    )


def no_redundant_calls(*, input, output, expected_output, metadata=None, **kwargs):
    """重复（同名+同参）工具调用惩罚：1 - 重复占比。"""
    traj = _traj(output)
    total = traj["tool_call_count"]
    if total <= 0:
        return Evaluation(name="no_redundant_calls", value=None, comment="无工具调用")
    dup = traj["duplicate_calls"]
    return Evaluation(
        name="no_redundant_calls",
        value=1.0 - dup / total,
        comment=f"重复调用 {dup}/{total}",
    )


def tool_call_count(*, input, output, expected_output, metadata=None, **kwargs):
    """工具调用次数（观测量，非达标判定）。"""
    return Evaluation(
        name="tool_call_count",
        value=float(_traj(output)["tool_call_count"]),
        comment="工具调用总次数",
    )


import functools


def na_to_none(fn):
    """
    把 value=None 的"不适用/裁判缺失"结果转成 Python None。

    为什么必须这样：Langfuse run_experiment 会把 evaluator 返回的每个 Evaluation
    自动建成 score，而 Langfuse 的 ScoreBody **不接受 null 值**（value 必须是数字
    或字符串）。所以"本例不适用"的指标不能返回 Evaluation(value=None)，而要返回
    Python None —— run_experiment 见 None 即跳过，离线侧也据此过滤。functools.wraps
    保留 __name__，离线日志与测试按名取用不受影响。
    """

    @functools.wraps(fn)
    def wrapped(**kwargs):
        ev = fn(**kwargs)
        if ev is None or getattr(ev, "value", None) is None:
            return None
        return ev

    return wrapped


# 确定性 evaluator 清单（run_eval 装配用）——统一包一层 na_to_none
DETERMINISTIC_EVALUATORS = [
    na_to_none(f)
    for f in (
        diagnosis_acc,
        keyword_coverage,
        citation_present,
        citation_match,
        component_named,
        expected_tools_invoked,
        source_tool_used,
        iteration_efficiency,
        no_redundant_calls,
        tool_call_count,
    )
]


# ============================================================
# LLM-as-judge evaluators
# ============================================================

def _run_async(coro):
    """在同步 evaluator 里安全跑一个协程；run_experiment 在运行中 loop 里调用时自动改用线程。"""
    return run_coro_blocking(coro)


def _parse_score(text: str) -> dict | None:
    """从裁判回复里解析 {"score": 0-1, "reason": "..."}；失败返回 None。"""
    if not text:
        return None
    # 容忍 ```json ... ``` 包裹或前后多余文本：截取第一个 {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    score = data.get("score")
    try:
        score = float(score)
    except (TypeError, ValueError):
        return None
    score = max(0.0, min(1.0, score))
    return {"score": score, "reason": str(data.get("reason", ""))[:300]}


async def _judge(llm, system: str, user: str) -> dict | None:
    """调一次裁判 LLM，返回解析后的分数 dict；任何异常 → None（降级）。"""
    try:
        resp = await llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=None,
        )
    except Exception:
        return None
    return _parse_score((resp or {}).get("content") or "")


def _evidence(output) -> str:
    """把工具轨迹序列化成裁判可读的"证据"，用于忠实度判定。"""
    lines = []
    for c in _traj(output)["tool_calls"]:
        res = c.get("result")
        res_str = json.dumps(res, ensure_ascii=False, default=str)
        lines.append(f"- {c['name']}({json.dumps(c['args'], ensure_ascii=False)}) -> {res_str[:600]}")
    return "\n".join(lines) if lines else "(本轮未调用任何工具)"


_JUDGE_DEGRADED = "裁判不可用（无 LLM key 或解析失败），已降级跳过"


def make_llm_evaluators(judge_llm):
    """
    构造 LLM-as-judge evaluator 列表（注入裁判 llm）。judge_llm 为 None 时，
    返回的 evaluator 一律产出 value=None（降级），保证确定性指标仍能出完整记分卡。
    """

    def rootcause_correctness(*, input, output, expected_output, metadata=None, **kwargs):
        if judge_llm is None:
            return Evaluation(name="rootcause_correctness", value=None, comment=_JUDGE_DEGRADED)
        exp = coerce_expected(expected_output)
        question = (input or {}).get("question", "") if isinstance(input, dict) else ""
        ref = exp["reference_answer"] or "、".join(exp["root_cause_keywords"])
        system = (
            "你是 BMC 日志诊断的严格评审。判断「待评回答」是否正确定位了根因。"
            "只输出 JSON：{\"score\": 0到1之间的小数, \"reason\": \"简短中文理由\"}。"
            "1=根因完全正确，0=完全错误或答非所问。"
        )
        user = (
            f"用户问题：{question}\n\n参考根因：{ref}\n\n待评回答：\n{_reply(output)}"
        )
        res = _run_async(_judge(judge_llm, system, user))
        if res is None:
            return Evaluation(name="rootcause_correctness", value=None, comment=_JUDGE_DEGRADED)
        return Evaluation(name="rootcause_correctness", value=res["score"], comment=res["reason"])

    def faithfulness(*, input, output, expected_output, metadata=None, **kwargs):
        if judge_llm is None:
            return Evaluation(name="faithfulness", value=None, comment=_JUDGE_DEGRADED)
        system = (
            "你是严格的事实核查员。判断「回答」中的结论是否都能由「检索证据」"
            "（agent 调用工具拿到的日志/源码）支撑，有没有编造证据里没有的代码/函数/结论。"
            "只输出 JSON：{\"score\": 0到1之间的小数, \"reason\": \"简短中文理由\"}。"
            "1=完全有据，0=明显编造。若回答如实声明信息不足/需先建索引，视为忠实(高分)。"
        )
        user = f"检索证据：\n{_evidence(output)}\n\n回答：\n{_reply(output)}"
        res = _run_async(_judge(judge_llm, system, user))
        if res is None:
            return Evaluation(name="faithfulness", value=None, comment=_JUDGE_DEGRADED)
        return Evaluation(name="faithfulness", value=res["score"], comment=res["reason"])

    def helpfulness(*, input, output, expected_output, metadata=None, **kwargs):
        if judge_llm is None:
            return Evaluation(name="helpfulness", value=None, comment=_JUDGE_DEGRADED)
        question = (input or {}).get("question", "") if isinstance(input, dict) else ""
        system = (
            "你是运维专家评审。判断「回答」对排查问题是否有可执行价值："
            "是否给出了具体的下一步排查/修复方向，而非空泛套话。"
            "只输出 JSON：{\"score\": 0到1之间的小数, \"reason\": \"简短中文理由\"}。"
        )
        user = f"用户问题：{question}\n\n回答：\n{_reply(output)}"
        res = _run_async(_judge(judge_llm, system, user))
        if res is None:
            return Evaluation(name="helpfulness", value=None, comment=_JUDGE_DEGRADED)
        return Evaluation(name="helpfulness", value=res["score"], comment=res["reason"])

    return [na_to_none(rootcause_correctness), na_to_none(faithfulness), na_to_none(helpfulness)]
