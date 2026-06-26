"""
日志分析工具集

提供搜索、过滤、统计等日志分析能力。
所有工具通过 @ToolRegistry.register() 装饰器注册，
第一个参数固定为 dataset: LogDataset。
"""

from collections import defaultdict

from agent.tools.registry import ToolRegistry
from agent.dataset import LogDataset


# ============================================================
# 基础查询工具
# ============================================================


@ToolRegistry.register(
    name="search_logs",
    description=(
        "根据关键词搜索日志消息（不区分大小写）。"
        "返回匹配的日志条目，包含 id、时间戳、组件、级别和消息。"
        "适用于查找特定错误消息、组件名或关键术语。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "要搜索的关键词或短语",
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回条数，默认 20，最大 100",
            },
        },
        "required": ["keyword"],
    },
)
async def search_logs(
    dataset: LogDataset, keyword: str, max_results: int = 20
) -> dict:
    """全文搜索日志消息"""
    max_results = min(max_results, 100)
    keyword_lower = keyword.lower()

    matches = []
    for entry in dataset.entries:
        message = entry.get("message") or ""
        if keyword_lower in message.lower():
            matches.append(
                {
                    "id": entry["id"],
                    "timestamp": entry["timestamp"],
                    "component": entry["component"],
                    "level": entry["level"],
                    "message": message,
                    "source": entry["source"],
                }
            )
            if len(matches) >= max_results:
                break

    return {"count": len(matches), "results": matches}


@ToolRegistry.register(
    name="filter_by_component",
    description=(
        "按组件名筛选日志条目，可选按日志级别进一步过滤。"
        "返回指定组件的日志条目列表。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {
                "type": "string",
                "description": "要筛选的组件名称",
            },
            "level": {
                "type": "string",
                "description": "可选：日志级别过滤（ERROR, WARNING, NOTICE, LAUNCH），默认 ALL 返回所有级别",
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回条数，默认 50，最大 200",
            },
        },
        "required": ["component"],
    },
)
async def filter_by_component(
    dataset: LogDataset,
    component: str,
    level: str = "ALL",
    max_results: int = 50,
) -> dict:
    """按组件筛选日志"""
    max_results = min(max_results, 200)
    component_lower = component.lower()

    matches = []
    for entry in dataset.entries:
        entry_component = (entry.get("component") or "").lower()
        if component_lower not in entry_component:
            continue
        if level != "ALL" and entry.get("level") != level:
            continue

        matches.append(
            {
                "id": entry["id"],
                "timestamp": entry["timestamp"],
                "level": entry["level"],
                "message": entry.get("message"),
                "file": entry.get("file"),
                "line": entry.get("line"),
                "source": entry["source"],
            }
        )
        if len(matches) >= max_results:
            break

    return {
        "count": len(matches),
        "component": component,
        "level_filter": level,
        "results": matches,
    }


@ToolRegistry.register(
    name="filter_by_level",
    description=(
        "按日志级别筛选所有条目。"
        "支持 ERROR, WARNING, NOTICE, INFO, DEBUG, CRITICAL, LAUNCH, UNKNOWN。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "level": {
                "type": "string",
                "description": "日志级别（ERROR, WARNING, NOTICE, LAUNCH 等）",
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回条数，默认 50，最大 200",
            },
        },
        "required": ["level"],
    },
)
async def filter_by_level(
    dataset: LogDataset, level: str, max_results: int = 50
) -> dict:
    """按级别筛选日志"""
    max_results = min(max_results, 200)

    matches = []
    for entry in dataset.entries:
        if entry.get("level") != level:
            continue
        matches.append(
            {
                "id": entry["id"],
                "timestamp": entry["timestamp"],
                "component": entry["component"],
                "message": entry.get("message"),
                "file": entry.get("file"),
                "line": entry.get("line"),
                "source": entry["source"],
            }
        )
        if len(matches) >= max_results:
            break

    return {"count": len(matches), "level": level, "results": matches}


# ============================================================
# 统计工具
# ============================================================


