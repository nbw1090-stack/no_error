"""检索子 Agent（多 Agent 架构）测试 —— FakeLLMAdapter 打桩，零网络。

覆盖四层：
1. RetrievalSubAgent 小 ReAct 循环：摘要+出处契约、轮次封顶、空手而归、截断。
2. retrieve_evidence 工具：未配置降级、空 query、配置后转交。
3. 架构开关：Agent(source_mode=...) 决定暴露 retrieval 还是 source/wiki 工具组。
4. 评测适配：build_system_prompt subagent 段、trajectory 拉平子 Agent 轨迹。
"""

import json

import pytest

import agent.tools  # noqa: F401  # 导入即注册（含 retrieve_evidence）
from agent.core import Agent
from agent.dataset import LogDataset
from agent.prompts.system import build_system_prompt
from agent.session import SessionManager
from agent.subagent import RetrievalSubAgent
from agent.tools.registry import ToolRegistry
from agent.tools import retrieval_tool

from conftest import FakeLLMAdapter, make_tool_call

_COMPS = [{"name": "pcie_device", "file_count": 187, "symbol_count": 4559}]


@pytest.fixture
def user_dataset(sample_dataset):
    """带 user_id 的数据集（源码/wiki 工具执行需要）。"""
    return LogDataset(
        entries=sample_dataset.entries,
        summary=sample_dataset.summary,
        user_id=1,
    )


@pytest.fixture
def sub_sources(monkeypatch):
    """把子 Agent 的证据源钉死为：1 个已索引组件 + wiki 可用。"""

    async def fake_comps(user_id):
        return list(_COMPS) if user_id else []

    async def fake_wiki():
        return True

    monkeypatch.setattr("agent.subagent._load_source_components", fake_comps)
    monkeypatch.setattr("agent.subagent._wiki_available", fake_wiki)


def _fake_execute(result: dict):
    """替换 ToolRegistry.execute：固定返回给定 JSON（记录调用供断言）。"""
    calls = []

    async def execute(name, arguments, dataset):
        calls.append({"name": name, "args": arguments})
        return json.dumps(result, ensure_ascii=False)

    return execute, calls


# ============================================================
# 1. RetrievalSubAgent 循环
# ============================================================

async def test_subagent_summary_citations_stats(
    monkeypatch, sub_sources, user_dataset
):
    """取证一次 → 摘要 + 从工具结果抽出处 + 健康统计。"""
    execute, calls = _fake_execute(
        {
            "component": "pcie_device",
            "target": {
                "rel_path": "src/pcie_card.lua",
                "function": "pcie_oob_mgmt_init",
                "start_line": 41,
            },
        }
    )
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    llm = FakeLLMAdapter(
        [
            [make_tool_call("gather_code_context", {"component": "pcie_device"})],
            "根因在 pcie_oob_mgmt_init。出处：src/pcie_card.lua:41",
        ],
        usages=[{"input": 100, "output": 20, "total": 120}, {"input": 200, "output": 30, "total": 230}],
    )
    sub = RetrievalSubAgent(llm, max_iterations=6)
    out = await sub.run("查 pcie_card.lua:49 的报错根因", user_dataset)

    assert "pcie_oob_mgmt_init" in out["summary"]
    assert "pcie_device/src/pcie_card.lua:41" in out["citations"]
    assert calls[0]["name"] == "gather_code_context"
    assert out["stats"] == {
        "rounds": 2,
        "tool_calls": 1,
        "tools_used": ["gather_code_context"],
        "empty_handed": False,
    }
    assert out["usage"]["total"] == 350
    # 无状态：不落 session，消息只活在调用内（无从断言持久化，靠上面契约字段）


async def test_subagent_no_sources_returns_not_found(monkeypatch, user_dataset):
    async def none_comps(user_id):
        return []

    async def no_wiki():
        return False

    monkeypatch.setattr("agent.subagent._load_source_components", none_comps)
    monkeypatch.setattr("agent.subagent._wiki_available", no_wiki)
    sub = RetrievalSubAgent(FakeLLMAdapter([]))
    out = await sub.run("查点什么", user_dataset)
    assert "未找到" in out["summary"]
    assert out["stats"]["empty_handed"] is True
    assert out["stats"]["rounds"] == 0


