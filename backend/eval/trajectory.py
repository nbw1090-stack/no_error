"""
工具调用轨迹重建。

agent.core.Agent.run 只返回最终文本，拿不到中间的工具调用。但 ReAct 循环里
每条 assistant（含 tool_calls）与每条 tool 结果都被 session_manager 持久化了，
所以一次 eval run 跑完后，直接读临时 session 的 messages 就能无侵入地重建轨迹
——无需改动 agent 热路径（run/run_stream/main 共用）。

供 evaluators 打"过程类"指标：工具选得对不对、有没有真的 ground 到源码、
迭代是否高效、有无重复无效调用。
"""

import json

from agent.session import SessionManager


def _try_json(s):
    """尽量把工具结果 content 解析成 dict/list；失败则原样返回字符串。"""
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def call_signature(name: str, args: dict) -> str:
    """(工具名 + 规范化参数) 签名，用于检测重复调用（对齐 core._tool_signature）。"""
    try:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(args)
    return f"{name}|{canonical}"


def extract_trajectory(session_manager: SessionManager, session_id: str) -> dict:
    """
    从持久化的 session 消息重建工具调用轨迹。

    仅依赖 SessionManager.get(session_id).messages —— 不改 agent。

    Returns:
        {
          "tool_calls": [{"name","args","result","error"}],  # 按调用顺序
          "tool_names": [str, ...],
          "tool_call_count": int,
          "iterations": int,        # assistant 轮次数 ≈ ReAct 迭代数
          "duplicate_calls": int,   # 同 (名+参) 重复出现的次数
        }
    """
    session = session_manager.get(session_id)
    msgs = session.messages if session else []

    calls: list[dict] = []
    by_id: dict[str, dict] = {}
    iterations = 0

    for m in msgs:
        role = m.get("role")
        if role == "assistant":
            iterations += 1
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                rec = {"name": name, "args": args, "result": None, "error": False}
                calls.append(rec)
                cid = tc.get("id")
                if cid is not None:
                    by_id[cid] = rec
        elif role == "tool":
            rec = by_id.get(m.get("tool_call_id"))
            if rec is not None:
                parsed = _try_json(m.get("content"))
                rec["result"] = parsed
                rec["error"] = isinstance(parsed, dict) and bool(parsed.get("error"))

    seen: set[str] = set()
    duplicate_calls = 0
    for c in calls:
        sig = call_signature(c["name"], c["args"])
        if sig in seen:
            duplicate_calls += 1
        else:
            seen.add(sig)

    return {
        "tool_calls": calls,
        "tool_names": [c["name"] for c in calls],
        "tool_call_count": len(calls),
        "iterations": iterations,
        "duplicate_calls": duplicate_calls,
    }