@ToolRegistry.register(
    name="get_summary",
    description=(
        "获取日志数据集的整体统计摘要。"
        "返回总行数、各级别计数、组件列表和时间范围。"
        "用户询问概览、总结、统计数据时使用此工具。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
async def get_summary(dataset: LogDataset) -> dict:
    """返回日志整体摘要"""
    return dataset.summary


@ToolRegistry.register(
    name="get_component_stats",
    description=(
        "获取每个组件的日志统计：各级别（ERROR/WARNING/NOTICE/LAUNCH）日志条数。"
        "返回按错误数降序排列的组件列表。"
        "适用于了解哪些组件问题最多。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
async def get_component_stats(dataset: LogDataset) -> dict:
    """按组件统计各级别日志数量"""
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"ERROR": 0, "WARNING": 0, "NOTICE": 0, "LAUNCH": 0, "OTHER": 0}
    )

    for entry in dataset.entries:
        component = entry.get("component") or "unknown"
        level = entry.get("level") or "UNKNOWN"

        if level in ("ERROR", "WARNING", "NOTICE", "LAUNCH"):
            stats[component][level] += 1
        else:
            stats[component]["OTHER"] += 1

    # 按 ERROR 数降序排列
    sorted_stats = sorted(
        [
            {
                "component": comp,
                "total": sum(counts.values()),
                "errors": counts["ERROR"],
                "warnings": counts["WARNING"],
                "notices": counts["NOTICE"],
                "launches": counts["LAUNCH"],
                "other": counts["OTHER"],
            }
            for comp, counts in stats.items()
        ],
        key=lambda x: x["errors"],
        reverse=True,
    )

    # 找到错误最多的 Top 5 组件
    top_error_components = [s["component"] for s in sorted_stats[:5] if s["errors"] > 0]

    return {
        "total_components": len(sorted_stats),
        "top_error_components": top_error_components,
        "components": sorted_stats,
    }


@ToolRegistry.register(
    name="get_time_range",
    description="获取日志的时间范围（起始时间和结束时间）。",
    parameters={
        "type": "object",
        "properties": {},
    },
)
async def get_time_range(dataset: LogDataset) -> dict:
    """返回日志时间范围"""
    return {
        "start": dataset.time_start,
        "end": dataset.time_end,
    }


# ============================================================
# 深度分析工具
# ============================================================


@ToolRegistry.register(
    name="get_errors_by_component",
    description=(
        "获取指定组件的所有 ERROR 级别日志，按时间排序。"
        "用于深入分析某个组件的具体错误。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {
                "type": "string",
                "description": "组件名称",
            },
        },
        "required": ["component"],
    },
)
async def get_errors_by_component(
    dataset: LogDataset, component: str
) -> dict:
    """获取指定组件的所有错误"""
    component_lower = component.lower()

    errors = []
    for entry in dataset.entries:
        entry_component = (entry.get("component") or "").lower()
        if entry_component != component_lower:
            continue
        if entry.get("level") != "ERROR":
            continue

        errors.append(
            {
                "id": entry["id"],
                "timestamp": entry["timestamp"],
                "message": entry.get("message"),
                "file": entry.get("file"),
                "line": entry.get("line"),
                "source": entry["source"],
            }
        )

    # 按时间戳排序
    errors.sort(key=lambda e: e["timestamp"] or "")

    return {"count": len(errors), "component": component, "errors": errors}


@ToolRegistry.register(
    name="get_error_timeline",
    description=(
        "获取错误日志的时间线分布（按小时聚合）。"
        "可以指定组件来查看特定组件的错误趋势，不指定则展示全局趋势。"
        "适用于发现错误爆发的时间段。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {
                "type": "string",
                "description": "可选：要查看的组件名，不提供则查看全局错误趋势",
            },
        },
    },
)
async def get_error_timeline(
    dataset: LogDataset, component: str = ""
) -> dict:
    """获取错误时间线（按小时聚合）"""
    component_lower = component.lower()

    timeline: dict[str, int] = defaultdict(int)
    for entry in dataset.entries:
        if entry.get("level") != "ERROR":
            continue
        if component_lower:
            entry_component = (entry.get("component") or "").lower()
            if entry_component != component_lower:
                continue

        ts = entry.get("timestamp") or ""
        # 提取小时：2025-07-24 11:31:13.714532 → 2025-07-24 11:00
        if len(ts) >= 13:
            hour_bucket = ts[:13] + ":00"
            timeline[hour_bucket] += 1

    sorted_timeline = sorted(timeline.items())

    return {
        "component": component or "all",
        "timeline": [
            {"hour": hour, "error_count": count}
            for hour, count in sorted_timeline
        ],
    }


@ToolRegistry.register(
    name="get_context_around",
    description=(
        "获取指定日志条目前后各 N 条日志（共 2N+1 条）。"
        "适用于查看某条错误发生前后的上下文，帮助理解根因。"
        "entry_id 可以从其他工具返回的结果中获得。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "integer",
                "description": "目标日志条目的 ID",
            },
            "before": {
                "type": "integer",
                "description": "向前取多少条，默认 5",
            },
            "after": {
                "type": "integer",
                "description": "向后取多少条，默认 5",
            },
        },
        "required": ["entry_id"],
    },
)
async def get_context_around(
    dataset: LogDataset, entry_id: int, before: int = 5, after: int = 5
) -> dict:
    """获取某条日志的上下文"""
    before = min(before, 20)
    after = min(after, 20)

    entries = dataset.entries
    target_idx = None

    # 查找目标条目索引
    for i, entry in enumerate(entries):
        if entry["id"] == entry_id:
            target_idx = i
            break

    if target_idx is None:
        return {"error": f"Entry with id {entry_id} not found"}

    start = max(0, target_idx - before)
    end = min(len(entries), target_idx + after + 1)

    context_entries = []
    for i in range(start, end):
        entry = entries[i]
        context_entries.append(
            {
                "id": entry["id"],
                "timestamp": entry["timestamp"],
                "component": entry["component"],
                "level": entry["level"],
                "message": entry.get("message"),
                "file": entry.get("file"),
                "line": entry.get("line"),
                "source": entry["source"],
                "is_target": entry["id"] == entry_id,
            }
        )

    return {
        "target_id": entry_id,
        "before": before,
        "after": after,
        "total_returned": len(context_entries),
        "entries": context_entries,
    }
