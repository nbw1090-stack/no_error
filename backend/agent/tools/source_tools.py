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

from agent.tools.registry import ToolRegistry
from agent.dataset import LogDataset


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

    # 带行号渲染，便于 agent 精确引用行号
    rendered = "\n".join(
        f"{(lo + i + 1):>5} | {s}" for i, s in enumerate(slice_lines)
    )
    return (rendered, lo + 1, lo + len(slice_lines))
