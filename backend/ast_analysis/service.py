"""
AST 分析编排：增量算法 + SSE 流

核心算法（纯增量 upsert —— 只更新本次选中组件，不删除未选中组件）：

    S_new = 本次选择且在组件注册表中存在的组件集合
    S_old = 该用户上一次已持久化的组件集合
    to_add    = S_new - S_old       # 全新克隆 + 解析
    to_check  = S_new & S_old       # ls-remote 比对 commit：
                                      相同 → 复用旧数据（unchanged）
                                      不同 → 重新克隆解析（updated）

注意：未选中组件（S_old - S_new）保持不动——删除是显式操作，由
「删除分析」(DELETE /api/ast/components/{name}) 负责，不在 analyze 流程里。

成功组件在一次事务内整体落盘；失败组件保持既有数据不动，绝不误删。

阻塞调用（git / 文件 IO / DB）全部经 asyncio.to_thread 包装。
"""

import asyncio
import os
import shutil
from datetime import datetime, timezone

from ast_analysis import analyzer, db, downloader


def _sse(event: dict) -> str:
    """事件 dict → SSE data 行（局部实现，避免与 main 互相 import）。"""
    import json

    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def analyze_stream(user_id: int, selected_names: list[str]):
    """
    增量分析异步生成器：逐个 yield SSE 事件 dict（调用方负责序列化为 SSE 文本）。

    事件序列（详见模块 docstring 与 routes 的契约说明）：
        plan → (progress|component_result)* → summary → done
    """
    # 1) 解析组件注册表：name → (git_url, branch)（按用户隔离）
    registry = await asyncio.to_thread(_resolve_registry, user_id)

    S_new = {n for n in selected_names if n in registry}
    invalid = [n for n in selected_names if n not in registry]

    # 选择里全部无效且非空 → 报错返回，绝不清空既有数据
    if selected_names and not S_new:
        yield {
            "type": "error",
            "message": "no valid components selected (none exist in registry)",
            "invalid": invalid,
        }
        return

    S_old = {
        c["component"] for c in await asyncio.to_thread(db.list_user_components, user_id)
    }
    to_add = S_new - S_old
    to_check = S_new & S_old
    # 注：S_old - S_new（本次未选中的组件）刻意不处理——analyze 只增量 upsert
    # 选中组件，绝不删除既有组件。删除走显式的「删除分析」端点。

    yield {
        "type": "plan",
        "added": sorted(to_add),
        "check": sorted(to_check),
        "invalid": sorted(invalid),
    }

    # 2) 收集要落盘的结果
    collected: dict[str, dict] = {}   # component -> 持久化载荷
    # 本次分析后「已分析」的组件名集合（added/updated/unchanged 成功项），
    # 用于回写组件注册表 enabled=True；失败项不入列，保持原 enabled。
    analyzed_ok: set[str] = set()

    # 计数
    counts = {"added": 0, "updated": 0, "unchanged": 0, "error": 0}

    for c in sorted(to_add | to_check):
        git_url, branch = registry[c]

        # to_check：先 ls-remote 比对
        if c in to_check:
            current = await asyncio.to_thread(
                downloader.remote_commit, git_url, branch
            )
            stored = await asyncio.to_thread(db.get_component_commit, user_id, c)
            if (
                current is not None
                and stored is not None
                and current == stored
            ):
                # commit 未变：复用旧数据
                file_count, symbol_count = await asyncio.to_thread(
                    db._stored_counts, user_id, c
                )
                counts["unchanged"] += 1
                analyzed_ok.add(c)
                yield {
                    "type": "component_result",
                    "component": c,
                    "action": "unchanged",
                    "commit": (stored or "")[:8],
                    "files": file_count,
                    "symbols": symbol_count,
                    "error": None,
                }
                continue
            # 否则按「更新」处理，落入下方克隆流程

        # 克隆 + 解析。源码树保留到 SOURCE_DIR/<user_id>/<component> 作为快照，
        # 供 get_function_source 按行号区间切出函数体。
        yield {"type": "progress", "stage": "cloning", "component": c}
        action = "added" if c in to_add else "updated"
        try:
            dest = await asyncio.to_thread(_prepare_source_dir, user_id, c)
        except ValueError as e:
            # 不安全的 component 名（路径穿越风险）→ 上报错误，不克隆
            counts["error"] += 1
            yield {
                "type": "component_result",
                "component": c,
                "action": "error",
                "commit": None,
                "files": 0,
                "symbols": 0,
                "error": str(e),
            }
            continue
        try:
            commit = await asyncio.to_thread(
                downloader.clone_component, git_url, branch, dest
            )
            # 清理 .git，快照只留工作树源码
            await asyncio.to_thread(_strip_git, dest)
            yield {"type": "progress", "stage": "parsing", "component": c}
            result = await asyncio.to_thread(analyzer.analyze_directory, dest, c)
            collected[c] = {
                "git_url": git_url,
                "branch": branch,
                "commit": commit,
                "files": result["files"],
            }
            analyzed_ok.add(c)
            counts[action] += 1
            yield {
                "type": "component_result",
                "component": c,
                "action": action,
                "commit": commit[:8],
                "files": len(result["files"]),
                "symbols": result["stats"]["symbols"],
                "error": None,
            }
        except Exception as e:
            # 失败：ast.db 既有数据保持不变（不进 collected），仅清理半成品快照
            await asyncio.to_thread(shutil.rmtree, dest, True)
            counts["error"] += 1
            yield {
                "type": "component_result",
                "component": c,
                "action": "error",
                "commit": None,
                "files": 0,
                "symbols": 0,
                "error": str(e),
            }

    # 3) 持久化（一次提交：替换成功项 + meta + 维护注册表 enabled）
    if collected or analyzed_ok:
        await asyncio.to_thread(
            _persist, user_id, collected, analyzed_ok, _now_iso()
        )

    # 4) 汇总
    final_result = await asyncio.to_thread(db.get_result, user_id)
    components_total = (
        final_result["stats"]["components"] if final_result else 0
    )
    files_total = final_result["stats"]["files"] if final_result else 0
    symbols_total = final_result["stats"]["symbols"] if final_result else 0

    yield {
        "type": "summary",
        "stats": {
            "added": counts["added"],
            "updated": counts["updated"],
            "unchanged": counts["unchanged"],
            "error": counts["error"],
            "components_total": components_total,
            "files_total": files_total,
            "symbols_total": symbols_total,
        },
        "last_analyzed_at": _now_iso(),
    }
    yield {"type": "done"}


