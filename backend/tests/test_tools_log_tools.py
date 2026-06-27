"""日志工具单元测试 —— 经 ToolRegistry.execute 调用各工具"""

import json

import agent.tools  # noqa: F401
from agent.tools.registry import ToolRegistry
from agent.dataset import LogDataset


async def _exec(name, args, dataset):
    """执行工具并解析 JSON 结果。"""
    return json.loads(await ToolRegistry.execute(name, args, dataset))


def _big_dataset(n):
    """构造 n 条 pcie_device ERROR，用于验证截断上限。"""
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


# -------------------- search_logs --------------------

async def test_search_logs_case_insensitive(sample_dataset):
    data = await _exec("search_logs", {"keyword": "PCIE"}, sample_dataset)
    assert data["count"] == 2  # 两条 "PCIe card init failed"


async def test_search_logs_cap_100(many_error_entries):
    ds = LogDataset(entries=many_error_entries, summary={})
    data = await _exec("search_logs", {"keyword": "pcie", "max_results": 999}, ds)
    assert data["count"] == 100  # min(999, 100)


async def test_search_logs_default_max_20(many_error_entries):
    ds = LogDataset(entries=many_error_entries, summary={})
    data = await _exec("search_logs", {"keyword": "pcie"}, ds)
    assert data["count"] == 20


# -------------------- filter_by_component --------------------

async def test_filter_by_component_substring_case_insensitive(sample_dataset):
    data = await _exec("filter_by_component", {"component": "PCIE"}, sample_dataset)
    assert data["count"] == 2


async def test_filter_by_component_level_all_skips_filter(sample_dataset):
    # level=ALL 时返回该组件所有级别（不只 ERROR）
    data = await _exec(
        "filter_by_component", {"component": "pcie_device", "level": "ALL"}, sample_dataset
    )
    assert data["count"] == 2
    assert all(r["level"] == "ERROR" for r in data["results"])  # pcie_device 本就只有 ERROR


async def test_filter_by_component_level_exact(sample_dataset):
    # level 精确匹配：filter_by_component 对 sensor 没意义，改用全部组件 + level 过滤
    data = await _exec(
        "filter_by_component", {"component": "pcie", "level": "WARNING"}, sample_dataset
    )
    assert data["count"] == 0  # pcie_device 没有 WARNING


async def test_filter_by_component_cap_200():
    ds = _big_dataset(250)
    data = await _exec(
        "filter_by_component", {"component": "pcie", "max_results": 999}, ds
    )
    assert data["count"] == 200  # min(999, 200)


# -------------------- filter_by_level --------------------

async def test_filter_by_level_exact(sample_dataset):
    data = await _exec("filter_by_level", {"level": "ERROR"}, sample_dataset)
    assert data["count"] == 2
    assert all(r["component"] == "pcie_device" for r in data["results"])


async def test_filter_by_level_cap_200():
    ds = _big_dataset(250)
    data = await _exec("filter_by_level", {"level": "ERROR", "max_results": 999}, ds)
    assert data["count"] == 200


# -------------------- get_summary / get_time_range --------------------

async def test_get_summary_returns_dataset_summary(sample_dataset):
    data = await _exec("get_summary", {}, sample_dataset)
    assert data == sample_dataset.summary


async def test_get_time_range(sample_dataset):
    data = await _exec("get_time_range", {}, sample_dataset)
    assert data == {
        "start": "1970-01-01 00:00:21.666",
        "end": "2025-07-24 11:33:00.000",
    }


# -------------------- get_component_stats --------------------

async def test_get_component_stats_buckets_and_top5(sample_dataset):
    data = await _exec("get_component_stats", {}, sample_dataset)
    comps = {c["component"]: c for c in data["components"]}
    assert comps["pcie_device"]["errors"] == 2
    assert comps["sensor"]["warnings"] == 1
    assert comps["hwproxy"]["notices"] == 1
    assert comps["framework"]["launches"] == 1
    assert comps["unknown"]["other"] == 1  # UNKNOWN entry → OTHER 桶
    # 按错误数降序
    errors = [c["errors"] for c in data["components"]]
    assert errors == sorted(errors, reverse=True)
    # top_error_components 仅含 errors>0
    assert data["top_error_components"] == ["pcie_device"]


# -------------------- get_errors_by_component --------------------

async def test_get_errors_by_component_exact_lowercase(sample_dataset):
    data = await _exec("get_errors_by_component", {"component": "PCIE_Device"}, sample_dataset)
    assert data["count"] == 2
    # 按时间戳排序
    ts = [e["timestamp"] for e in data["errors"]]
    assert ts == sorted(ts)


async def test_get_errors_by_component_no_match(sample_dataset):
    data = await _exec("get_errors_by_component", {"component": "nope"}, sample_dataset)
    assert data["count"] == 0


# -------------------- get_error_timeline --------------------

async def test_get_error_timeline_hour_bucket(sample_dataset):
    data = await _exec("get_error_timeline", {}, sample_dataset)
    # 两条 ERROR 都在 11 点 → 同一小时桶
    assert data["timeline"] == [{"hour": "2025-07-24 11:00", "error_count": 2}]
    assert data["component"] == "all"


async def test_get_error_timeline_component_filter(sample_dataset):
    data = await _exec("get_error_timeline", {"component": "pcie_device"}, sample_dataset)
    assert data["component"] == "pcie_device"
    assert sum(b["error_count"] for b in data["timeline"]) == 2


async def test_get_error_timeline_short_ts_skipped():
    # ERROR 但时间戳过短（len<13）应被跳过
    ds = LogDataset(
        entries=[
            {"id": 1, "timestamp": "2025", "component": "x", "level": "ERROR",
             "message": "m", "file": None, "line": None, "source": "app.log"},
        ],
        summary={},
    )
    data = await _exec("get_error_timeline", {}, ds)
    assert data["timeline"] == []


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
