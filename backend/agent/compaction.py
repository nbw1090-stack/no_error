"""
claw-code（Claude Code）风格的上下文压缩（compaction）—— 纯函数工具集

为什么需要它（替代旧的「陈旧工具结果省略」渐进改写）：
- 旧机制超预算后「每轮多省一条」，省略线逐轮后挪，改写点之后的整段历史对
  DeepSeek 前缀缓存都是新前缀 → 实测 direct 模式主 Agent 缓存命中率仅 ~20%
  （命中计费是未命中的 1/10，等于放弃了 10 倍的成本优势）；
- 丢弃式占位符把证据直接抹掉，评测显示难 case 里模型"觉得还差一点"就反复
  重查，40 轮也救不回来。
- 压缩机制改为：把旧历史折叠成**确定性模板摘要**（七段式，不调 LLM，零延迟
  零成本零幻觉），分割点（upto）与摘要在两次触发之间**冻结**，发给 LLM 的
  前缀逐字节稳定 → 前缀缓存可持续命中；证据要点仍以摘要形式在场。

本模块只放**纯函数**（同输入必同输出，便于单测与复用）：
- summarize_messages：七段式模板摘要（Scope / Tools / Requests / Pending /
  Files / Current / Timeline），适配本项目的 OpenAI 消息格式；
- safe_split_point：分割点边界安全回退（tool 消息不与其 assistant 拆散）；
- merge_compact_summaries：二次压缩时旧摘要与新摘要的合并 + 预算裁剪；
- build_compaction_message：把摘要包装成发给 LLM 的 system 消息。

压缩的**决策与执行**（何时触发、状态持久化）在 context.ConversationContext。
"""

import re

# ============================================================
# 常量
# ============================================================

# 摘要各字段的截断上限（与 claw-code 一致：时间线/请求 160，当前工作 200）
_LINE_MAX_CHARS = 160
_CURRENT_WORK_MAX_CHARS = 200
# 摘要合并后的总字符上限：超过则按优先级做行级裁剪（见 _trim_summary）。
# 4000 字符 ≈ 1300 token，摘要本身若不受控，多次合并后会重新吃掉压缩收益。
SUMMARY_MAX_CHARS = 4000
# 关键文件最多保留数（宁可漏掉一些，也不要让文件清单膨胀）
_KEY_FILES_MAX = 8
# 待办条目上限：pending 在裁剪时优先级最高，若不封顶，多次合并累积的旧待办
# 会把其余段落全部挤出预算
_PENDING_MAX = 6
# 最近用户请求条数
_RECENT_REQUESTS_MAX = 3

# 待办关键词（中英都要：本项目回复以中文为主，但工具/模型输出常夹英文）
_PENDING_KEYWORDS = (
    "todo",
    "next",
    "pending",
    "follow up",
    "remaining",
    "待办",
    "下一步",
    "还需",
    "剩余",
    "后续",
)

# 关键文件后缀白名单：适配本项目（BMC 组件源码 lua/c/cpp + 后端 py + 文档/日志）
_FILE_EXTS = ("lua", "cpp", "hpp", "c", "h", "py", "json", "md", "log")
# 文件引用形态：路径（含 /）或 `xxx.lua:123` 行号引用；先整体匹配再取 path 分组
_FILE_REF_RE = re.compile(
    r"^(?P<path>[A-Za-z0-9_.\-/]+\.(?:%s))(?P<line>:\d+)?$" % "|".join(_FILE_EXTS),
    re.IGNORECASE,
)
# 文件提取的分词器：除空白外，把 JSON 引号/逗号/括号也当分隔符（这样
# tool_calls 的 arguments JSON 里的 "src/a.lua" 也能被拆出来）；中文标点
# 同样切分——中文叙述里文件名常与全角逗号/句号粘连。
_FILE_TOKEN_SPLIT_RE = re.compile(
    r"[\s\"'`,;()\[\]{}<>|=，。；：、！？（）【】《》…]+"
)

