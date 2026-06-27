"""parser.parse_logs 单元测试 —— 双正则解析 + summary 统计"""

from parser import parse_logs


def test_standard_format_error():
    app = "2025-07-24 11:31:13.714532 pcie_device ERROR: pcie_card.lua(49): PCIe card init failed"
    result = parse_logs(app, "")
    e = result["entries"][0]
    assert e["timestamp"] == "2025-07-24 11:31:13.714532"
    assert e["component"] == "pcie_device"
    assert e["level"] == "ERROR"
    assert e["file"] == "pcie_card.lua"
    assert e["line"] == 49  # int
    assert e["message"] == "PCIe card init failed"
    assert e["source"] == "app.log"
    assert result["summary"]["errorCount"] == 1


def test_warn_normalized_to_warning():
    app = "2025-07-24 11:31:13.714532 sensor WARN: sensor.lua(10): temp high"
    result = parse_logs(app, "")
    assert result["entries"][0]["level"] == "WARNING"
    assert result["summary"]["warningCount"] == 1
    assert result["summary"]["errorCount"] == 0


def test_launch_pattern_framework():
    fw = "1970-01-01 00:00:21.666722 [:00000002] framework: LAUNCH snlua bootstrap"
    result = parse_logs("", fw)
    e = result["entries"][0]
    assert e["level"] == "LAUNCH"
    assert e["component"] == "framework"
    assert e["file"] is None
    assert e["line"] is None
    assert e["message"] == "snlua bootstrap"
    assert e["source"] == "framework.log"
    assert result["summary"]["launchCount"] == 1


def test_unknown_line_kept():
    app = "this line matches no known format ===!!!"
    result = parse_logs(app, "")
    e = result["entries"][0]
    assert e["level"] == "UNKNOWN"
    assert e["message"] == "this line matches no known format ===!!!"
    assert e["timestamp"] is None
    assert result["summary"]["unknownCount"] == 1


def test_empty_inputs_return_empty_summary():
    result = parse_logs("", "")
    assert result["entries"] == []
    s = result["summary"]
    assert s["totalLines"] == 0
    assert s["errorCount"] == 0
    assert s["warningCount"] == 0
    assert s["components"] == []
    assert s["componentErrors"] == []
    assert s["timeRange"] == {"start": None, "end": None}


def test_blank_lines_skipped():
    app = "\n\n  \n2025-07-24 11:31:13.714532 x ERROR: f.lua(1): msg\n\n"
    result = parse_logs(app, "")
    assert len(result["entries"]) == 1


def test_component_errors_top20_desc_and_excludes_non_error():
    # 22 个不同组件，comp_i 有 (22-i) 个 ERROR，确保 Top-20 截断且降序
    lines = []
    for i in range(22):
        comp = f"comp_{i:02d}"
        for _ in range(22 - i):
            lines.append(
                f"2025-07-24 11:00:00.000000 {comp} ERROR: f.lua(1): boom"
            )
    # 加一条 WARNING（不应计入 componentErrors）
    lines.append("2025-07-24 11:00:00.000000 warn_comp WARNING: f.lua(1): w")
    result = parse_logs("\n".join(lines), "")
    ce = result["summary"]["componentErrors"]
    assert len(ce) == 20  # Top-20 截断
    # 降序
    counts = [c["count"] for c in ce]
    assert counts == sorted(counts, reverse=True)
    assert ce[0]["name"] == "comp_00" and ce[0]["count"] == 22
    # WARNING 组件不计入
    assert all(c["name"] != "warn_comp" for c in ce)


def test_components_sorted_ascending():
    app = (
        "2025-07-24 11:00:00.000000 zeta ERROR: f.lua(1): a\n"
        "2025-07-24 11:00:00.000000 alpha ERROR: f.lua(1): b\n"
        "2025-07-24 11:00:00.000000 mu ERROR: f.lua(1): c\n"
    )
    comps = parse_logs(app, "")["summary"]["components"]
    assert comps == ["alpha", "mu", "zeta"]


def test_ids_increment_across_both_files():
    app = "2025-07-24 11:00:00.000000 a ERROR: f.lua(1): x\n2025-07-24 11:00:00.000000 b ERROR: f.lua(1): y"
    fw = "1970-01-01 00:00:21.666722 [:t] framework: LAUNCH z"
    entries = parse_logs(app, fw)["entries"]
    assert [e["id"] for e in entries] == [1, 2, 3]
    assert entries[2]["source"] == "framework.log"


def test_timerange_min_max():
    app = (
        "2025-07-24 11:00:00.000000 a ERROR: f.lua(1): x\n"
        "2025-07-24 11:00:05.000000 b ERROR: f.lua(1): y\n"
        "2025-07-24 11:00:03.000000 c ERROR: f.lua(1): z\n"
    )
    tr = parse_logs(app, "")["summary"]["timeRange"]
    assert tr["start"] == "2025-07-24 11:00:00.000000"
    assert tr["end"] == "2025-07-24 11:00:05.000000"


def test_summary_keys_complete():
    s = parse_logs("", "")["summary"]
    for key in (
        "totalLines",
        "errorCount",
        "warningCount",
        "noticeCount",
        "launchCount",
        "unknownCount",
        "components",
        "componentErrors",
        "timeRange",
    ):
        assert key in s


def test_notice_count():
    app = "2025-07-24 11:00:00.000000 a NOTICE: f.lua(1): n"
    assert parse_logs(app, "")["summary"]["noticeCount"] == 1
