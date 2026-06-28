"""
本地记分卡聚合。

把每条用例产出的 Evaluation 列表汇总成：
- 每个指标的均值（跳过 value=None 的"不适用/裁判缺失"）
- 按 metadata.category / difficulty 的分组均值
并打印表格、可选 dump 成 JSON，供离线/CI 出分（无需 Langfuse UI）。

输入约定：results = [
    {"input":..., "expected_output":..., "metadata":..., "evaluations":[Evaluation,...]},
    ...
]
其中 Evaluation 为 langfuse.Evaluation（有 .name / .value / .comment 属性）。
"""

import json
import os


def _val(ev):
    """取 Evaluation 的数值；None / 非数值返回 None（聚合时跳过）。"""
    v = getattr(ev, "value", None)
    if isinstance(v, bool):  # 防 True/False 被当数字
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def aggregate(results: list[dict]) -> dict:
    """汇总成 {overall:{metric:mean}, by_category:{...}, by_difficulty:{...}, n}。"""
    overall: dict[str, list] = {}
    by_cat: dict[str, dict[str, list]] = {}
    by_diff: dict[str, dict[str, list]] = {}

    for r in results:
        meta = r.get("metadata") or {}
        cat = meta.get("category", "uncategorized")
        diff = meta.get("difficulty", "unknown")
        for ev in r.get("evaluations") or []:
            name = getattr(ev, "name", "?")
            v = _val(ev)
            overall.setdefault(name, []).append(v)
            by_cat.setdefault(cat, {}).setdefault(name, []).append(v)
            by_diff.setdefault(diff, {}).setdefault(name, []).append(v)

    def _collapse(d):
        return {m: _mean(vs) for m, vs in d.items()}

    return {
        "n": len(results),
        "overall": _collapse(overall),
        "by_category": {c: _collapse(m) for c, m in by_cat.items()},
        "by_difficulty": {d: _collapse(m) for d, m in by_diff.items()},
    }


def _fmt(v):
    return "  n/a" if v is None else f"{v:5.2f}"


def render_table(summary: dict) -> str:
    """把聚合结果渲染成可读文本表。"""
    lines = [f"\n===== Eval Scorecard (n={summary['n']}) ====="]
    lines.append("\n[Overall]")
    for m, v in sorted(summary["overall"].items()):
        lines.append(f"  {m:<24} {_fmt(v)}")

    lines.append("\n[By category]")
    metrics = sorted(summary["overall"].keys())
    for cat, mv in sorted(summary["by_category"].items()):
        lines.append(f"  · {cat}")
        for m in metrics:
            if m in mv:
                lines.append(f"      {m:<22} {_fmt(mv[m])}")

    lines.append("\n[By difficulty]")
    for diff, mv in sorted(summary["by_difficulty"].items()):
        lines.append(f"  · {diff}")
        for m in metrics:
            if m in mv:
                lines.append(f"      {m:<22} {_fmt(mv[m])}")
    return "\n".join(lines)


def dump_json(results: list[dict], summary: dict, path: str) -> None:
    """把逐条明细 + 聚合落盘成 JSON（Evaluation 转成可序列化 dict）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    serializable = []
    for r in results:
        serializable.append(
            {
                "input": r.get("input"),
                "expected_output": r.get("expected_output"),
                "metadata": r.get("metadata"),
                "reply": (r.get("output") or {}).get("reply")
                if isinstance(r.get("output"), dict)
                else r.get("output"),
                "trajectory": (r.get("output") or {}).get("trajectory")
                if isinstance(r.get("output"), dict)
                else None,
                "scores": [
                    {
                        "name": getattr(ev, "name", "?"),
                        "value": getattr(ev, "value", None),
                        "comment": getattr(ev, "comment", ""),
                    }
                    for ev in r.get("evaluations") or []
                ],
            }
        )
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "cases": serializable}, f, ensure_ascii=False, indent=2)
