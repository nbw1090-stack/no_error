"""
源码检索工具

get_function_source：根据日志条目的 (component, file, line)，从该用户已分析的
源码快照中切出对应的 Lua/C 函数体（含可选上下文行），供 agent 诊断错误根因。

依赖：
- ast_analysis.db.find_function_at_line：按 (user_id, component, basename, line) 定位符号
- ast_analysis.db.SOURCE_DIR：源码快照根目录（<user_id>/<component>/<rel_path>）
- agent.dataset.LogDataset.user_id：由 /api/chat 注入的登录用户 id

工具签名约定（与 log_tools 一致）：第一个位置参数固定为 dataset。
"""

import os
import re

from agent.tools.registry import ToolRegistry
from agent.dataset import LogDataset


# ============================================================
# 组件概览(summarize_component)用的入口符号启发式
# ============================================================
# 入口符号启发式:匹配「方法名片段」。Lua 形如 mod:method / mod.method,
# 故先用 _entry_symbol_token 取最后一段再匹配。覆盖 init/new/register/on_*/init_*/*_init。
_ENTRY_SYMBOL_RE = re.compile(
    r"^(init|main|start|new|create|open|setup|register|initialize"
    r"|on_\w+|init_\w+|\w+_init)$",
    re.IGNORECASE,
)
# 源码优先目录前缀:test/gen 多为噪声。无 src//lib/ 时回退到非噪声目录。
_SOURCE_DIR_PREFIXES = ("src/", "lib/")
_NOISE_DIR_PREFIXES = ("test/", "tests/", "gen/", "vendor/", "build/")


def _entry_symbol_token(name: str) -> str:
    """取 Lua mod:method / mod.method 的最后一段方法名;无点冒号则原样返回。"""
    if not name:
        return ""
    return re.split(r"[.:]", name)[-1]


@ToolRegistry.register(
    name="get_function_source",
    description=(
        "根据日志条目的 component/file/line，取出对应 Lua/C 源码的函数体"
        "（含可选上下文行）。当日志含 file:line 锚点、需要查看出错位置的真实"
        "源码以诊断根因时调用。需先在「组件管理」对相关组件构建源码索引。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {
                "type": "string",
                "description": "组件名称（日志条目的 component 字段）",
            },
            "file": {
                "type": "string",
                "description": (
                    "源码文件名（日志条目的 file 字段，通常是裸文件名，"
                    "如 pcie_card.lua）"
                ),
            },
            "line": {
                "type": "integer",
                "description": "出错行号（日志条目的 line 字段）",
            },
            "context_lines": {
                "type": "integer",
                "description": "函数体前后额外取的上下文行数，默认 5",
            },
        },
        "required": ["component", "file", "line"],
    },
    group="source",
)
async def get_function_source(
    dataset: LogDataset,
    component: str,
    file: str,
    line: int,
    context_lines: int = 5,
) -> dict:
    """取出出错位置对应的函数源码体。"""
    from ast_analysis import db

    user_id = dataset.user_id
    if user_id is None:
        return {"error": "源码索引需登录后可用"}

    file_basename = os.path.basename(file or "")
    candidates = db.find_function_at_line(user_id, component, file_basename, line)

    if not candidates:
        # 区分「组件未索引」与「文件未找到」，给 agent 可操作的提示
        analyzed = {c["component"] for c in db.list_user_components(user_id)}
        if component not in analyzed:
            return {
                "error": "该组件尚未构建源码索引",
                "component": component,
                "hint": "请在「组件管理」页面为该组件构建 AST 源码索引后重试",
            }
        return {
            "error": "未找到匹配的源码文件",
            "component": component,
            "file": file,
            "line": line,
            "hint": (
                f"组件 {component} 已索引，但未找到文件 {file_basename}，"
                "或该行不在任何已识别函数内"
            ),
        }

    # 取首个候选（line 已精确落入其函数区间）；其余作为备选附上
    primary = candidates[0]
    source, ctx_start, ctx_end = _read_source_slice(
        user_id,
        component,
        primary["rel_path"],
        primary["start_line"],
        primary["end_line"],
        context_lines,
    )

    result = {
        "component": component,
        "file": file,
        "rel_path": primary["rel_path"],
        "function": primary["name"],
        "kind": primary["kind"],
        "start_line": primary["start_line"],
        "end_line": primary["end_line"],
        "context_start_line": ctx_start,
        "context_end_line": ctx_end,
        "source": source,
        "candidates_count": len(candidates),
    }
    if len(candidates) > 1:
        result["alternatives"] = [
            {
                "rel_path": c["rel_path"],
                "function": c["name"],
                "start_line": c["start_line"],
                "end_line": c["end_line"],
            }
            for c in candidates[1:]
        ]
    return result


