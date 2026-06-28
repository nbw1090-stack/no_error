"""log_dedup 模块单元测试 —— 归一化正则边界 + 分组聚合逻辑"""

from agent.tools.log_dedup import (
    normalize_message,
    group_by_pattern,
    _signature,
)


# -------------------- normalize_message:正则顺序与边界 --------------------

def test_norm_ip():
    assert normalize_message("connect 192.168.1.10 ok") == "connect <IP> ok"


def test_norm_ip_with_port_not_eaten_into_ip():
    # IP 先整体吃掉,端口 8080 再由 INT 处理,二者独立
    assert normalize_message("connect 192.168.1.10:8080") == "connect <IP>:<NUM>"


def test_norm_hex():
    assert normalize_message("code 0xDEADBEEF") == "code <HEX>"


def test_norm_hex_before_int_order():
    # 0xAB12 里的数字不能先被 INT 吃成 0x<NUM>
    assert normalize_message("0xAB12 fail") == "<HEX> fail"


def test_norm_float():
    assert normalize_message("load 3.14") == "load <FLOAT>"


def test_norm_float_not_break_ip():
    # IP 先替换成 <IP>,残余不影响 FLOAT
    assert normalize_message("192.168.1.1 vs 3.14") == "<IP> vs <FLOAT>"


def test_norm_int_no_word_boundary():
    # Event_Mem17OverTempMajor_01010117 数字紧贴字母,裸 \d+ 才命中(\b 会漏)
    assert (
        normalize_message("Event_Mem17OverTempMajor_01010117 failed")
        == "Event_Mem<NUM>OverTempMajor_<NUM> failed"
    )


def test_norm_pure_alpha_unchanged():
    assert normalize_message("PCIe card oob management init failed.") == (
        "PCIe card oob management init failed."
    )


def test_norm_mixed_order():
    s = normalize_message("ip 10.0.0.1 hex 0xFF float 2.5 int 7 end")
    assert s == "ip <IP> hex <HEX> float <FLOAT> int <NUM> end"


def test_norm_empty_and_none():
    assert normalize_message("") == ""
    assert normalize_message(None) == ""


# -------------------- group_by_pattern:聚合 --------------------

def _e(i, msg, ts, comp="c", level="ERROR", f="a.lua", line=1):
    return {
        "id": i,
        "message": msg,
        "timestamp": ts,
        "component": comp,
        "level": level,
        "file": f,
        "line": line,
        "source": "app.log",
    }


def test_group_same_template_diff_timestamps_merge():
    entries = [
        _e(1, "PCIe card init failed", "2025-07-24 11:00:00.000"),
        _e(2, "PCIe card init failed", "2025-07-24 11:01:00.000"),
        _e(3, "PCIe card init failed", "2025-07-24 11:02:00.000"),
    ]
    groups = group_by_pattern(entries)
    assert len(groups) == 1
    g = groups[0]
    assert g["count"] == 3
    assert g["interval"] == "1.0min"  # 60s 落在 < 3600 分支
    assert g["first_seen"] == "2025-07-24 11:00:00.000"
    assert g["last_seen"] == "2025-07-24 11:02:00.000"
    assert g["sample_ids"] == [1, 2, 3]  # 首+中+尾
    assert g["sample_message"] == "PCIe card init failed"
    assert g["distinct_count"] == 1


def test_group_count_one_interval_none():
    entries = [_e(1, "solo error", "2025-07-24 11:00:00.000")]
    g = group_by_pattern(entries)[0]
    assert g["count"] == 1
    assert g["interval"] is None
    assert g["sample_ids"] == [1]


def test_group_diff_params_same_template_sample_messages():
    # 同模板不同参数(参数即诊断)→ 合并成 1 组,sample_messages 保留 3 种变体
    entries = [
        _e(1, "SlotID=11 failed", "2025-07-24 11:00:00.000"),
        _e(2, "SlotID=12 failed", "2025-07-24 11:00:01.000"),
        _e(3, "SlotID=13 failed", "2025-07-24 11:00:02.000"),
        _e(4, "SlotID=14 failed", "2025-07-24 11:00:03.000"),
    ]
    g = group_by_pattern(entries)[0]
    assert g["count"] == 4
    assert g["distinct_count"] == 4
    assert len(g["sample_messages"]) == 3  # 最多 3 条
    assert g["sample_messages"][0] == "SlotID=11 failed"


def test_group_signature_carries_file_line():
    # 同消息不同 file:line → 不同组(流程链不同步骤不合并)
    entries = [
        _e(1, "ca fail", "2025-07-24 11:00:00.000", f="ca.lua", line=118),
        _e(2, "ca fail", "2025-07-24 11:00:01.000", f="ca.lua", line=208),
    ]
    assert len(group_by_pattern(entries)) == 2


def test_group_unparseable_ts_interval_none():
    # 时间戳解析失败 → 不报错,interval=None
    entries = [
        _e(1, "x", "garbage-ts"),
        _e(2, "x", "also-garbage"),
    ]
    g = group_by_pattern(entries)[0]
    assert g["count"] == 2
    assert g["interval"] is None


def test_group_sorted_by_count_desc():
    entries = [
        _e(1, "rare", "2025-07-24 11:00:00.000"),
        _e(2, "freq", "2025-07-24 11:00:00.000"),
        _e(3, "freq", "2025-07-24 11:00:01.000"),
    ]
    groups = group_by_pattern(entries)
    assert groups[0]["count"] == 2  # 高频组排前
    assert groups[0]["sample_message"] == "freq"


def test_signature_none_file_line_uses_placeholder():
    # LAUNCH/UNKNOWN 的 file/line 为 None → 签名用 ?:? 占位,仍可分组
    entry = {"id": 1, "message": "snlua bootstrap", "timestamp": None,
             "component": "framework", "level": "LAUNCH", "file": None,
             "line": None, "source": "framework.log"}
    sig = _signature(entry)
    assert "|LAUNCH|?:?|" in sig
