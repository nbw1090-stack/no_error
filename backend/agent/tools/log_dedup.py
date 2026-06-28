"""
日志消息模板归一化与分组聚合

目的:日志里大量「同一问题重复触发」(组件拉不起来每分钟重试、心跳轮询),
逐条原样回喂 LLM 会浪费大量 Token。本模块把同模板的日志条目聚合成一组,
每组只保留代表性样本 + 触发次数 + 中位间隔 + 首末时间,大幅压缩回喂量。

被 log_tools.py 的 4 个明细工具(search_logs / filter_by_component /
filter_by_level / get_errors_by_component)在 dedup=True 时复用。
"""

import re
from collections import defaultdict
from datetime import datetime
from statistics import median


# ============================================================
# 消息归一化
# ============================================================

# 替换顺序至关重要:先吃长格式(IP/HEX/FLOAT),最后吃整数,否则会相互破坏。
#   - IP 必须最先:否则 192.168.1.10 会被 INT 拆成四段;IP 吃掉后,
#     残余的端口(如 :8080)再由 INT 处理。
#   - HEX 必须在 INT 前:否则 0xDEAD 的数字段先被 INT 吃掉。
#   - FLOAT 必须在 INT 前:前后用 lookbehind/lookahead 排除 [\w.],
#     避免匹配到标识符里的点或已被替换的 <IP> 残段。
#   - INT 用裸 \d+(不带 \b):真实日志有 Event_Mem17OverTempMajor_01010117
#     这种数字紧贴字母的标识符,\b 在字母与数字交界处不匹配会漏归一化。
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_FLOAT_RE = re.compile(r"(?<![\w.])\d+\.\d+(?![\w.])")
_INT_RE = re.compile(r"\d+")


def normalize_message(msg: str) -> str:
    """把消息里的数值参数归一化为占位符,用作模板分组的签名成分。

    时间戳不在 message 字段内(解析时已单独存到 entry["timestamp"]),
    因此本函数只处理数值参数,不识别时间戳。
    """
    if not msg:
        return ""
    s = _IP_RE.sub("<IP>", msg)
    s = _HEX_RE.sub("<HEX>", s)
    s = _FLOAT_RE.sub("<FLOAT>", s)
    s = _INT_RE.sub("<NUM>", s)
    return s


# ============================================================
# 分组签名
# ============================================================

def _signature(entry: dict) -> str:
    """分组签名:component | level | file:line | normalized_message。

    带 file:line 是关键:同一措辞在不同代码行往往是流程链的不同阶段
    (如 certificate_collection_ca.lua 的 import :118 与 :208),
    不带会被错误合并。LAUNCH/UNKNOWN 的 file/line 为 None → 用 ?:? 占位。
    """
    comp = entry.get("component") or "unknown"
    level = entry.get("level") or "UNKNOWN"
    file_ = entry.get("file") or "?"
    line = entry.get("line")
    loc = f"{file_}:{line}" if line is not None else f"{file_}:?"
    norm = normalize_message(entry.get("message") or "")
    return f"{comp}|{level}|{loc}|{norm}"


# ============================================================
# 时间戳解析与间隔格式化
# ============================================================

_TS_FMTS = (
    "%Y-%m-%d %H:%M:%S.%f",  # 解析器产出的标准格式
    "%Y-%m-%d %H:%M:%S",  # 容错:无微秒
)


def _parse_ts(ts):
    """解析时间戳为 datetime;失败/空串返回 None。"""
    if not ts:
        return None
    for fmt in _TS_FMTS:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _fmt_interval(seconds):
    """把秒数格式化为 LLM 友好的间隔串;None → None。"""
    if seconds is None:
        return None
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _median_interval(dts):
    """对 datetime 列表算相邻间隔的中位数;不足两点返回 None。

    用中位数而非均值:能稳健区分「周期重试(1min/10min/60min)」与
    「高频爆发(0.05s)」,不被偶发长跨度拉偏。
    """
    if len(dts) < 2:
        return None
    gaps = [(dts[i + 1] - dts[i]).total_seconds() for i in range(len(dts) - 1)]
    return median(gaps)


# ============================================================
# 分组聚合
# ============================================================

def group_by_pattern(entries: list[dict]) -> list[dict]:
    """把 entries 按模板签名聚合成组列表。

    每组字段:
      - signature: 分组签名(component|level|file:line|normalized_message)
      - count: 该模式触发的次数
      - first_seen / last_seen: 首末时间戳(字符串)
      - interval: 相邻触发的中位间隔(格式化字符串;count<2 或时间戳不可解析 → None)
      - sample_message: 一条代表性原文(首条)
      - sample_messages: 最多 3 条「不同」原文(参数有变化时提示)
      - distinct_count: 该组实际不同原文条数(变体多时引导 LLM 用 dedup=false 下钻)
      - sample_ids: 首+中+尾的 entry id(供 get_context_around 下钻)
      - component / level / file / line: 冗余,便于 LLM 直接引用

    返回顺序:按 count 降序、同频按 first_seen 升序。
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        buckets[_signature(e)].append(e)

    groups = []
    for members in buckets.values():
        # None 时间戳排到末尾,确保首条是真实最早时间戳
        members.sort(key=lambda e: (e.get("timestamp") is None, e.get("timestamp") or ""))

        dts = [t for t in (_parse_ts(m.get("timestamp")) for m in members) if t]
        first_seen = members[0].get("timestamp")
        last_seen = members[-1].get("timestamp")
        interval = _fmt_interval(_median_interval(dts)) if len(dts) >= 2 else None

        # 不同原文(去重,保持出现顺序),最多 3 条
        seen_msgs: list = []
        for m in members:
            raw = m.get("message")
            if raw not in seen_msgs:
                seen_msgs.append(raw)
            if len(seen_msgs) >= 3:
                break
        distinct_count = len({m.get("message") for m in members})

        # 首+中+尾采样 id(覆盖触发首末与中段)
        n = len(members)
        sample_ids = [members[i]["id"] for i in sorted({0, n // 2, n - 1})]

        head = members[0]
        groups.append(
            {
                "count": n,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "interval": interval,
                "sample_message": head.get("message"),
                "sample_messages": seen_msgs,
                "distinct_count": distinct_count,
                "sample_ids": sample_ids,
                "component": head.get("component"),
                "level": head.get("level"),
                "file": head.get("file"),
                "line": head.get("line"),
            }
        )

    groups.sort(key=lambda g: (-g["count"], g["first_seen"] or ""))
    return groups
