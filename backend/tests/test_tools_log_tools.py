"""日志工具单元测试 —— 经 ToolRegistry.execute 调用各工具"""

import json

import agent.tools  # noqa: F401
from agent.tools.registry import ToolRegistry
from agent.dataset import LogDataset


async def _exec(name, args, dataset):
    """执行工具并解析 JSON 结果。"""
    return json.loads(await ToolRegistry.execute(name, args, dataset))


def _big_dataset(n):
    """构造 n 条 pcie_device ERROR，用于验证截断上限。

    每条 line 不同 → 在 dedup 模式下每条独立成组（验证 max_results 限制的是组数）。
    """
    return LogDataset(
        entries=[
            {
                "id": i + 1,
                "timestamp": "2025-07-24 11:00:%05d" % (i % 60),
                "component": "pcie_device",
                "level": "ERROR",
                "file": "f.lua",
                "line": i,
                "message": "PCIe card init failed",
                "source": "app.log",
            }
            for i in range(n)
        ],
        summary={"totalLines": n, "components": ["pcie_device"], "timeRange": {}},
    )


def _repeated_dataset():
    """同模板不同时间戳（应合并）+ 同模板不同参数（应合并但保留多变体）。

    - 3 条 pcie_card.lua:49 同消息、间隔 60s → 1 组，count=3，interval=1.0min
    - 2 条 device_loader.lua:30 同模板不同 SlotID → 1 组，count=2，sample_messages 2 种
    """
    entries = []
    for i, ts in enumerate(
        [
            "2025-07-24 11:00:00.000",
            "2025-07-24 11:01:00.000",
            "2025-07-24 11:02:00.000",
        ]
    ):
        entries.append(
            {
                "id": i + 1,
                "timestamp": ts,
                "component": "pcie_device",
                "level": "ERROR",
                "file": "pcie_card.lua",
                "line": 49,
                "message": "PCIe card oob management init failed.",
                "source": "app.log",
            }
        )
    for i, slot in [(4, 11), (5, 12)]:
        entries.append(
            {
                "id": i,
                "timestamp": "2025-07-24 11:10:00.000",
                "component": "pcie_device",
                "level": "ERROR",
                "file": "device_loader.lua",
                "line": 30,
                "message": f"SlotID={slot} failed",
                "source": "app.log",
            }
        )
    return LogDataset(entries=entries, summary={})


# -------------------- search_logs --------------------

async def test_search_logs_case_insensitive(sample_dataset):
    data = await _exec("search_logs", {"keyword": "PCIE", "dedup": False}, sample_dataset)
    assert data["count"] == 2  # 两条 "PCIe card init failed"


async def test_search_logs_cap_100(many_error_entries):
    ds = LogDataset(entries=many_error_entries, summary={})
    data = await _exec("search_logs", {"keyword": "pcie", "max_results": 999, "dedup": False}, ds)
    assert data["count"] == 100  # min(999, 100)


async def test_search_logs_default_max_20(many_error_entries):
    ds = LogDataset(entries=many_error_entries, summary={})
    data = await _exec("search_logs", {"keyword": "pcie", "dedup": False}, ds)
    assert data["count"] == 20


async def test_search_logs_dedup_default_groups():
    ds = _repeated_dataset()
    data = await _exec("search_logs", {"keyword": "pcie"}, ds)
    assert data["dedup"] is True
    assert data["total_matches"] == 3  # 3 条含 "pcie"
    assert len(data["groups"]) == 1
    g = data["groups"][0]
    assert g["count"] == 3
    assert g["interval"] == "1.0min"
    assert g["sample_ids"] == [1, 2, 3]


async def test_search_logs_dedup_param_variants_sample_messages():
    ds = _repeated_dataset()
    data = await _exec("search_logs", {"keyword": "SlotID"}, ds)
    g = data["groups"][0]
    assert g["count"] == 2
    assert g["distinct_count"] == 2
    assert len(g["sample_messages"]) == 2  # 两种参数变体都保留


async def test_search_logs_dedup_false_falls_back_flat():
    ds = _repeated_dataset()
    data = await _exec("search_logs", {"keyword": "pcie", "dedup": False}, ds)
    assert data["dedup"] is False
    assert "results" in data and "groups" not in data
    assert data["count"] == 3


