"""main.py FastAPI 端点测试 —— TestClient + 模块全局隔离 + 两种 chat 模式

每个 chat 测试都显式替换 main.agent（降级=None / Agent=FakeLLM），绝不触达真实 LLM。
数据目录经 tmp_data fixture 重定向到 tmp_path，不污染真实 backend/data。
"""

import json

import pytest

from conftest import FakeLLMAdapter, create_session


APP_ERR = "2025-07-24 11:00:00.000000 pcie_device ERROR: f.lua(1): boom"
FW_LAUNCH = "1970-01-01 00:00:21.666722 [:t] framework: LAUNCH bootstrap"


# ============================================================
# 隔离 sanity
# ============================================================

def test_isolation_langfuse_disabled(tmp_data):
    # env 强制覆盖生效：langfuse 未启用，端点中的 observation() 全是 no-op，不联网
    assert tmp_data["main"].observability.enabled is False


# ============================================================
# health
# ============================================================

def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert {"llm_available", "agent_available"} <= set(body.keys())


# ============================================================
# /api/parse（非流式）
# ============================================================

def test_parse_upload_returns_summary(client, make_tar_gz, tmp_data):
    data = make_tar_gz(app_text=APP_ERR, framework_text=FW_LAUNCH)
    resp = client.post(
        "/api/parse",
        files={"file": ("dump.tar.gz", data, "application/gzip")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["summary"]["errorCount"] >= 1
    # 原子落盘验证
    assert (tmp_data["datasets"] / f"{body['dataset_id']}.json").exists()


def test_parse_invalid_payload_500(client):
    resp = client.post(
        "/api/parse",
        files={"file": ("bad.bin", b"not a tar.gz", "application/gzip")},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["success"] is False
    assert body["errors"]


# ============================================================
# /api/parse/stream（SSE）
# ============================================================

def test_parse_stream_sse_order(client, make_tar_gz):
    data = make_tar_gz(app_text=APP_ERR, framework_text=FW_LAUNCH)
    resp = client.post(
        "/api/parse/stream",
        files={"file": ("dump.tar.gz", data, "application/gzip")},
    )
    assert resp.status_code == 200
    events = [json.loads(l[5:].strip()) for l in resp.text.splitlines() if l.startswith("data:")]
    types = [e["type"] for e in events]
    # progress(extracting) → progress(parsing) → summary → done
    prog = [i for i, e in enumerate(events) if e["type"] == "progress"]
    assert [events[i]["stage"] for i in prog] == ["extracting", "parsing"]
    assert types.index("summary") > prog[1]
    assert types.index("done") > types.index("summary")


# ============================================================
# /api/datasets
# ============================================================

def test_get_dataset_backfills_component_errors(client, seed_dataset, sample_entries, sample_summary):
    # 旧数据集 summary 缺 componentErrors → 端点按 entries 现场补齐
    summary = {k: v for k, v in sample_summary.items() if k != "componentErrors"}
    did = seed_dataset(sample_entries, summary)
    resp = client.get(f"/api/datasets/{did}")
    assert resp.status_code == 200
    assert resp.json()["summary"]["componentErrors"] == [{"name": "pcie_device", "count": 2}]


def test_get_dataset_entries_level_error(client, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary)
    resp = client.get(f"/api/datasets/{did}/entries?offset=0&limit=2&level=ERROR")
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) <= 2
    assert all(i["level"] == "ERROR" for i in body["items"])


def test_get_dataset_entries_level_all(client, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary)
    resp = client.get(f"/api/datasets/{did}/entries?level=ALL")
    assert resp.json()["total"] == 6  # ALL 不过滤


def test_get_dataset_entries_search_case_insensitive(client, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary)
    resp = client.get(f"/api/datasets/{did}/entries?search=PCIe")
    assert resp.json()["total"] == 2  # 两条 "PCIe card init failed"


def test_get_dataset_entries_404(client):
    resp = client.get("/api/datasets/no-such/entries")
    assert resp.status_code == 404


def test_get_dataset_404(client):
    assert client.get("/api/datasets/no-such").status_code == 404


# ============================================================
# /api/sessions
# ============================================================

def test_create_session_404_when_dataset_missing(client):
    resp = client.post("/api/sessions", json={"dataset_id": "nope"})
    assert resp.status_code == 404


def test_session_crud(client, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary)
    # 创建
    sid = create_session(client, did)
    # 详情
    detail = client.get(f"/api/sessions/{sid}")
    assert detail.status_code == 200
    assert detail.json()["dataset_id"] == did
    # 列表
    listing = client.get("/api/sessions").json()["sessions"]
    assert any(s["session_id"] == sid for s in listing)
    # 删除
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_get_session_404(client):
    assert client.get("/api/sessions/no-such").status_code == 404


# ============================================================
# /api/chat —— 降级模式（agent=None，规则匹配）
# ============================================================

def _seed_and_session(client, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary)
    return did, create_session(client, did)


def test_chat_fallback_overview(client, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(client, seed_dataset, sample_entries, sample_summary)
    resp = client.post("/api/chat", json={"message": "概览", "session_id": sid})
    assert resp.status_code == 200
    assert "日志分析概览" in resp.json()["reply"]
    # 降级模式也会把 user/assistant 消息追加到会话
    msgs = client.get(f"/api/sessions/{sid}").json()["messages"]
    assert any(m["role"] == "user" for m in msgs)
    assert any(m["role"] == "assistant" for m in msgs)


def test_chat_fallback_error_keyword(client, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(client, seed_dataset, sample_entries, sample_summary)
    resp = client.post("/api/chat", json={"message": "错误", "session_id": sid})
    assert "错误分析" in resp.json()["reply"]


def test_chat_fallback_default(client, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(client, seed_dataset, sample_entries, sample_summary)
    resp = client.post("/api/chat", json={"message": "random query xyz", "session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["reply"]  # 非空默认回复


def test_chat_empty_message_400(client, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(client, seed_dataset, sample_entries, sample_summary)
    resp = client.post("/api/chat", json={"message": "   ", "session_id": sid})
    assert resp.status_code == 400


def test_chat_missing_session_404(client, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    resp = client.post("/api/chat", json={"message": "hi", "session_id": "no-such"})
    assert resp.status_code == 404


def test_chat_missing_dataset_404(client, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    # 直接造一个引用不存在 dataset 的 session（绕过 POST 的 dataset 校验）
    session = tmp_data["main"].session_manager.create("nonexistent-dataset")
    resp = client.post("/api/chat", json={"message": "hi", "session_id": session.session_id})
    assert resp.status_code == 404


# ============================================================
# /api/chat —— Agent 模式（FakeLLM，无网络）
# ============================================================

def test_chat_agent_mode_uses_fake_llm(client, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch, tc):
    from agent.core import Agent

    main = tmp_data["main"]
    fake_agent = Agent(
        llm=FakeLLMAdapter([[tc("get_summary", {})], "智能回复"]),
        session_manager=main.session_manager,
        max_iterations=5,
    )
    monkeypatch.setattr(main, "agent", fake_agent)

    did = seed_dataset(sample_entries, sample_summary)
    sid = create_session(client, did)
    resp = client.post("/api/chat", json={"message": "分析", "session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "智能回复"
    assert len(fake_agent.llm.calls) == 2  # 工具一轮 + 最终一轮


def test_chat_agent_run_failure_500(client, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    from agent.core import Agent

    main = tmp_data["main"]
    # 空 responses 队列 → 第一次 chat 即抛 → Agent.run 失败 → 500
    fake_agent = Agent(
        llm=FakeLLMAdapter([]),
        session_manager=main.session_manager,
        max_iterations=5,
    )
    monkeypatch.setattr(main, "agent", fake_agent)

    did = seed_dataset(sample_entries, sample_summary)
    sid = create_session(client, did)
    resp = client.post("/api/chat", json={"message": "分析", "session_id": sid})
    assert resp.status_code == 500


# ============================================================
# /api/chat/stream
# ============================================================

def _sse_events(resp):
    return [json.loads(l[5:].strip()) for l in resp.text.splitlines() if l.startswith("data:")]


def test_chat_stream_agent_mode_order(client, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch, tc):
    from agent.core import Agent

    main = tmp_data["main"]
    fake_agent = Agent(
        llm=FakeLLMAdapter([[tc("get_summary", {})], "最终答案"]),
        session_manager=main.session_manager,
        max_iterations=5,
    )
    monkeypatch.setattr(main, "agent", fake_agent)

    did = seed_dataset(sample_entries, sample_summary)
    sid = create_session(client, did)
    resp = client.post("/api/chat/stream", json={"message": "分析", "session_id": sid})
    events = _sse_events(resp)
    types = [e["type"] for e in events]
    # tool_progress(start/done) → delta(s) → done
    assert "tool_progress" in types
    assert "delta" in types
    assert types[-1] == "done"
    deltas = [e for e in events if e["type"] == "delta"]
    assert "".join(d.get("text", "") for d in deltas).replace(" ", "") == "最终答案"


def test_chat_stream_fallback_word_deltas(client, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    did = seed_dataset(sample_entries, sample_summary)
    sid = create_session(client, did)
    resp = client.post("/api/chat/stream", json={"message": "概览", "session_id": sid})
    events = _sse_events(resp)
    types = [e["type"] for e in events]
    assert "delta" in types
    assert types[-1] == "done"