@ToolRegistry.register(
    name="search_symbols",
    description=(
        "按函数/符号名称检索某组件的源码，返回匹配的函数体源码（带行号）。"
        "当用户按名称询问某个函数/方法/类的行为时（如 'init_card 是做什么的'）"
        "调用，无需提供行号。需先在「组件管理」对相关组件构建源码索引。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {"type": "string", "description": "组件名称"},
            "name": {
                "type": "string",
                "description": "符号名称或其片段（大小写不敏感，子串匹配）",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回的匹配数，默认 5",
            },
            "context_lines": {
                "type": "integer",
                "description": "每个函数体前后额外取的上下文行数，默认 3",
            },
        },
        "required": ["component", "name"],
    },
    group="source",
)
async def search_symbols(
    dataset: LogDataset,
    component: str,
    name: str,
    limit: int = 5,
    context_lines: int = 3,
) -> dict:
    """按符号名检索源码函数体。"""
    from ast_analysis import db

    user_id = dataset.user_id
    if user_id is None:
        return {"error": "源码索引需登录后可用"}

    matches = db.find_symbols_by_name(
        user_id, component, name, limit=max(1, limit)
    )
    if not matches:
        return _not_found_payload(
            user_id, component, name=name,
        )

    results = []
    for m in matches:
        source, ctx_start, ctx_end = _read_source_slice(
            user_id,
            component,
            m["rel_path"],
            m["start_line"],
            m["end_line"],
            context_lines,
        )
        results.append(
            {
                "rel_path": m["rel_path"],
                "function": m["name"],
                "kind": m["kind"],
                "start_line": m["start_line"],
                "end_line": m["end_line"],
                "context_start_line": ctx_start,
                "context_end_line": ctx_end,
                "source": source,
            }
        )

    return {
        "component": component,
        "query": name,
        "matches_count": len(results),
        "results": results,
    }


@ToolRegistry.register(
    name="list_source_files",
    description=(
        "列出某组件下所有已索引的源码文件及其符号清单（函数/类名 + 行号区间）。"
        "当需要先了解组件代码结构、再决定查看哪个文件或符号时调用。"
        "需先在「组件管理」对相关组件构建源码索引。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {"type": "string", "description": "组件名称"},
        },
        "required": ["component"],
    },
    group="source",
)
async def list_source_files(dataset: LogDataset, component: str) -> dict:
    """列出组件源码文件结构与符号清单。"""
    from ast_analysis import db

    user_id = dataset.user_id
    if user_id is None:
        return {"error": "源码索引需登录后可用"}

    files = db.list_component_files(user_id, component)
    if not files:
        return _not_found_payload(user_id, component)

    return {
        "component": component,
        "files_count": len(files),
        "files": files,
    }


@ToolRegistry.register(
    name="summarize_component",
    description=(
        "给出某组件源码的「概览」:文件/符号总数、按顶层目录分组的文件数、"
        "候选入口符号(init/new/register/on_*/initialize 等,集中体现组件职责)"
        "以及符号最多的大文件。当用户问「这个组件是做什么的 / 结构 / 职责」"
        "等开放式问题时,应**优先调用本工具**拿到全貌,再决定深入哪个符号。"
        "有界小载荷,不含全部源码。需先在「组件管理」构建源码索引。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {"type": "string", "description": "组件名称"},
            "max_entry_symbols": {
                "type": "integer",
                "description": "最多返回候选入口符号数,默认 15",
            },
            "max_sample_files": {
                "type": "integer",
                "description": "最多返回大文件样本数,默认 8",
            },
        },
        "required": ["component"],
    },
    group="source",
)
async def summarize_component(
    dataset: LogDataset,
    component: str,
    max_entry_symbols: int = 15,
    max_sample_files: int = 8,
) -> dict:
    """组件源码概览(有界小载荷):总数 + 目录分组 + 候选入口符号 + 大文件样本。"""
    from ast_analysis import db

    user_id = dataset.user_id
    if user_id is None:
        return {"error": "源码索引需登录后可用"}

    files = db.list_component_files(user_id, component)
    if not files:
        return _not_found_payload(user_id, component)

    # 1) 总数 + 顶层目录分组(全部文件,含 test/gen)+ 语言
    dir_counts: dict[str, int] = {}
    lang_counts: dict[str, int] = {}
    for f in files:
        rel = f["rel_path"]
        top = rel.split("/")[0] if "/" in rel else "<root>"
        dir_counts[top] = dir_counts.get(top, 0) + 1
        lang_counts[f["language"]] = lang_counts.get(f["language"], 0) + 1

    # 2) 候选入口符号:仅源码目录(无 src//lib/ 时回退非噪声目录)+ 方法名命中启发式
    src_files = [
        f for f in files if f["rel_path"].startswith(_SOURCE_DIR_PREFIXES)
    ] or [
        f for f in files if not f["rel_path"].startswith(_NOISE_DIR_PREFIXES)
    ]
    cap_e = max(1, max_entry_symbols)
    entry: list[dict] = []
    for f in src_files:
        for s in f["symbols"]:
            nm = s.get("name", "") or ""
            if nm and _ENTRY_SYMBOL_RE.match(_entry_symbol_token(nm)):
                entry.append(
                    {
                        "name": nm,
                        "rel_path": f["rel_path"],
                        "kind": s.get("kind", ""),
                        "start_line": s["start_line"],
                        "end_line": s["end_line"],
                    }
                )
                if len(entry) >= cap_e:
                    break
        if len(entry) >= cap_e:
            break

    # 3) 大文件样本(源码目录,按 symbol_count 降序)
    cap_s = max(1, max_sample_files)
    largest = [
        {
            "rel_path": f["rel_path"],
            "language": f["language"],
            "symbol_count": f["symbol_count"],
        }
        for f in sorted(src_files, key=lambda x: x["symbol_count"], reverse=True)[
            :cap_s
        ]
    ]

    return {
        "component": component,
        "totals": {
            "file_count": len(files),
            "symbol_count": sum(f["symbol_count"] for f in files),
        },
        "by_language": lang_counts,
        "by_top_directory": dict(
            sorted(dir_counts.items(), key=lambda kv: kv[1], reverse=True)
        ),
        "entry_symbols": entry,
        "entry_symbols_truncated": len(entry) >= cap_e,
        "largest_source_files": largest,
        "guidance": (
            "以上是组件源码概览。entry_symbols 是按命名启发式挑出的候选入口符号"
            "(集中体现组件职责)。请据此选择具体符号,再用 gather_code_context 或 "
            "search_symbols 深入,并在最终回答中引用 file:line。"
        ),
    }