async def test_search_logs_dedup_max_results_is_group_count():
    # _big_dataset 每条 line 不同 → 每条独立成组；max_results 限制组数
    ds = _big_dataset(130)
    data = await _exec("search_logs", {"keyword": "PCIe", "max_results": 5}, ds)
    assert data["total_matches"] == 130
    assert len(data["groups"]) <= 5  # 组数被裁，不是条数


# -------------------- filter_by_component --------------------

async def test_filter_by_component_substring_case_insensitive(sample_dataset):
    data = await _exec("filter_by_component", {"component": "PCIE", "dedup": False}, sample_dataset)
    assert data["count"] == 2


async def test_filter_by_component_level_all_skips_filter(sample_dataset):
    # level=ALL 时返回该组件所有级别（不只 ERROR）
    data = await _exec(
        "filter_by_component", {"component": "pcie_device", "level": "ALL", "dedup": False}, sample_dataset
    )
    assert data["count"] == 2
    assert all(r["level"] == "ERROR" for r in data["results"])  # pcie_device 本就只有 ERROR


async def test_filter_by_component_level_exact(sample_dataset):
    # level 精确匹配：filter_by_component 对 sensor 没意义，改用全部组件 + level 过滤
    data = await _exec(
        "filter_by_component", {"component": "pcie", "level": "WARNING", "dedup": False}, sample_dataset
    )
    assert data["count"] == 0  # pcie_device 没有 WARNING


async def test_filter_by_component_cap_200():
    ds = _big_dataset(250)
    data = await _exec(
        "filter_by_component", {"component": "pcie", "max_results": 999, "dedup": False}, ds
    )
    assert data["count"] == 200  # min(999, 200)


async def test_filter_by_component_dedup_groups():
    ds = _repeated_dataset()
    data = await _exec("filter_by_component", {"component": "pcie_device"}, ds)
    assert data["dedup"] is True
    assert data["total_matches"] == 5
    assert len(data["groups"]) == 2  # pcie_card.lua:49 / device_loader.lua:30
    assert data["groups"][0]["count"] == 3  # 高频组在前


# -------------------- filter_by_level --------------------

async def test_filter_by_level_exact(sample_dataset):
    data = await _exec("filter_by_level", {"level": "ERROR", "dedup": False}, sample_dataset)
    assert data["count"] == 2
    assert all(r["component"] == "pcie_device" for r in data["results"])


async def test_filter_by_level_cap_200():
    ds = _big_dataset(250)
    data = await _exec("filter_by_level", {"level": "ERROR", "max_results": 999, "dedup": False}, ds)
    assert data["count"] == 200


async def test_filter_by_level_dedup_groups():
    ds = _repeated_dataset()
    data = await _exec("filter_by_level", {"level": "ERROR"}, ds)
    assert data["dedup"] is True
    assert data["total_matches"] == 5
    assert len(data["groups"]) == 2


# -------------------- get_summary（合并了原 component_stats） --------------------

async def test_get_summary_overall_counts(sample_dataset):
    data = await _exec("get_summary", {}, sample_dataset)
    assert data["total_lines"] == 6
    assert data["errors"] == 2
    assert data["warnings"] == 1
    assert data["notices"] == 1
    assert data["launches"] == 1
    assert data["time_range"] == {
        "start": "1970-01-01 00:00:21.666",
        "end": "2025-07-24 11:33:00.000",
    }
    assert data["total_components"] == 5  # pcie_device/sensor/hwproxy/framework/unknown


async def test_get_summary_per_component_breakdown(sample_dataset):
    data = await _exec("get_summary", {}, sample_dataset)
    comps = {c["component"]: c for c in data["components"]}
    assert comps["pcie_device"]["errors"] == 2
    assert comps["sensor"]["warnings"] == 1
    assert comps["hwproxy"]["notices"] == 1
    assert comps["framework"]["launches"] == 1
    # 仅保留非零级别键：UNKNOWN 条目只进 OTHER 桶 → 无级别键，仅 total
    assert "errors" not in comps["unknown"]
    assert comps["unknown"]["total"] == 1
    # 按错误数降序，错误最多的组件在最前
    assert data["components"][0]["component"] == "pcie_device"
    # top_error_components 仅含 errors>0
    assert data["top_error_components"] == ["pcie_device"]


async def test_get_summary_max_components_caps_list(sample_dataset):
    data = await _exec("get_summary", {"max_components": 2}, sample_dataset)
    assert len(data["components"]) == 2  # 列表被裁
    assert data["total_components"] == 5  # 但总数仍如实报告


# -------------------- get_time_range --------------------

