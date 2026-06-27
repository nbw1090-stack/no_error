"""LogDataset 属性映射单元测试"""

from agent.dataset import LogDataset


def test_property_accessors_map_summary(sample_summary, sample_entries):
    ds = LogDataset(entries=sample_entries, summary=sample_summary)
    assert ds.total_lines == 6
    assert ds.error_count == 2
    assert ds.warning_count == 1
    assert ds.notice_count == 1
    assert ds.launch_count == 1
    assert ds.components == ["framework", "hwproxy", "pcie_device", "sensor"]
    assert ds.time_start == "1970-01-01 00:00:21.666"
    assert ds.time_end == "2025-07-24 11:33:00.000"


def test_empty_summary_defaults():
    ds = LogDataset(entries=[], summary={})
    assert ds.total_lines == 0
    assert ds.error_count == 0
    assert ds.warning_count == 0
    assert ds.notice_count == 0
    assert ds.launch_count == 0
    assert ds.components == []
    assert ds.time_start is None
    assert ds.time_end is None