def _resolve_registry(user_id: int) -> dict[str, tuple[str, str]]:
    """
    从该用户的组件注册表读取全部组件，返回 {name: (git_url, branch)}。

    独立函数 + asyncio.to_thread 包装，避免在事件循环里直接做 SQLite IO。
    """
    import db as component_db

    out: dict[str, tuple[str, str]] = {}
    for c in component_db.list_components(user_id):
        out[c["name"]] = (c["git_url"], c["branch"])
    return out


def _is_safe_component_name(name: str) -> bool:
    """component 名作为目录段是否安全（拒绝分隔符 / 上跳 / 隐藏名）。"""
    if not name or name in (".", ".."):
        return False
    return "/" not in name and "\\" not in name and not name.startswith(".")


def _component_source_dir(user_id: int, component: str) -> str:
    """组件源码快照目录：<SOURCE_DIR>/<user_id>/<component>。"""
    if not _is_safe_component_name(component):
        raise ValueError(f"unsafe component name: {component!r}")
    return os.path.join(db.SOURCE_DIR, str(user_id), component)


def _prepare_source_dir(user_id: int, component: str) -> str:
    """返回组件快照目录并清空旧内容（git clone 要求目标不存在/为空）。"""
    dest = _component_source_dir(user_id, component)
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    return dest


def _strip_git(dest: str) -> None:
    """clone 后清理 .git 目录，快照只保留工作树源码。"""
    git_dir = os.path.join(dest, ".git")
    if os.path.isdir(git_dir):
        shutil.rmtree(git_dir, ignore_errors=True)


def _persist(
    user_id: int,
    collected: dict[str, dict],
    analyzed_ok: set[str],
    analyzed_at: str,
) -> None:
    """
    一次性落盘：替换成功项 + 更新 meta + 维护组件注册表 enabled。

    注意：analyze 不删除任何组件——失败组件不在 collected / analyzed_ok 中，
    因此其既有数据与 enabled 均保持不变；未选中组件更不会被触碰。
    删除走显式的「删除分析」端点（db.delete_component）。
    """
    import db as component_db

    for c, payload in collected.items():
        db.replace_component(
            user_id,
            c,
            payload["git_url"],
            payload["branch"],
            payload["commit"],
            analyzed_at,
            payload["files"],
        )
    # 维护组件注册表 enabled（= 已分析）：成功项置 True。
    # set_enabled 在组件已被用户从注册表删除时静默忽略，不报错。
    for c in analyzed_ok:
        component_db.set_enabled(user_id, c, True)
    db.set_last_analyzed(user_id, analyzed_at)