@ToolRegistry.register(
    name="get_file_source",
    description=(
        "按文件名取出某组件整个源码文件的内容（带行号）。"
        "当用户指名要查看某个文件，或需要阅读完整文件上下文时调用。"
        "超长文件会被截断并提示。需先在「组件管理」对相关组件构建源码索引。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {"type": "string", "description": "组件名称"},
            "file": {
                "type": "string",
                "description": "源码文件名或相对路径（如 pcie_card.lua 或 src/foo.c）",
            },
            "max_lines": {
                "type": "integer",
                "description": "从起始行起最多返回的行数，超过则截断，默认 200",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（1-based）；用于翻页读取长文件，"
                "缺省或 <=0 表示从第 1 行开始。配合上次返回的 hint 中的 offset 续读。",
            },
        },
        "required": ["component", "file"],
    },
    group="source",
)
async def get_file_source(
    dataset: LogDataset,
    component: str,
    file: str,
    max_lines: int = 200,
    offset: int = 0,
) -> dict:
    """按文件名取整个源码文件（支持 offset 翻页）。"""
    from ast_analysis import db

    user_id = dataset.user_id
    if user_id is None:
        return {"error": "源码索引需登录后可用"}

    files = db.list_component_files(user_id, component)
    if not files:
        return _not_found_payload(user_id, component)

    target = _match_source_file(files, file)
    # 文件名歧义（多个同名文件）：返回候选让 LLM 用完整相对路径重试，
    # 而非按列表顺序闷头返回某一个（旧实现会因字母序误返回错文件）。
    if isinstance(target, list):
        return {
            "error": "文件名不唯一，存在多个同名文件，请用完整相对路径重新调用",
            "component": component,
            "file": file,
            "candidates": target,
        }
    if target is None:
        return {
            "error": "未找到匹配的源码文件",
            "component": component,
            "file": file,
            "hint": f"组件 {component} 已索引，但未找到文件 {file or ''}",
        }

    cap = max(1, max_lines)
    start_off = offset if offset and offset > 0 else 0
    source, total, start_line, shown = _read_file_source(
        user_id, component, target, cap, start_off
    )
    end_line = start_line + shown - 1 if shown else 0
    has_more = shown > 0 and end_line < total  # 窗口之后仍有内容
    result = {
        "component": component,
        "rel_path": target,
        "total_lines": total,
        "start_line": start_line,
        "end_line": end_line,
        "shown_lines": shown,
        "truncated": has_more,
        "source": source,
    }
    if shown == 0 and total > 0:
        result["hint"] = (
            f"offset={start_off} 超出文件范围（共 {total} 行），未返回内容。"
            f"请用 1~{total} 之间的 offset 重试。"
        )
    elif has_more:
        result["hint"] = (
            f"文件共 {total} 行，已展示 {start_line}-{end_line} 行。"
            f"如需后续内容，用 offset={end_line + 1} 续读；"
            "或用 get_function_source / search_symbols 精确定位某符号。"
        )
    return result


