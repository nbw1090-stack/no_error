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
          "tool_names": [str, ...],   # 含子 Agent 内层实际调用的工具（拉平）
          "tool_call_count": int,     # 仅主 Agent 直接调用数
          "iterations": int,        # assistant 轮次数 ≈ ReAct 迭代数
          "duplicate_calls": int,   # 同 (名+参) 重复出现的次数
          "sub_calls": int,          # retrieve_evidence 取证次数
          "sub_rounds": int,         # 子 Agent 累计 LLM 轮次
          "sub_tool_calls": int,     # 子 Agent 累计工具调用数
          "sub_empty_handed": int,   # 空手而归的取证次数
          "sub_usage_total": int,    # 子 Agent 累计 token（含缓存）
        }

    subagent 模式下，retrieve_evidence 结果里携带子 Agent 的 stats/usage；
    这里把内层 tools_used 拉平进 tool_names——使 source_tool_used /
    expected_tools_invoked 等指标在两种架构下比的是同一件事：整条流水线
    是否真的 ground 到了源码/wiki。
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

    # 拉平子 Agent 内层轨迹（仅 retrieve_evidence 结果里带 stats 的）
    tool_names = [c["name"] for c in calls]
    sub_calls = sub_rounds = sub_tool_calls = sub_empty = sub_usage = 0
    for c in calls:
        if c["name"] != "retrieve_evidence" or not isinstance(c["result"], dict):
            continue
        stats = c["result"].get("stats")
        if not isinstance(stats, dict):
            continue
        sub_calls += 1
        sub_rounds += stats.get("rounds") or 0
        sub_tool_calls += stats.get("tool_calls") or 0
        sub_empty += 1 if stats.get("empty_handed") else 0
        tool_names.extend(stats.get("tools_used") or [])
        usage = c["result"].get("usage")
        if isinstance(usage, dict):
            sub_usage += usage.get("total") or 0

    return {
        "tool_calls": calls,
        "tool_names": tool_names,
        "tool_call_count": len(calls),
        "iterations": iterations,
        "duplicate_calls": duplicate_calls,
        "sub_calls": sub_calls,
        "sub_rounds": sub_rounds,
        "sub_tool_calls": sub_tool_calls,
        "sub_empty_handed": sub_empty,
        "sub_usage_total": sub_usage,
    }
