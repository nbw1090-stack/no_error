"""
日志分析工具集

提供搜索、过滤、统计等日志分析能力。
所有工具通过 @ToolRegistry.register() 装饰器注册，
第一个参数固定为 dataset: LogDataset。
"""

from collections import defaultdict

from agent.tools.registry import ToolRegistry
from agent.tools.log_dedup import group_by_pattern
from agent.dataset import LogDataset


# ============================================================
# 基础查询工具
# ============================================================


@ToolRegistry.register(
    name="search_logs",
    description=(
        "根据关键词搜索日志消息（不区分大小写）。"
        "默认按消息模板分组聚合：同模板重复触发的日志合并为一组，"
        "含触发次数 count、中位间隔 interval 和首/中/尾采样 id（sample_ids）。"
        "设置 dedup=false 可回退为逐条平铺返回。"
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
                "description": "最大返回组数（dedup=true）或条数（dedup=false），默认 20，最大 100",
            },
            "dedup": {
                "type": "boolean",
                "description": "是否按消息模板分组聚合，默认 true。false 则逐条平铺返回。",
            },
        },
        "required": ["keyword"],
    },
)
async def search_logs(
    dataset: LogDataset, keyword: str, max_results: int = 20, dedup: bool = True
) -> dict:
    """全文搜索日志消息。默认按模板聚合，dedup=false 回退平铺。"""
    max_results = min(max_results, 100)
    keyword_lower = keyword.lower()

    matches = []
    for entry in dataset.entries:
        message = entry.get("message") or ""
        if keyword_lower in message.lower():
            matches.append(entry)

    if dedup:
        groups = group_by_pattern(matches)[:max_results]
        return {
            "total_matches": len(matches),
            "returned_groups": len(groups),
            "dedup": True,
            "keyword": keyword,
            "groups": groups,
        }

    flat = [
        {
            "id": e["id"],
            "timestamp": e["timestamp"],
            "component": e["component"],
            "level": e["level"],
            "message": e.get("message"),
            "source": e["source"],
        }
        for e in matches[:max_results]
    ]
    return {
        "count": len(flat),
        "total_matches": len(matches),
        "dedup": False,
        "keyword": keyword,
        "results": flat,
    }


@ToolRegistry.register(
    name="filter_by_component",
    description=(
        "按组件名筛选日志条目，可选按日志级别进一步过滤。"
        "默认按消息模板分组聚合（同模板重复触发的日志合并为一组，含 count/interval/sample_ids），"
        "设置 dedup=false 可回退为逐条平铺返回。"
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
                "description": "最大返回组数（dedup=true）或条数（dedup=false），默认 50，最大 200",
            },
            "dedup": {
                "type": "boolean",
                "description": "是否按消息模板分组聚合，默认 true。false 则逐条平铺返回。",
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
    dedup: bool = True,
) -> dict:
    """按组件筛选日志。默认按模板聚合，dedup=false 回退平铺。"""
    max_results = min(max_results, 200)
    component_lower = component.lower()

    matches = []
    for entry in dataset.entries:
        entry_component = (entry.get("component") or "").lower()
        if component_lower not in entry_component:
            continue
        if level != "ALL" and entry.get("level") != level:
            continue
        matches.append(entry)

    if dedup:
        groups = group_by_pattern(matches)[:max_results]
        return {
            "total_matches": len(matches),
            "returned_groups": len(groups),
            "dedup": True,
            "component": component,
            "level_filter": level,
            "groups": groups,
        }

    flat = [
        {
            "id": e["id"],
            "timestamp": e["timestamp"],
            "level": e["level"],
            "message": e.get("message"),
            "file": e.get("file"),
            "line": e.get("line"),
            "source": e["source"],
        }
        for e in matches[:max_results]
    ]
    return {
        "count": len(flat),
        "total_matches": len(matches),
        "dedup": False,
        "component": component,
        "level_filter": level,
        "results": flat,
    }


@ToolRegistry.register(
    name="filter_by_level",
    description=(
        "按日志级别筛选所有条目。"
        "支持 ERROR, WARNING, NOTICE, INFO, DEBUG, CRITICAL, LAUNCH, UNKNOWN。"
        "默认按消息模板分组聚合（同模板重复触发的日志合并为一组，含 count/interval/sample_ids），"
        "设置 dedup=false 可回退为逐条平铺返回。"
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
                "description": "最大返回组数（dedup=true）或条数（dedup=false），默认 50，最大 200",
            },
            "dedup": {
                "type": "boolean",
                "description": "是否按消息模板分组聚合，默认 true。false 则逐条平铺返回。",
            },
        },
        "required": ["level"],
    },
)
async def filter_by_level(
    dataset: LogDataset, level: str, max_results: int = 50, dedup: bool = True
) -> dict:
    """按级别筛选日志。默认按模板聚合，dedup=false 回退平铺。"""
    max_results = min(max_results, 200)

    matches = []
    for entry in dataset.entries:
        if entry.get("level") != level:
            continue
        matches.append(entry)

    if dedup:
        groups = group_by_pattern(matches)[:max_results]
        return {
            "total_matches": len(matches),
            "returned_groups": len(groups),
            "dedup": True,
            "level": level,
            "groups": groups,
        }

    flat = [
        {
            "id": e["id"],
            "timestamp": e["timestamp"],
            "component": e["component"],
            "message": e.get("message"),
            "file": e.get("file"),
            "line": e.get("line"),
            "source": e["source"],
        }
        for e in matches[:max_results]
    ]
    return {
        "count": len(flat),
        "total_matches": len(matches),
        "dedup": False,
        "level": level,
        "results": flat,
    }


# ============================================================
# 统计工具
# ============================================================