def _match_source_file(files: list[dict], file: str):
    """
    把请求的 file 解析成唯一 rel_path。

    匹配优先级（修复点）：
    1. 全表先找**精确 rel_path**——不让 basename 命中抢先（旧实现把精确与
       basename 放在同一轮 OR 里，配合 ORDER BY rel_path 会让字母序靠前的同名
       文件覆盖真正想要的精确路径）。
    2. 无精确命中再按 basename 退化；唯一命中直接用，多个同名文件返回候选列表。

    Returns:
        命中的 rel_path（str）/ 候选列表（list，歧义）/ None（无匹配）。
    """
    for f in files:
        if f["rel_path"] == file:
            return f["rel_path"]

    basename_input = os.path.basename(file or "")
    matches = [
        f["rel_path"]
        for f in files
        if os.path.basename(f["rel_path"]) == basename_input
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches
    return None


def _not_found_payload(
    user_id: int, component: str, name: str | None = None
) -> dict:
    """
    统一构造「未命中」返回：区分组件未索引 vs 组件已索引但无匹配。

    复用 db.list_user_components 判定组件是否已索引，给 agent 可操作提示。
    """
    from ast_analysis import db

    analyzed = {c["component"] for c in db.list_user_components(user_id)}
    if component not in analyzed:
        return {
            "error": "该组件尚未构建源码索引",
            "component": component,
            "hint": "请在「组件管理」页面为该组件构建 AST 源码索引后重试",
        }
    if name is not None:
        return {
            "error": "未找到匹配的符号",
            "component": component,
            "name": name,
            "hint": f"组件 {component} 已索引，但未找到名称含 '{name}' 的符号",
        }
    return {
        "component": component,
        "files": [],
        "hint": f"组件 {component} 已索引，但未发现任何源码文件",
    }


# 单个函数体切片最多渲染的行数：超长函数（数百行）若整体塞入会被 ReAct 反复重发，
# 这里截断中段、保留首尾，既给出函数轮廓又挡住 token 膨胀（需细看可用 offset 翻页读）。
_MAX_SLICE_LINES = 160


def _read_source_slice(
    user_id: int,
    component: str,
    rel_path: str,
    start_line: int,
    end_line: int,
    context_lines: int,
) -> tuple[str, int, int]:
    """
    从源码快照读出函数体（含上下文），带行号渲染。

    超长函数体（> _MAX_SLICE_LINES）会保留首尾、折叠中段，避免单条结果过大。

    Returns:
        (渲染后的源码, 实际起始行, 实际结束行)。文件读不到时源码为空串。
    """
    from ast_analysis.service import _component_source_dir

    path = os.path.join(_component_source_dir(user_id, component), rel_path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ("", start_line, end_line)

    ctx = max(0, context_lines)
    lo = max(0, start_line - 1 - ctx)
    hi = min(len(lines), end_line + ctx)
    slice_lines = lines[lo:hi]

    def _render(seg: list[str], base: int) -> list[str]:
        return [f"{(base + i + 1):>5} | {s}" for i, s in enumerate(seg)]

    # 超长则折叠中段，保留首尾各半，给出函数轮廓而非全文
    if len(slice_lines) > _MAX_SLICE_LINES:
        head = _MAX_SLICE_LINES // 2
        tail = _MAX_SLICE_LINES - head
        omitted = len(slice_lines) - head - tail
        rendered = "\n".join(
            _render(slice_lines[:head], lo)
            + [f"      | … 省略中段 {omitted} 行（如需细看用 get_file_source 翻页）…"]
            + _render(slice_lines[-tail:], lo + len(slice_lines) - tail)
        )
    else:
        rendered = "\n".join(_render(slice_lines, lo))
    return (rendered, lo + 1, lo + len(slice_lines))


def _read_file_source(
    user_id: int, component: str, rel_path: str, max_lines: int, offset: int = 0
) -> tuple[str, int, int, int]:
    """
    读取源码文件的一个窗口（带**真实行号**），支持从 offset 起翻页。

    Args:
        max_lines: 从起始行起最多返回的行数。
        offset: 起始行号（1-based）；<=0 表示从第 1 行开始。

    Returns:
        (带行号的源码, 文件总行数, 窗口起始行号(1-based), 实际返回行数)。
        文件读不到时返回 ("", 0, 0, 0)。
    """
    from ast_analysis.service import _component_source_dir

    path = os.path.join(_component_source_dir(user_id, component), rel_path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ("", 0, 0, 0)

    total = len(lines)
    start = offset - 1 if offset and offset > 0 else 0
    start = max(0, min(start, total))  # 越界则收敛到文件尾（窗口为空）
    window = lines[start:start + max_lines]
    rendered = "\n".join(
        f"{(start + i + 1):>5} | {s}" for i, s in enumerate(window)
    )
    return (rendered, total, start + 1, len(window))


@ToolRegistry.register(
    name="gather_code_context",
    description=(
        "一次性汇聚某组件中目标符号的完整代码上下文：目标函数体源码 + 同文件"
        "兄弟符号 + 跨文件调用点。当需要基于源码定位错误根因、理解某函数行为"
        "时，应**优先调用本工具**（而非先凭日志猜测或直接汇总答案），拿到代码"
        "证据后再做根因分析与总结。锚点二选一：name（按符号名检索）或 file+line"
        "（按日志行号反查，file+line 更精确优先）。返回单一聚合结果，减少多轮"
        "工具调用。需先在「组件管理」对相关组件构建源码索引。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {
                "type": "string",
                "description": "组件名称（日志条目的 component 字段）",
            },
            "name": {
                "type": "string",
                "description": (
                    "目标符号名（按名称检索锚点；与 file/line 二选一，"
                    "同时给出时以 file+line 为准）"
                ),
            },
            "file": {
                "type": "string",
                "description": "源码文件名或相对路径（与 line 配合作为行号锚点）",
            },
            "line": {
                "type": "integer",
                "description": "出错行号（与 file 配合作为行号锚点）",
            },
            "context_lines": {
                "type": "integer",
                "description": "函数体前后额外取的上下文行数，默认 5",
            },
            "max_usages": {
                "type": "integer",
                "description": "最多返回的跨文件调用点数，默认 6",
            },
        },
        "required": ["component"],
    },
    group="source",
)
async def gather_code_context(
    dataset: LogDataset,
    component: str,
    name: str | None = None,
    file: str | None = None,
    line: int | None = None,
    context_lines: int = 5,
    max_usages: int = 6,
) -> dict:
    """
    汇聚目标符号的完整代码上下文（函数体 + 兄弟符号 + 跨文件调用点）。

    高层聚合入口：用一次调用替代「search → get_source → 手动找调用方」多轮。
    锚点解析优先级：file+line（精确）> name。两者皆空时返回结构化错误。
    """
    from ast_analysis import db
    from ast_analysis.service import _component_source_dir

    user_id = dataset.user_id
    if user_id is None:
        return {"error": "源码索引需登录后可用"}

    # --- 锚点解析（file+line 与 name 二选一；JSON Schema 无法表达，故在体内校验）---
    file_line_anchor = bool(file) and line is not None
    name_anchor = bool(name)

    if not file_line_anchor and not name_anchor:
        return {
            "error": "缺少锚点：需提供 name，或 file+line",
            "component": component,
            "hint": "至少提供一个锚点（符号名 或 文件名+行号）",
        }

    if file_line_anchor:
        # file+line 更精确（日志 ground truth），优先于 name
        file_basename = os.path.basename(file or "")
        candidates = db.find_function_at_line(
            user_id, component, file_basename, line
        )
        anchor_desc = {"type": "file_line", "file": file, "line": line}
    else:
        # name 锚点：取首个匹配作为上下文中心（多匹配场景由 search_symbols 负责）
        candidates = db.find_symbols_by_name(user_id, component, name, limit=1)
        anchor_desc = {"type": "name", "name": name}

    if not candidates:
        # 复用统一「未命中」载荷：区分组件未索引 vs 符号未找到
        return _not_found_payload(user_id, component, name=name)

    primary = candidates[0]
    rel_path = primary["rel_path"]

    # 1) 目标函数体（含上下文）
    source, ctx_start, ctx_end = _read_source_slice(
        user_id,
        component,
        rel_path,
        primary["start_line"],
        primary["end_line"],
        context_lines,
    )

    # 2) 同文件兄弟符号（排除目标自身，便于了解文件结构）
    sibling_symbols: list[dict] = []
    file_info = db.get_file_symbols(user_id, component, rel_path)
    if file_info:
        sibling_symbols = [
            {
                "name": s["name"],
                "kind": s["kind"],
                "start_line": s["start_line"],
                "end_line": s["end_line"],
            }
            for s in file_info["symbols"]
            if s["name"] != primary["name"]
        ]

    # 3) 跨文件调用点（有界文本扫描；root 已由 _component_source_dir 校验路径安全）
    root = _component_source_dir(user_id, component)
    related_usages = _find_usages(
        root, primary["name"], rel_path, max(1, max_usages)
    )

    return {
        "component": component,
        "anchor": anchor_desc,
        "target": {
            "rel_path": rel_path,
            "function": primary["name"],
            "kind": primary["kind"],
            "start_line": primary["start_line"],
            "end_line": primary["end_line"],
            "context_start_line": ctx_start,
            "context_end_line": ctx_end,
            "source": source,
        },
        "sibling_symbols": sibling_symbols,
        "related_usages": related_usages,
        "guidance": (
            "已汇聚目标符号的函数体、同文件兄弟符号与跨文件调用点。"
            "请先基于以上源码完成代码定位与根因分析，再给出最终汇总，"
            "并引用具体的 file:line 证据。"
        ),
    }


def _find_usages(
    root: str,
    name: str,
    target_rel_path: str,
    max_usages: int,
    per_file_cap: int = 3,
) -> list[dict]:
    """
    在源码快照根下有界扫描符号名的调用点，返回带行号的片段。

    - 仅扫描 analyzer 支持的扩展名（天然跳过二进制/文档/配置）。
    - 跳过 analyzer._SKIP_DIRS 与隐藏目录；跳过 > analyzer._MAX_FILE_BYTES。
    - 行号渲染格式与 _read_source_slice 一致（"{n:>5} | {line}"）。
    - 大小写敏感（c/cpp/lua 函数名均区分大小写，避免 init vs INIT 误命中）。
    - 定义文件命中统一计入（不特殊标记），受 per_file_cap 约束。

    Returns:
        [{rel_path, line, snippet}, ...]，总量不超过 max_usages。
    """
    import re

    from ast_analysis import analyzer

    if not name:
        return []

    pattern = re.compile(rf"(?<![\w$])({re.escape(name)})(?![\w$])")
    results: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # 原地剪枝跳过目录 / 隐藏目录（镜像 analyzer.analyze_directory）
        dirnames[:] = [
            d
            for d in dirnames
            if d not in analyzer._SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in analyzer._EXT_TO_LANG:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(fpath) > analyzer._MAX_FILE_BYTES:
                    continue
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue

            rel = os.path.relpath(fpath, root).replace(os.sep, "/")
            file_hits = 0
            for i, raw in enumerate(lines, start=1):
                if pattern.search(raw):
                    results.append(
                        {
                            "rel_path": rel,
                            "line": i,
                            "snippet": f"{i:>5} | {raw.rstrip()}",
                        }
                    )
                    file_hits += 1
                    if len(results) >= max_usages:
                        return results
                    if file_hits >= per_file_cap:
                        break
    return results


# ============================================================
# 多跳导航（trace_call_chain）+ 跨组件接口解析（resolve_interface）
#
# 设计原则:把「导航」(廉价:名字 + file:line + 每跳一行调用语句,无函数体) 与
# 「细看」(昂贵:单跳函数体,用 gather_code_context 懒加载) 拆开。多跳工具只吐骨架,
# 避免把 N 个函数体塞进一个工具结果——那会被 ReAct 每轮全量重发(二次方膨胀),且因
# 它恰是「最近一条」躲不过 context.py 的旧结果省略。骨架由构造即小(~KB 级)。
# ============================================================

# 服务端硬上限:挡住 LLM 传入过大的 depth/breadth 撑爆骨架。
_TRACE_MAX_DEPTH = 4
_TRACE_MAX_BREADTH = 5
# unresolved（解析不到本组件符号、又非接口）的 callee 名只名字-only 收集,封顶。
_TRACE_MAX_UNRESOLVED = 12

# 调用点正则:取「紧贴左括号的标识符」。对 foo()/obj.method()/obj:method() 均命中
# 被调用的那个名字(method/foo),因为 mdb 形如 mdb.try_get_object( 中 . 后的
# try_get_object 才贴着 (，mdb 贴着 . 不会误命中。
_CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")

# 控制流 / 语言关键字停用词:它们后面也跟 (，但不是函数调用。
_CALL_KEYWORDS = frozenset(
    {
        # Lua
        "if", "elseif", "while", "for", "return", "function", "and", "or",
        "not", "do", "then", "end", "until", "repeat", "in", "local",
        # C / C++
        "switch", "sizeof", "catch", "defined", "static_cast", "dynamic_cast",
        "reinterpret_cast", "const_cast", "decltype", "typeof", "alignof",
        "else", "case", "goto",
    }
)

# mdb/dbus 接口字面串:openUBMC 跨组件通信的「边」本质是这种字符串常量。
_INTERFACE_RE = re.compile(r"bmc\.(?:dev|kepler)\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")

# 接口命中的角色启发式(同行邻近 token,小写后子串判定)。provide 先判:注册语义更具体。
_PROVIDE_SIGNALS = (
    "register", "add_object", "addobject", "new_object", "create_object",
    "add_interface", "object_define", ":implement", "implement(", "model.json",
)
_CONSUME_SIGNALS = (
    "make_proxy", "try_get_object", "get_object_list", "get_object",
    "getobject", "prop_table[", "proxy", "subscribe", "lookup",
)


def _read_raw_lines(user_id: int, component: str, rel_path: str) -> list[str]:
    """读源码快照文件的原始行(不渲染、不折叠)。读不到返回空列表。"""
    from ast_analysis.service import _component_source_dir

    path = os.path.join(_component_source_dir(user_id, component), rel_path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _detect_interface(line: str) -> str | None:
    """从一行里抽出 bmc.dev.* / bmc.kepler.* 接口字面串(取首个)。"""
    m = _INTERFACE_RE.search(line)
    return m.group(0).rstrip(".") if m else None


def _classify_interface_hit(line: str) -> str | None:
    """按同行邻近 token 把接口命中分类为 provide / consume；都不像则 None。"""
    low = line.lower()
    if any(sig in low for sig in _PROVIDE_SIGNALS):
        return "provide"
    if any(sig in low for sig in _CONSUME_SIGNALS):
        return "consume"
    return None


def _extract_callees(
    body_lines: list[tuple[int, str]], self_name: str
) -> list[tuple[str, int, str]]:
    """
    从函数体行里抽 callee。

    Args:
        body_lines: [(行号, 原始行文本), ...]
        self_name: 锚点符号自身名(及其方法尾段),用于跳过定义/自递归噪声。

    Returns:
        [(callee 名, 调用行号, 调用行文本), ...]，按出现顺序、同名去重。
    """
    self_token = _entry_symbol_token(self_name)
    seen: set[str] = set()
    out: list[tuple[str, int, str]] = []
    for lineno, raw in body_lines:
        for m in _CALL_RE.finditer(raw):
            name = m.group(1)
            if name in _CALL_KEYWORDS:
                continue
            if name == self_name or name == self_token:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append((name, lineno, raw))
    return out


def _resolve_callee(user_id: int, component: str, callee: str) -> dict | None:
    """
    把 callee 名反查回本组件的符号定义(精确名优先,其次 Lua 方法尾段匹配)。

    find_symbols_by_name 是子串匹配,这里收紧为精确命中,避免 init 命中 init_card。

    Returns:
        {rel_path, name, kind, start_line, end_line} 或 None。
    """
    from ast_analysis import db

    cands = db.find_symbols_by_name(user_id, component, callee, limit=20)
    if not cands:
        return None
    # 精确名优先
    for c in cands:
        if c["name"] == callee:
            return c
    # 退化:Lua mod:method / mod.method 的尾段等于 callee
    for c in cands:
        if _entry_symbol_token(c["name"]) == callee:
            return c
    return None


def _resolve_anchor(
    user_id: int,
    component: str,
    name: str | None,
    file: str | None,
    line: int | None,
) -> dict | None:
    """解析 trace 锚点:file+line(精确,优先) > name。返回符号 dict 或 None。"""
    from ast_analysis import db

    if file and line is not None:
        cands = db.find_function_at_line(
            user_id, component, os.path.basename(file or ""), line
        )
        return cands[0] if cands else None
    if name:
        cands = db.find_symbols_by_name(user_id, component, name, limit=1)
        return cands[0] if cands else None
    return None


@ToolRegistry.register(
    name="trace_call_chain",
    description=(
        "多跳调用链「导航骨架」:从锚点符号出发,逐跳列出它调用了哪些函数/方法"
        "(函数名 + file:line + 每跳那一行调用语句),**不含函数体**,载荷很小。"
        "当根因可能在调用链更深处(跨多层函数)、需要先看清链路再决定深入哪一跳时,"
        "**先调本工具**;读完骨架后,用 gather_code_context 只拉你挑中的那一个函数体。"
        "命中的 mdb 接口(bmc.dev.*/bmc.kepler.*)会列在 interfaces_seen,可交给 "
        "resolve_interface 定位 provider 组件。锚点二选一:name 或 file+line"
        "(file+line 更精确优先)。需先在「组件管理」构建源码索引。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {"type": "string", "description": "组件名称"},
            "name": {
                "type": "string",
                "description": "锚点符号名(与 file/line 二选一)",
            },
            "file": {
                "type": "string",
                "description": "源码文件名(与 line 配合作为行号锚点,更精确)",
            },
            "line": {
                "type": "integer",
                "description": "出错行号(与 file 配合)",
            },
            "max_depth": {
                "type": "integer",
                "description": "展开的跳数,默认 3,服务端硬上限 4",
            },
            "max_breadth": {
                "type": "integer",
                "description": "每跳展开的 callee 数,默认 4,服务端硬上限 5",
            },
        },
        "required": ["component"],
    },
    group="source",
)
async def trace_call_chain(
    dataset: LogDataset,
    component: str,
    name: str | None = None,
    file: str | None = None,
    line: int | None = None,
    max_depth: int = 3,
    max_breadth: int = 4,
) -> dict:
    """
    构建调用链骨架(BFS,深度/广度封顶,无函数体)。

    导航优先:只吐名字 + file:line + 每跳一行调用语句;细看交给 gather_code_context。
    """
    from ast_analysis import db

    user_id = dataset.user_id
    if user_id is None:
        return {"error": "源码索引需登录后可用"}

    if not (file and line is not None) and not name:
        return {
            "error": "缺少锚点：需提供 name，或 file+line",
            "component": component,
            "hint": "至少提供一个锚点(符号名 或 文件名+行号)",
        }

    depth_cap = max(1, min(int(max_depth or 3), _TRACE_MAX_DEPTH))
    breadth_cap = max(1, min(int(max_breadth or 4), _TRACE_MAX_BREADTH))

    anchor_sym = _resolve_anchor(user_id, component, name, file, line)
    if anchor_sym is None:
        return _not_found_payload(user_id, component, name=name)

    hops: list[dict] = []
    interfaces_seen: list[str] = []
    unresolved: list[str] = []
    visited: set[tuple[str, str]] = set()
    depth_reached = 0

    # BFS 队列:(符号 dict, 该符号所在深度)。深度 < depth_cap 的函数才展开其调用。
    queue: list[tuple[dict, int]] = [(anchor_sym, 0)]
    visited.add((anchor_sym["rel_path"], anchor_sym["name"]))

    while queue:
        sym, depth = queue.pop(0)
        if depth >= depth_cap:
            continue

        raw = _read_raw_lines(user_id, component, sym["rel_path"])
        if not raw:
            continue
        lo = max(0, int(sym["start_line"]) - 1)
        hi = min(len(raw), int(sym["end_line"]))
        body = [(i + 1, raw[i]) for i in range(lo, hi)]

        callees = _extract_callees(body, sym["name"])
        calls: list[dict] = []
        truncated = False
        for callee, call_line, call_src in callees:
            iface = _detect_interface(call_src)
            resolved = _resolve_callee(user_id, component, callee)
            # 只保留:能解析到本组件符号的,或命中接口的;其余(stdlib 等)丢进 unresolved
            if resolved is None and iface is None:
                if callee not in unresolved and len(unresolved) < _TRACE_MAX_UNRESOLVED:
                    unresolved.append(callee)
                continue
            if len(calls) >= breadth_cap:
                truncated = True
                break
            entry: dict = {
                "callee": callee,
                "call_line": call_line,
                "call_src": f"{call_line:>5} | {call_src.rstrip()}",
            }
            if resolved is not None:
                entry["resolved"] = {
                    "rel_path": resolved["rel_path"],
                    "start_line": resolved["start_line"],
                }
            else:
                entry["resolved"] = None
            if iface is not None:
                entry["interface"] = iface
                if iface not in interfaces_seen:
                    interfaces_seen.append(iface)
            calls.append(entry)

            # 入队更深一跳(去环 + 仅 resolved 的能继续展开)
            if resolved is not None:
                key = (resolved["rel_path"], resolved["name"])
                if key not in visited and depth + 1 < depth_cap:
                    visited.add(key)
                    queue.append((resolved, depth + 1))

        hops.append(
            {
                "depth": depth,
                "from": {
                    "function": sym["name"],
                    "rel_path": sym["rel_path"],
                    "start_line": sym["start_line"],
                    "end_line": sym["end_line"],
                },
                "calls": calls,
                "truncated_calls": truncated,
            }
        )
        depth_reached = max(depth_reached, depth + 1 if calls else depth)

    return {
        "component": component,
        "anchor": {
            "rel_path": anchor_sym["rel_path"],
            "function": anchor_sym["name"],
            "start_line": anchor_sym["start_line"],
            "end_line": anchor_sym["end_line"],
        },
        "hops": hops,
        "interfaces_seen": interfaces_seen,
        "unresolved_calls": unresolved,
        "depth_reached": depth_reached,
        "guidance": (
            "这是导航骨架(名字 + file:line + 每跳一行调用语句,无函数体)。"
            "挑出可疑那一跳,用 gather_code_context(component, file=<rel_path>, "
            "line=<start_line>) 读那一个函数体;对 interfaces_seen 里的接口用 "
            "resolve_interface(name) 找 provider 组件。"
        ),
    }


def _scan_component_interface(
    root: str, interface: str, cap: int
) -> list[dict]:
    """
    在单个组件快照根下有界扫描接口字面串,返回带角色分类的命中。

    复用 _find_usages 的遍历约束(analyzer._SKIP_DIRS / _EXT_TO_LANG / 大小限制)。

    Returns:
        [{rel_path, line, signal(=role 信号词原文或''), role}, ...]，总量 ≤ cap。
    """
    from ast_analysis import analyzer

    results: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in analyzer._SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in analyzer._EXT_TO_LANG:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(fpath) > analyzer._MAX_FILE_BYTES:
                    continue
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            rel = os.path.relpath(fpath, root).replace(os.sep, "/")
            for i, raw in enumerate(lines, start=1):
                if interface in raw:
                    results.append(
                        {
                            "rel_path": rel,
                            "line": i,
                            "role": _classify_interface_hit(raw),
                        }
                    )
                    if len(results) >= cap:
                        return results
    return results


@ToolRegistry.register(
    name="resolve_interface",
    description=(
        "把一个 mdb/dbus 接口(bmc.dev.*/bmc.kepler.*)解析到「哪个组件提供(provide)、"
        "哪些组件消费(consume)」。openUBMC 跨组件根因常是:消费侧拿不到对象/属性,真正"
        "的根因在 provider 组件。当日志/调用链指向某接口、需判定是哪一侧出问题时调用。"
        "在**所有已索引组件**里扫接口字面串并分 provide/consume;若没有已索引的 provider"
        "(常见,provider 组件未被索引),会用 wiki 兜底点名架构文档里的 provider。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "接口名,如 bmc.dev.PCIeDevice",
            },
            "max_hits": {
                "type": "integer",
                "description": "每组件最多返回的命中数,默认 8",
            },
        },
        "required": ["name"],
    },
    group="source",
)
async def resolve_interface(
    dataset: LogDataset,
    name: str,
    max_hits: int = 8,
) -> dict:
    """在已索引组件里定位接口的 provider/consumer;无已索引 provider 时 wiki 兜底。"""
    from ast_analysis import db
    from ast_analysis.service import _component_source_dir

    user_id = dataset.user_id
    if user_id is None:
        return {"error": "源码索引需登录后可用"}

    interface = (name or "").strip()
    if not interface:
        return {"error": "缺少接口名", "hint": "请提供接口名,如 bmc.dev.PCIeDevice"}

    cap = max(1, int(max_hits or 8))
    providers: list[dict] = []
    consumers: list[dict] = []
    for comp in db.list_user_components(user_id):
        cname = comp["component"]
        root = _component_source_dir(user_id, cname)
        if not os.path.isdir(root):
            continue
        for hit in _scan_component_interface(root, interface, cap):
            row = {
                "component": cname,
                "rel_path": hit["rel_path"],
                "line": hit["line"],
                "signal": hit["role"] or "",
            }
            # provide 信号明确才算 provider;其余(consume / 无信号)归 consumer 侧
            if hit["role"] == "provide":
                providers.append(row)
            else:
                consumers.append(row)

    result: dict = {
        "interface": interface,
        "providers": providers[:cap],
        "consumers": consumers[:cap],
        "provider_indexed": bool(providers),
    }

    # wiki 兜底:没有已索引 provider 时,从架构文档点名 provider(不臆造其代码)
    if not providers:
        try:
            from wiki import store as wiki_store

            if wiki_store.is_indexed():
                hits = wiki_store.search(interface, top_k=3)
                if hits:
                    top = hits[0]
                    result["wiki_hint"] = {
                        "slug": top["slug"],
                        "title": top["title"],
                        "snippet": top["snippet"],
                    }
        except Exception:
            pass

    result["guidance"] = (
        "对象/属性缺失时,provider 组件通常是根因。provider_indexed 为 false 时,"
        "从 wiki_hint 点名 provider,不要臆造它的代码;并在回答里指出消费侧的容错改法。"
    )
    return result
