"""
日志解析器

解析 app.log 和 framework.log 两种 BMC 日志格式：
- 标准格式：时间戳 组件 级别: 文件(行号): 消息
- LAUNCH 格式：时间戳 [线程] 组件: LAUNCH 消息

采用双正则策略：先尝试标准格式，失败则尝试 LAUNCH 格式，
均失败则标记为 UNKNOWN 并保留原始行。
"""

import re
from typing import Optional

# ============================================================
# 预编译正则表达式
# ============================================================

# 标准格式：2025-07-24 11:31:13.714532 pcie_device ERROR: pcie_card.lua(49): PCIe card ...
STANDARD_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) "
    r"(?P<component>\S+) "
    r"(?P<level>ERROR|WARNING|WARN|NOTICE|INFO|DEBUG|CRITICAL): "
    r"(?P<file>\S+)\((?P<line>\d+)\): "
    r"(?P<message>.*)$"
)

# LAUNCH 格式（仅 framework.log）：1970-01-01 00:00:21.666722 [:00000002] framework: LAUNCH snlua bootstrap
LAUNCH_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) "
    r"\[(?P<thread>[^\]]+)\] "
    r"(?P<component>\S+): "
    r"LAUNCH "
    r"(?P<message>.*)$"
)


def parse_logs(app_content: str, framework_content: str) -> dict:
    """
    解析 app.log 和 framework.log 的全部内容。

    Args:
        app_content: app.log 的文本内容
        framework_content: framework.log 的文本内容

    Returns:
        字典包含 entries（条目列表）和 summary（汇总统计）。
    """
    entries: list[dict] = []
    components: set[str] = set()
    timestamps: list[str] = []

    # 各级别计数器
    error_count = 0
    warning_count = 0
    notice_count = 0
    launch_count = 0
    unknown_count = 0

    # 按组件累计错误数（供前端 StatsPanel 直接展示，无需全量聚合 entries）
    component_error_counts: dict[str, int] = {}

    entry_id = 1  # 递增 ID，从 1 开始

    # 依次解析两个日志文件
    log_sources = [
        ("app.log", app_content),
        ("framework.log", framework_content),
    ]

    for source_name, content in log_sources:
        if not content:
            continue

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            # 先尝试标准格式匹配
            match = STANDARD_PATTERN.match(line)
            if match:
                level = match.group("level")
                # WARN 规范化为 WARNING
                if level == "WARN":
                    level = "WARNING"

                entry = {
                    "id": entry_id,
                    "timestamp": match.group("timestamp"),
                    "component": match.group("component"),
                    "level": level,
                    "file": match.group("file"),
                    "line": int(match.group("line")),
                    "message": match.group("message"),
                    "source": source_name,
                }
                entries.append(entry)
                timestamps.append(entry["timestamp"])
                components.add(entry["component"])

                # 统计计数
                if level == "ERROR":
                    error_count += 1
                    comp = entry["component"]
                    if comp:
                        component_error_counts[comp] = (
                            component_error_counts.get(comp, 0) + 1
                        )
                elif level == "WARNING":
                    warning_count += 1
                elif level == "NOTICE":
                    notice_count += 1

                entry_id += 1
                continue

            # 标准格式失败，尝试 LAUNCH 格式
            match = LAUNCH_PATTERN.match(line)
            if match:
                entry = {
                    "id": entry_id,
                    "timestamp": match.group("timestamp"),
                    "component": match.group("component"),
                    "level": "LAUNCH",
                    "file": None,
                    "line": None,
                    "message": match.group("message"),
                    "source": source_name,
                }
                entries.append(entry)
                timestamps.append(entry["timestamp"])
                components.add(entry["component"])
                launch_count += 1
                entry_id += 1
                continue

            # 两种格式均不匹配 → 标记为 UNKNOWN
            entry = {
                "id": entry_id,
                "timestamp": None,
                "component": None,
                "level": "UNKNOWN",
                "file": None,
                "line": None,
                "message": line,
                "source": source_name,
            }
            entries.append(entry)
            unknown_count += 1
            entry_id += 1

    # ============================================================
    # 构建汇总统计
    # ============================================================
    summary = {
        "totalLines": len(entries),
        "errorCount": error_count,
        "warningCount": warning_count,
        "noticeCount": notice_count,
        "launchCount": launch_count,
        "unknownCount": unknown_count,
        "components": sorted(components),
        "componentErrors": [
            {"name": name, "count": count}
            for name, count in sorted(
                component_error_counts.items(), key=lambda x: x[1], reverse=True
            )[:20]
        ],
        "timeRange": {
            "start": min(timestamps) if timestamps else None,
            "end": max(timestamps) if timestamps else None,
        },
    }

    return {
        "entries": entries,
        "summary": summary,
    }
