"""
日志数据集封装

提供对解析后日志数据的类型化访问，作为工具函数的标准输入。
"""

from dataclasses import dataclass


@dataclass
class LogDataset:
    """
    不可变的数据集封装，包含解析后的日志条目和汇总统计。

    由 parser.parse_logs() 的输出构造，传递给所有工具函数。
    """

    entries: list[dict]  # 解析后的日志条目列表
    summary: dict  # 汇总统计字典
    # 当前请求的登录用户 id（由 /api/chat 注入），供按用户隔离的源码索引工具使用。
    # 默认 None：测试与降级（规则匹配）场景不涉及源码查询。
    user_id: int | None = None

    @property
    def total_lines(self) -> int:
        return self.summary.get("totalLines", 0)

    @property
    def error_count(self) -> int:
        return self.summary.get("errorCount", 0)

    @property
    def warning_count(self) -> int:
        return self.summary.get("warningCount", 0)

    @property
    def notice_count(self) -> int:
        return self.summary.get("noticeCount", 0)

    @property
    def launch_count(self) -> int:
        return self.summary.get("launchCount", 0)

    @property
    def components(self) -> list[str]:
        return self.summary.get("components", [])

    @property
    def time_start(self) -> str | None:
        return self.summary.get("timeRange", {}).get("start")

    @property
    def time_end(self) -> str | None:
        return self.summary.get("timeRange", {}).get("end")