async def test_subagent_iteration_cap_forces_no_tools(
    monkeypatch, sub_sources, user_dataset
):
    """轮次封顶：最后一轮不提供工具；一直要工具则兜底'未产出摘要'。"""
    execute, _ = _fake_execute({"results": [], "matches_count": 0})
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    llm = FakeLLMAdapter(
        [
            [make_tool_call("search_symbols", {"component": "a", "name": "x"}, "c1")],
            [make_tool_call("search_symbols", {"component": "b", "name": "y"}, "c2")],
            [make_tool_call("search_symbols", {"component": "c", "name": "z"}, "c3")],
        ]
    )
    sub = RetrievalSubAgent(llm, max_iterations=3)
    out = await sub.run("query", user_dataset)

    # 最后一轮 tools 必须为空列表（禁工具逼总结）
    assert llm.calls[-1]["tools"] == []
    # 最后一轮返回的 tool_calls 不执行，直接走"无摘要"兜底
    assert "未找到" in out["summary"]
    assert out["stats"]["rounds"] == 3
    assert out["stats"]["tool_calls"] == 2  # 第三轮的调用未执行
    assert out["stats"]["empty_handed"] is True  # 全部空结果


async def test_subagent_truncates_long_summary(
    monkeypatch, sub_sources, user_dataset
):
    long_text = "结" * 1000
    sub = RetrievalSubAgent(FakeLLMAdapter([long_text]), summary_max_chars=200)
    out = await sub.run("q", user_dataset)
    assert len(out["summary"]) < 300
    assert "已截断" in out["summary"]


async def test_subagent_llm_failure_degrades(monkeypatch, sub_sources, user_dataset):
    class BoomLLM:
        async def chat(self, messages, tools=None):
            raise RuntimeError("api down")

    sub = RetrievalSubAgent(BoomLLM())
    out = await sub.run("q", user_dataset)
    assert "LLM 调用失败" in out["summary"]
    assert out["stats"]["empty_handed"] is True


async def test_subagent_wiki_citation(monkeypatch, sub_sources, user_dataset):
    execute, _ = _fake_execute(
        {"slug": "mdb-model", "title": "MDB 模型", "content": "..."}
    )
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    llm = FakeLLMAdapter(
        [[make_tool_call("read_wiki_page", {"slug": "mdb-model"})], "见 wiki"]
    )
    out = await RetrievalSubAgent(llm).run("查 mdb 设计", user_dataset)
    assert "wiki:mdb-model" in out["citations"]


# ============================================================
# 2. retrieve_evidence 工具
# ============================================================

async def test_retrieve_evidence_unconfigured(user_dataset):
    retrieval_tool.configure_retrieval(None)
    out = await retrieval_tool.retrieve_evidence(user_dataset, query="q")
    assert "未配置" in out["error"]


async def test_retrieve_evidence_empty_query(user_dataset):
    retrieval_tool.configure_retrieval(FakeLLMAdapter([]))
    try:
        out = await retrieval_tool.retrieve_evidence(user_dataset, query="  ")
        assert out["error"] == "缺少 query"
    finally:
        retrieval_tool.configure_retrieval(None)


async def test_retrieve_evidence_configured_delegates(
    monkeypatch, sub_sources, user_dataset
):
    retrieval_tool.configure_retrieval(FakeLLMAdapter(["查到了。出处：无"]))
    try:
        out = await retrieval_tool.retrieve_evidence(user_dataset, query="查一下")
        assert out["summary"].startswith("查到了")
        assert "guidance" in out
        assert out["stats"]["rounds"] == 1
    finally:
        retrieval_tool.configure_retrieval(None)


# ============================================================
# 3. 架构开关：工具暴露
# ============================================================

def _patch_availability(monkeypatch):
    """主 Agent 侧：钉死已索引组件 + wiki 可用（core 模块命名空间）。"""

    async def fake_comps(user_id):
        return list(_COMPS)

    async def fake_wiki():
        return True

    monkeypatch.setattr("agent.core._load_source_components", fake_comps)
    monkeypatch.setattr("agent.core._wiki_available", fake_wiki)


def _offered_tool_names(llm_call) -> set[str]:
    return {t["function"]["name"] for t in (llm_call["tools"] or [])}


async def test_agent_subagent_mode_exposes_retrieval_only(
    tmp_path, sample_dataset, monkeypatch
):
    _patch_availability(monkeypatch)
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds", user_id=1)
    agent = Agent(
        llm=FakeLLMAdapter(["ok"]),
        session_manager=sm,
        source_mode="subagent",
    )
    await agent.run(session.session_id, "hi", sample_dataset)
    offered = _offered_tool_names(agent.llm.calls[0])
    assert "retrieve_evidence" in offered
    assert "get_summary" in offered  # log 工具保持直连
    assert not offered & set(ToolRegistry.get_names(groups={"source", "wiki"}))
    # system prompt 走本地 subagent 版
    sys_prompt = agent.llm.calls[0]["messages"][0]["content"]
    assert "retrieve_evidence" in sys_prompt