# Scope 行格式（渲染与解析共用，保证 merge 时能把旧计数累加回来）
_SCOPE_RE = re.compile(
    r"^- Scope: (\d+) earlier messages compacted "
    r"\(user=(\d+), assistant=(\d+), tool=(\d+)\)\.$"
)

# 摘要 system 消息的中文前言：告知模型这是自动摘要、原文未丢（仍在会话存档），
# 并要求直接续接——否则模型压缩后第一句常是"根据之前的摘要我了解到…"式废话。
COMPACTION_PREAMBLE = (
    "以下是本会话较早历史的自动压缩摘要（原始消息并未丢失，仍完整保存在会话"
    "存档中；摘要之后的消息为逐字保留的近期对话）。请基于摘要与近期消息直接"
    "继续当前任务，不要向用户复述或确认这份摘要。"
)


# ============================================================
# 基础工具
# ============================================================


def _one_line(text: str, max_chars: int) -> str:
    """折叠空白成单行并截断（摘要按行组织，多行内容会破坏行级裁剪/解析）。"""
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1] + "…"


def _text_content(msg: dict) -> str | None:
    """取消息的文本 content（仅接受 str；None / 非字符串一律视为无文本）。"""
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    return None


def _dedup_keep_order(items: list[str]) -> list[str]:
    """去重但保持首次出现顺序（摘要是时间性文本，排序会打乱叙事）。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ============================================================
# 七段式摘要（确定性模板，不调 LLM）
# ============================================================


def _collect_tool_names(messages: list[dict]) -> list[str]:
    """从 assistant.tool_calls[].function.name 收集去重的工具名（排序保证确定性）。"""
    names: set[str] = set()
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            name = (tc.get("function") or {}).get("name")
            if name:
                names.add(name)
    return sorted(names)


def _collect_recent_user_requests(messages: list[dict]) -> list[str]:
    """最近 ≤3 条 user 文本（时间顺序输出，每条截 160 字符）。"""
    requests: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        text = _text_content(msg)
        if text is None:
            continue
        requests.append(_one_line(text, _LINE_MAX_CHARS))
        if len(requests) >= _RECENT_REQUESTS_MAX:
            break
    return list(reversed(requests))


def _infer_pending_work(messages: list[dict]) -> list[str]:
    """
    待办推断：user/assistant 文本中含待办关键词（中英）的消息，最新的 ≤3 条。

    刻意不扫 tool 结果——工具返回的 JSON 数据里 "next"/"剩余" 等字样多为字段名
    或数据本身，误报率高；待办语义只出现在人和模型的自然语言里。
    """
    hits: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") not in ("user", "assistant"):
            continue
        text = _text_content(msg)
        if text is None:
            continue
        lowered = text.lower()
        if any(kw in lowered for kw in _PENDING_KEYWORDS):
            hits.append(_one_line(text, _LINE_MAX_CHARS))
            if len(hits) >= 3:
                break
    return list(reversed(hits))


def _extract_file_candidates(content: str) -> list[str]:
    """
    从一段文本里提取文件引用：分词 → 去尾部标点 → 后缀白名单 +（含 `/` 或
    带 `:行号`）。返回去掉行号后的路径（行号只是引用形态，聚合时按文件去重）。
    """
    found: list[str] = []
    for token in _FILE_TOKEN_SPLIT_RE.split(content):
        candidate = token.rstrip(".:")
        if not candidate:
            continue
        m = _FILE_REF_RE.match(candidate)
        if not m:
            continue
        path = m.group("path")
        # 裸文件名（无路径、无行号）证据太弱，宁可漏掉也不误报
        if "/" not in path and not m.group("line"):
            continue
        found.append(path)
    return found


def _collect_key_files(messages: list[dict]) -> list[str]:
    """聚合所有消息（含 tool_calls 参数）里的文件引用，去重后最多保留 8 个。"""
    files: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            files.extend(_extract_file_candidates(content))
        for tc in msg.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments")
            if isinstance(args, str):
                files.extend(_extract_file_candidates(args))
    return _dedup_keep_order(files)[:_KEY_FILES_MAX]


def _infer_current_work(messages: list[dict]) -> str | None:
    """当前工作 = 最后一条非空 assistant 文本（截 200 字符）。"""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        text = _text_content(msg)
        if text is not None:
            return _one_line(text, _CURRENT_WORK_MAX_CHARS)
    return None


def _summarize_one_message(msg: dict) -> str:
    """
    时间线单行渲染：`role: 内容摘要`。
    assistant 的 tool_calls 渲染为 `tool_use 名称({参数截断})`，
    tool 结果渲染为 `tool_result: 内容截断`，多个部分用 " | " 连接。
    """
    role = msg.get("role", "unknown")
    parts: list[str] = []
    text = _text_content(msg)
    if role == "tool":
        parts.append("tool_result: " + (_one_line(text or "", _LINE_MAX_CHARS) or "(空)"))
    else:
        if text is not None:
            parts.append(text)
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function") or {}
            name = func.get("name", "?")
            args = func.get("arguments") or ""
            # 参数先单独截短：一条 tool_use 不应独占整行 160 字符预算
            parts.append(f"tool_use {name}({_one_line(str(args), 80)})")
    if not parts:
        parts.append("(无内容)")
    return _one_line(f"{role}: " + " | ".join(parts), _LINE_MAX_CHARS)


def _render_summary(
    scope: tuple[int, int, int, int],
    tools: list[str],
    requests: list[str],
    pending: list[str],
    files: list[str],
    current: str | None,
    timeline: list[str],
) -> str:
    """
    七段式摘要渲染（summarize 与 merge 共用同一渲染器 → 合并结果仍是同一
    结构，可被再次解析/合并，压缩可无限组合）。
    """
    total, users, assistants, tool_n = scope
    lines = [
        "<summary>",
        "Conversation summary:",
        (
            f"- Scope: {total} earlier messages compacted "
            f"(user={users}, assistant={assistants}, tool={tool_n})."
        ),
    ]
    if tools:
        lines.append(f"- Tools mentioned: {', '.join(tools)}.")
    if requests:
        lines.append("- Recent user requests:")
        lines.extend(f"  - {r}" for r in requests)
    if pending:
        lines.append("- Pending work:")
        lines.extend(f"  - {p}" for p in pending)
    if files:
        lines.append(f"- Key files referenced: {', '.join(files)}.")
    if current:
        lines.append(f"- Current work: {current}")
    if timeline:
        lines.append("- Key timeline:")
        lines.extend(f"  - {t}" for t in timeline)
    lines.append("</summary>")
    return "\n".join(lines)


def summarize_messages(messages: list[dict]) -> str:
    """
    把一段历史消息提炼为七段式结构化摘要（纯模板填充，**不调用 LLM**）。

    这是压缩机制的核心认知：计数 + 取值 + 关键词匹配 + 截断 + 拼接即可保留
    "还差什么、在干什么、查过哪些文件"这些续接所需的信息——零延迟、零 token
    成本、零幻觉，且**确定性**（同输入必同输出，是前缀冻结的前提）。

    Args:
        messages: OpenAI 格式消息列表（{"role","content","tool_calls"} /
            role=="tool" 的 {"tool_call_id","content"}）。

    Returns:
        `<summary>...</summary>` 包裹的摘要文本。
    """
    users = sum(1 for m in messages if m.get("role") == "user")
    assistants = sum(1 for m in messages if m.get("role") == "assistant")
    tool_n = sum(1 for m in messages if m.get("role") == "tool")

    return _render_summary(
        scope=(len(messages), users, assistants, tool_n),
        tools=_collect_tool_names(messages),
        requests=_collect_recent_user_requests(messages),
        pending=_infer_pending_work(messages),
        files=_collect_key_files(messages),
        current=_infer_current_work(messages),
        timeline=[_summarize_one_message(m) for m in messages],
    )


# ============================================================
# 边界安全：分割点回退
# ============================================================


def safe_split_point(messages: list[dict], upto: int, lower: int = 0) -> int:
    """
    分割点边界安全回退（claw-code 5.2 的回退循环，适配 OpenAI 消息格式）。

    messages[upto]（压缩后保留的第一条）不能是 role=="tool"：OpenAI/DeepSeek
    要求 tool 消息必须紧跟带 tool_calls 的 assistant，拆散配对会直接 400。
    连续 tool（一条 assistant 并行发起多个 tool_calls）逐条回退，直到落在
    非 tool 消息上——即对应的 assistant(tool_calls) 也一并保留。

    Args:
        messages: 完整消息列表
        upto: 期望的分割点（[0, upto) 压缩、[upto:] 保留）
        lower: 回退下界（二次压缩时为旧 upto，不能回退进已压缩区）

    Returns:
        安全的分割点，∈ [lower, len(messages))；退无可退时返回 lower
        （调用方应据此判定"本次无法压缩"）。
    """
    upto = min(upto, len(messages))
    while upto > lower and upto < len(messages) and messages[upto].get("role") == "tool":
        upto -= 1
    return max(upto, lower)


# ============================================================
# 二次压缩：摘要解析、合并与预算裁剪
# ============================================================


def _parse_summary(summary: str) -> dict:
    """
    把七段式摘要解析回结构化字段（merge 的逆向操作）。

    渲染与解析共用同一套行格式，因此合并结果可被再次解析——压缩次数不限。
    无法识别的行（如裁剪产生的省略提示）安全忽略。
    """
    data: dict = {
        "scope": (0, 0, 0, 0),
        "tools": [],
        "requests": [],
        "pending": [],
        "files": [],
        "current": None,
        "timeline": [],
    }
    current_list: list[str] | None = None
    for line in summary.splitlines():
        if line in ("<summary>", "Conversation summary:", "</summary>"):
            current_list = None
            continue
        m = _SCOPE_RE.match(line)
        if m:
            data["scope"] = tuple(int(x) for x in m.groups())
            current_list = None
            continue
        if line.startswith("- Tools mentioned: "):
            raw = line[len("- Tools mentioned: "):].rstrip(".")
            data["tools"] = [t for t in raw.split(", ") if t]
            current_list = None
            continue
        if line == "- Recent user requests:":
            current_list = data["requests"]
            continue
        if line == "- Pending work:":
            current_list = data["pending"]
            continue
        if line.startswith("- Key files referenced: "):
            raw = line[len("- Key files referenced: "):].rstrip(".")
            data["files"] = [f for f in raw.split(", ") if f]
            current_list = None
            continue
        if line.startswith("- Current work: "):
            data["current"] = line[len("- Current work: "):]
            current_list = None
            continue
        if line == "- Key timeline:":
            current_list = data["timeline"]
            continue
        if line.startswith("  - ") and current_list is not None:
            current_list.append(line[4:])
            continue
        current_list = None
    return data


def _line_priorities(lines: list[str]) -> list[int]:
    """
    给摘要每一行标注裁剪优先级（数字越小越先保留）。

    0 = 结构行 + Scope + Tools（极小且是摘要骨架，必留）
    1 = Pending work（丢了待办，模型会忘记"还差什么"→ 反复重查）
    2 = Current work
    3 = Key files
    4 = Recent user requests
    5 = Key timeline / 其它（体积最大、可再生性最强，最先牺牲）
    """
    section_prio = {
        "- Pending work:": 1,
        "- Recent user requests:": 4,
        "- Key timeline:": 5,
    }
    priorities: list[int] = []
    current = 5  # 未识别的悬挂行按最低优先级
    for line in lines:
        if line in ("<summary>", "Conversation summary:", "</summary>"):
            priorities.append(0)
            current = 5
        elif line.startswith("- Scope:") or line.startswith("- Tools mentioned:"):
            priorities.append(0)
            current = 5
        elif line in section_prio:
            current = section_prio[line]
            priorities.append(current)
        elif line.startswith("- Current work:"):
            priorities.append(2)
            current = 5
        elif line.startswith("- Key files referenced:"):
            priorities.append(3)
            current = 5
        elif line.startswith("  - "):
            priorities.append(current)  # 子项跟随所属段落的优先级
        else:
            priorities.append(5)
            current = 5
    return priorities


def _trim_summary(summary: str, max_chars: int) -> str:
    """
    预算裁剪（SummaryCompressionBudget 的优先级选择思想）：摘要超过 max_chars
    时按行级优先级贪心保留——pending > current > files > requests > timeline，
    timeline 内部从**最新**条目开始选（越新越可能与当前任务相关）。

    被省略的行数以一条提示行标注，避免模型误以为时间线是完整的。
    """
    if len(summary) <= max_chars:
        return summary

    lines = summary.splitlines()
    priorities = _line_priorities(lines)
    # 给省略提示行预留空间（否则选满预算后加提示又超了）
    budget = max_chars - 40

    selected: set[int] = set()
    total = 0
    for prio in range(6):
        idxs = [i for i, p in enumerate(priorities) if p == prio]
        if prio == 5:
            idxs.reverse()  # timeline：新条目优先入选
        for i in idxs:
            cost = len(lines[i]) + 1  # +1 换行符
            if total + cost > budget:
                continue  # 该行放不下，继续尝试更短的行（贪心装箱）
            selected.add(i)
            total += cost

    omitted = len(lines) - len(selected)
    out = [lines[i] for i in sorted(selected)]
    if omitted > 0:
        notice = f"- …（{omitted} 行已按预算省略）"
        # 省略提示放在 </summary> 之前，保持结构闭合
        if out and out[-1] == "</summary>":
            out.insert(len(out) - 1, notice)
        else:
            out.append(notice)
    return "\n".join(out)


def merge_compact_summaries(
    existing_summary: str | None,
    new_summary: str,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str:
    """
    二次压缩的摘要合并：把旧摘要与新增区间的摘要合并成**同一七段结构**。

    合并策略（与 claw-code 同思路，结构上更进一步——保持可再解析）：
    - Scope 计数累加（压缩总规模不失真）；
    - Tools 取并集（用过什么能力是高频引用信息）；
    - Pending / Key files 新旧合并去重（丢待办/丢文件线索的代价最高）；
    - Recent requests / Current work / Timeline 只保留新的——时间线是逐条
      消息级的，多次合并必然膨胀，旧时间线的信息价值早已被消化进结论。

    合并结果超过 max_chars 时做行级优先级裁剪（见 _trim_summary）。
    """
    if existing_summary is None:
        return _trim_summary(new_summary, max_chars)

    old = _parse_summary(existing_summary)
    new = _parse_summary(new_summary)

    scope = tuple(a + b for a, b in zip(old["scope"], new["scope"]))
    tools = sorted(set(old["tools"]) | set(new["tools"]))
    pending = _dedup_keep_order(old["pending"] + new["pending"])[:_PENDING_MAX]
    files = _dedup_keep_order(old["files"] + new["files"])[:_KEY_FILES_MAX]
    requests = new["requests"]
    current = new["current"] or old["current"]
    timeline = new["timeline"]

    merged = _render_summary(
        scope, tools, requests, pending, files, current, timeline
    )
    return _trim_summary(merged, max_chars)


# ============================================================
# 摘要消息包装
# ============================================================


def build_compaction_message(summary: str) -> dict:
    """把压缩摘要包装为发给 LLM 的 system 消息（中文前言 + <summary> 正文）。"""
    return {"role": "system", "content": COMPACTION_PREAMBLE + "\n\n" + summary}