@ToolRegistry.register(
    name="get_summary",
    description=(
        "一站式概览：整体计数 + 按组件的级别分布，分析的第一步首选此工具。"
        "返回总行数、各级别（ERROR/WARNING/NOTICE/LAUNCH）总计、时间范围，"
        "以及按错误数降序排列的各组件级别统计（含 top_error_components）。"
        "用此判断「哪些组件问题最多」，再用 get_error_digest 看具体错误模式。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "max_components": {
                "type": "integer",
                "description": "components 列表最多返回多少个组件（按错误数降序），默认 20",
            },
        },
    },
)
async def get_summary(dataset: LogDataset, max_components: int = 20) -> dict:
    """整体计数 + 按组件级别分布的合并概览（精简）。

    合并了原 get_summary（整体摘要）与 get_component_stats（按组件统计），
    避免两次工具调用返回大量重合内容。仅保留非零级别字段以节省 token。
    """
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

    # 按 ERROR 数、再按总数降序排列
    ranked = sorted(
        stats.items(),
        key=lambda kv: (kv[1]["ERROR"], sum(kv[1].values())),
        reverse=True,
    )

    components = []
    for comp, counts in ranked[:max_components]:
        # 仅保留非零的级别字段，进一步压缩回喂量
        entry = {"component": comp, "total": sum(counts.values())}
        for level_key, out_key in (
            ("ERROR", "errors"),
            ("WARNING", "warnings"),
            ("NOTICE", "notices"),
            ("LAUNCH", "launches"),
        ):
            if counts[level_key]:
                entry[out_key] = counts[level_key]
        components.append(entry)

    top_error_components = [
        comp for comp, counts in ranked[:5] if counts["ERROR"] > 0
    ]

    return {
        "total_lines": dataset.total_lines,
        "errors": dataset.error_count,
        "warnings": dataset.warning_count,
        "notices": dataset.notice_count,
        "launches": dataset.launch_count,
        "time_range": {"start": dataset.time_start, "end": dataset.time_end},
        "total_components": len(ranked),
        "top_error_components": top_error_components,
        "components": components,
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
        "获取指定组件的所有 ERROR 级别日志。"
        "默认按消息模板分组聚合（同模板重复触发的错误合并为一组，含触发次数 count、"
        "中位间隔 interval、首末时间与首/中/尾采样 id sample_ids），"
        "设置 dedup=false 可逐条平铺返回。"
        "用于深入分析某个组件的具体错误。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {
                "type": "string",
                "description": "组件名称",
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回组数（dedup=true）或条数（dedup=false），默认 30，最大 200",
            },
            "dedup": {
                "type": "boolean",
                "description": "是否按消息模板分组聚合，默认 true。false 则逐条平铺返回。",
            },
        },
        "required": ["component"],
    },
)
async def get_errors_by_component(
    dataset: LogDataset,
    component: str,
    max_results: int = 30,
    dedup: bool = True,
) -> dict:
    """获取指定组件的所有错误。默认按模板聚合，dedup=false 回退平铺。"""
    max_results = min(max_results, 200)
    component_lower = component.lower()

    errors = []
    for entry in dataset.entries:
        entry_component = (entry.get("component") or "").lower()
        if entry_component != component_lower:
            continue
        if entry.get("level") != "ERROR":
            continue
        errors.append(entry)

    if dedup:
        groups = group_by_pattern(errors)[:max_results]
        return {
            "total_matches": len(errors),
            "returned_groups": len(groups),
            "dedup": True,
            "component": component,
            "groups": groups,
        }

    flat = [
        {
            "id": e["id"],
            "timestamp": e["timestamp"],
            "message": e.get("message"),
            "file": e.get("file"),
            "line": e.get("line"),
            "source": e["source"],
        }
        for e in errors
    ]
    flat.sort(key=lambda e: e["timestamp"] or "")
    flat = flat[:max_results]
    return {
        "count": len(flat),
        "total_matches": len(errors),
        "dedup": False,
        "component": component,
        "errors": flat,
    }


@ToolRegistry.register(
    name="get_error_digest",
    description=(
        "错误全景速览：所有 ERROR 按消息模板「按内容去重」后的精简清单。"
        "前期分析首选——不会逐条平铺成百上千条仅时间不同的重复错误，"
        "而是每种不同错误合并成一组，含触发次数 count、首末时间、"
        "中位间隔 interval 和首/中/尾采样 id（sample_ids）。"
        "可选按组件过滤。需要某条错误的精确原文/上下文时，"
        "再用 sample_ids 调 get_context_around，或用 get_errors_by_component(dedup=false)。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "component": {
                "type": "string",
                "description": "可选：只看某组件的错误，不提供则汇总全部组件",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回多少个错误模板组（按触发次数降序），默认 30，最大 100",
            },
        },
    },
)
async def get_error_digest(
    dataset: LogDataset, component: str = "", max_results: int = 30
) -> dict:
    """所有 ERROR 按消息模板去重后的精简清单（保留次数/时间跨度/采样 id）。

    取代原 get_error_timeline 的按小时直方图：前期分析关心的是
    「有哪些不同的错误」，而非「每小时几条」。逐条带时间戳的明细
    交给 get_context_around / dedup=false 的明细工具按需下钻。
    """
    max_results = min(max_results, 100)
    component_lower = component.lower()

    errors = []
    for entry in dataset.entries:
        if entry.get("level") != "ERROR":
            continue
        if component_lower:
            entry_component = (entry.get("component") or "").lower()
            if entry_component != component_lower:
                continue
        errors.append(entry)

    all_groups = group_by_pattern(errors)
    groups = all_groups[:max_results]

    return {
        "component": component or "all",
        "total_errors": len(errors),
        "distinct_patterns": len(all_groups),
        "returned_groups": len(groups),
        "groups": groups,
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