async def test_agent_direct_mode_unchanged(tmp_path, sample_dataset, monkeypatch):
    _patch_availability(monkeypatch)
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds", user_id=1)
    agent = Agent(llm=FakeLLMAdapter(["ok"]), session_manager=sm)  # 默认 direct
    await agent.run(session.session_id, "hi", sample_dataset)
    offered = _offered_tool_names(agent.llm.calls[0])
    assert "retrieve_evidence" not in offered
    assert "gather_code_context" in offered and "get_wiki_index" in offered


async def test_agent_subagent_mode_without_sources_no_retrieval(
    tmp_path, sample_dataset, monkeypatch
):
    async def none_comps(user_id):
        return []

    async def no_wiki():
        return False

    monkeypatch.setattr("agent.core._load_source_components", none_comps)
    monkeypatch.setattr("agent.core._wiki_available", no_wiki)
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds", user_id=1)
    agent = Agent(
        llm=FakeLLMAdapter(["ok"]), session_manager=sm, source_mode="subagent"
    )
    await agent.run(session.session_id, "hi", sample_dataset)
    assert "retrieve_evidence" not in _offered_tool_names(agent.llm.calls[0])


# ============================================================
# 4. 评测适配：prompt 段 + 轨迹拉平
# ============================================================

def test_build_system_prompt_subagent_mode(sample_summary):
    p = build_system_prompt(
        sample_summary, _COMPS, wiki_available=True, source_mode="subagent"
    )
    assert "retrieve_evidence" in p
    assert "trace_call_chain" not in p  # 直连源码工具指导不应出现
    assert "get_wiki_index" not in p
    # direct 模式不受影响
    p2 = build_system_prompt(sample_summary, _COMPS, wiki_available=True)
    assert "retrieve_evidence" not in p2 and "trace_call_chain" in p2


def test_trajectory_flattens_subagent(tmp_path):
    from eval.trajectory import extract_trajectory

    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create("ds", user_id=1)
    sm.add_message(session.session_id, {"role": "user", "content": "q"})
    sm.add_message(
        session.session_id,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                make_tool_call("retrieve_evidence", {"query": "查根因"}, "c1")
            ],
        },
    )
    sm.add_message(
        session.session_id,
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": json.dumps(
                {
                    "summary": "s",
                    "citations": ["pcie_device/src/a.lua:1"],
                    "stats": {
                        "rounds": 3,
                        "tool_calls": 2,
                        "tools_used": ["trace_call_chain", "gather_code_context"],
                        "empty_handed": False,
                    },
                    "usage": {"input": 1, "output": 2, "total": 300},
                },
                ensure_ascii=False,
            ),
        },
    )
    sm.add_message(session.session_id, {"role": "assistant", "content": "done"})

    traj = extract_trajectory(sm, session.session_id)
    assert traj["tool_call_count"] == 1
    assert "gather_code_context" in traj["tool_names"]  # 拉平进来
    assert traj["sub_calls"] == 1
    assert traj["sub_rounds"] == 3
    assert traj["sub_tool_calls"] == 2
    assert traj["sub_empty_handed"] == 0
    assert traj["sub_usage_total"] == 300


def test_evaluators_subagent_metrics():
    from eval.evaluators import total_tokens, subagent_rounds, subagent_empty_handed

    output = {
        "reply": "r",
        "usage": {"input": 1, "output": 2, "total": 1000},
        "trajectory": {
            "tool_calls": [],
            "tool_names": ["retrieve_evidence", "gather_code_context"],
            "tool_call_count": 1,
            "iterations": 2,
            "duplicate_calls": 0,
            "sub_calls": 2,
            "sub_rounds": 5,
            "sub_tool_calls": 3,
            "sub_empty_handed": 1,
            "sub_usage_total": 500,
        },
    }
    assert total_tokens(input={}, output=output, expected_output=None).value == 1500.0
    assert subagent_rounds(input={}, output=output, expected_output=None).value == 2.5
    assert (
        subagent_empty_handed(input={}, output=output, expected_output=None).value
        == 0.5
    )
    # 单 Agent（无子调用）→ 健康指标不适用
    single = {"reply": "r", "trajectory": {**output["trajectory"], "sub_calls": 0}}
    assert subagent_rounds(input={}, output=single, expected_output=None).value is None