async def test_get_time_range(sample_dataset):
    data = await _exec("get_time_range", {}, sample_dataset)
    assert data == {
        "start": "1970-01-01 00:00:21.666",
        "end": "2025-07-24 11:33:00.000",
    }


# -------------------- get_errors_by_component --------------------

async def test_get_errors_by_component_exact_lowercase(sample_dataset):
    data = await _exec("get_errors_by_component", {"component": "PCIE_Device", "dedup": False}, sample_dataset)
    assert data["count"] == 2
    # 按时间戳排序
    ts = [e["timestamp"] for e in data["errors"]]
    assert ts == sorted(ts)


async def test_get_errors_by_component_no_match(sample_dataset):
    data = await _exec("get_errors_by_component", {"component": "nope", "dedup": False}, sample_dataset)
    assert data["count"] == 0


async def test_get_errors_by_component_dedup_aggregates():
    ds = _repeated_dataset()
    data = await _exec("get_errors_by_component", {"component": "pcie_device"}, ds)
    assert data["dedup"] is True
    assert data["total_matches"] == 5
    assert len(data["groups"]) == 2  # 两个模板
    assert data["groups"][0]["count"] == 3  # 高频组在前


async def test_get_errors_by_component_dedup_false_keeps_errors():
    ds = _repeated_dataset()
    data = await _exec("get_errors_by_component", {"component": "pcie_device", "dedup": False}, ds)
    assert data["dedup"] is False
    assert data["count"] == 5
    assert "errors" in data


# -------------------- get_error_digest（按内容去重的错误速览） --------------------

async def test_get_error_digest_dedups_by_template():
    # 3 条同模板（仅时间不同）应合并成 1 组，count=3，仅 1 个去重模式
    ds = _repeated_dataset()  # 5 条 ERROR：3 同模板 + 2 同模板（参数变体）
    data = await _exec("get_error_digest", {}, ds)
    assert data["component"] == "all"
    assert data["total_errors"] == 5
    assert data["distinct_patterns"] == 2  # pcie_card.lua:49 / device_loader.lua:30
    assert len(data["groups"]) == 2
    g = data["groups"][0]  # 高频组在前
    assert g["count"] == 3
    assert g["interval"] == "1.0min"
    assert g["sample_ids"] == [1, 2, 3]


async def test_get_error_digest_component_filter(sample_dataset):
    data = await _exec("get_error_digest", {"component": "pcie_device"}, sample_dataset)
    assert data["component"] == "pcie_device"
    assert data["total_errors"] == 2


async def test_get_error_digest_max_results_caps_groups():
    # _big_dataset 每条 line 不同 → 每条独立成组；max_results 限制返回组数
    ds = _big_dataset(130)
    data = await _exec("get_error_digest", {"max_results": 5}, ds)
    assert data["total_errors"] == 130
    assert data["distinct_patterns"] == 130  # 真实去重模式数（裁剪前）
    assert len(data["groups"]) == 5  # 返回被裁到 5 组


# -------------------- get_context_around --------------------

async def test_get_context_around_target_found(sample_dataset):
    data = await _exec("get_context_around", {"entry_id": 2, "before": 1, "after": 1}, sample_dataset)
    assert data["total_returned"] == 3
    ids = [e["id"] for e in data["entries"]]
    assert ids == [1, 2, 3]
    assert [e["is_target"] for e in data["entries"]] == [False, True, False]


async def test_get_context_around_target_not_found(sample_dataset):
    data = await _exec("get_context_around", {"entry_id": 999}, sample_dataset)
    assert data == {"error": "Entry with id 999 not found"}


async def test_get_context_around_clamps_at_list_edges(sample_dataset):
    # 开头：before 超出范围，start clamp 到 0
    head = await _exec("get_context_around", {"entry_id": 1, "before": 5, "after": 1}, sample_dataset)
    assert [e["id"] for e in head["entries"]] == [1, 2]
    # 结尾：after 超出范围，end clamp 到 len
    tail = await _exec("get_context_around", {"entry_id": 6, "before": 1, "after": 5}, sample_dataset)
    assert [e["id"] for e in tail["entries"]] == [5, 6]


async def test_get_context_around_caps_before_after_at_20():
    ds = _big_dataset(130)
    # target 在中间(index 50)，before/after 远超 20 → 各截断到 20，共 41 条
    data = await _exec(
        "get_context_around", {"entry_id": 51, "before": 999, "after": 999}, ds
    )
    assert data["total_returned"] == 41
