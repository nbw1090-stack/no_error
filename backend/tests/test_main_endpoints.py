"""main.py FastAPI 端点测试 —— TestClient + 模块全局隔离 + 两种 chat 模式

每个 chat 测试都显式替换 main.agent（降级=None / Agent=FakeLLM），绝不触达真实 LLM。
数据类端点全部需要登录鉴权（authed fixture 提供 Bearer token + 归属用户）；
数据集/会话按用户隔离（seed_dataset 打 user_id，create_session 带鉴权头）。
数据目录经 tmp_data fixture 重定向到 tmp_path，不污染真实 backend/data。
"""

import json

import pytest

from conftest import FakeLLMAdapter, create_session


APP_ERR = "2025-07-24 11:00:00.000000 pcie_device ERROR: f.lua(1): boom"
FW_LAUNCH = "1970-01-01 00:00:21.666722 [:t] framework: LAUNCH bootstrap"


def _register(client, username, password):
    resp = client.post(
        "/api/auth/register", json={"username": username, "password": password}
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return {
        "token": data["token"],
        "headers": {"Authorization": f"Bearer {data['token']}"},
        "user_id": data["user_id"],
    }


# ============================================================
# 隔离 sanity
# ============================================================

def test_isolation_langfuse_disabled(tmp_data):
    # env 强制覆盖生效：langfuse 未启用，端点中的 observation() 全是 no-op，不联网
    assert tmp_data["main"].observability.enabled is False


# ============================================================
# health（无需鉴权）
# ============================================================

def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert {"llm_available", "agent_available"} <= set(body.keys())


# ============================================================
# /api/parse（非流式）—— 需鉴权，归属用户
# ============================================================

def test_parse_requires_auth(client, make_tar_gz):
    data = make_tar_gz(app_text=APP_ERR, framework_text=FW_LAUNCH)
    resp = client.post(
        "/api/parse",
        files={"file": ("dump.tar.gz", data, "application/gzip")},
    )
    assert resp.status_code == 401


def test_parse_upload_returns_summary(client, authed, make_tar_gz, tmp_data):
    data = make_tar_gz(app_text=APP_ERR, framework_text=FW_LAUNCH)
    resp = client.post(
        "/api/parse",
        files={"file": ("dump.tar.gz", data, "application/gzip")},
        headers=authed["headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["summary"]["errorCount"] >= 1
    # 原子落盘 + 打上归属用户
    path = tmp_data["datasets"] / f"{body['dataset_id']}.json"
    assert path.exists()
    saved = json.loads(path.read_text())
    assert saved["user_id"] == authed["user_id"]


def test_parse_invalid_payload_500(client, authed):
    resp = client.post(
        "/api/parse",
        files={"file": ("bad.bin", b"not a tar.gz", "application/gzip")},
        headers=authed["headers"],
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["success"] is False
    assert body["errors"]


# ============================================================
# /api/parse/stream（SSE）—— 需鉴权
# ============================================================

def test_parse_stream_requires_auth(client, make_tar_gz):
    data = make_tar_gz(app_text=APP_ERR, framework_text=FW_LAUNCH)
    resp = client.post(
        "/api/parse/stream",
        files={"file": ("dump.tar.gz", data, "application/gzip")},
    )
    assert resp.status_code == 401


def test_parse_stream_sse_order(client, authed, make_tar_gz):
    data = make_tar_gz(app_text=APP_ERR, framework_text=FW_LAUNCH)
    resp = client.post(
        "/api/parse/stream",
        files={"file": ("dump.tar.gz", data, "application/gzip")},
        headers=authed["headers"],
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
# /api/datasets —— 需鉴权 + 归属校验
# ============================================================

def test_get_dataset_requires_auth(client):
    assert client.get("/api/datasets/whatever").status_code == 401


def test_get_dataset_backfills_component_errors(client, authed, seed_dataset, sample_entries, sample_summary):
    # 旧数据集 summary 缺 componentErrors → 端点按 entries 现场补齐
    summary = {k: v for k, v in sample_summary.items() if k != "componentErrors"}
    did = seed_dataset(sample_entries, summary, authed["user_id"])
    resp = client.get(f"/api/datasets/{did}", headers=authed["headers"])
    assert resp.status_code == 200
    assert resp.json()["summary"]["componentErrors"] == [{"name": "pcie_device", "count": 2}]


def test_get_dataset_entries_level_error(client, authed, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    resp = client.get(
        f"/api/datasets/{did}/entries?offset=0&limit=2&level=ERROR",
        headers=authed["headers"],
    )
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) <= 2
    assert all(i["level"] == "ERROR" for i in body["items"])


def test_get_dataset_entries_level_all(client, authed, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    resp = client.get(
        f"/api/datasets/{did}/entries?level=ALL", headers=authed["headers"]
    )
    assert resp.json()["total"] == 6  # ALL 不过滤


def test_get_dataset_entries_search_case_insensitive(client, authed, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    resp = client.get(
        f"/api/datasets/{did}/entries?search=PCIe", headers=authed["headers"]
    )
    assert resp.json()["total"] == 2  # 两条 "PCIe card init failed"


def test_get_dataset_entries_404(client, authed):
    resp = client.get(
        f"/api/datasets/no-such/entries", headers=authed["headers"]
    )
    assert resp.status_code == 404


def test_get_dataset_404(client, authed):
    assert client.get(
        "/api/datasets/no-such", headers=authed["headers"]
    ).status_code == 404


# ============================================================
# /api/sessions —— 需鉴权 + 归属校验
# ============================================================

def test_sessions_require_auth(client):
    assert client.post("/api/sessions", json={"dataset_id": "x"}).status_code == 401
    assert client.get("/api/sessions").status_code == 401


def test_create_session_404_when_dataset_missing(client, authed):
    resp = client.post(
        "/api/sessions",
        json={"dataset_id": "nope"},
        headers=authed["headers"],
    )
    assert resp.status_code == 404


def test_session_crud(client, authed, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    # 创建
    sid = create_session(client, did, authed["headers"])
    # 详情
    detail = client.get(f"/api/sessions/{sid}", headers=authed["headers"])
    assert detail.status_code == 200
    assert detail.json()["dataset_id"] == did
    # 列表
    listing = client.get(
        "/api/sessions", headers=authed["headers"]
    ).json()["sessions"]
    assert any(s["session_id"] == sid for s in listing)
    # 删除
    assert client.delete(
        f"/api/sessions/{sid}", headers=authed["headers"]
    ).status_code == 200
    assert client.delete(
        f"/api/sessions/{sid}", headers=authed["headers"]
    ).status_code == 404


def test_get_session_404(client, authed):
    assert client.get(
        "/api/sessions/no-such", headers=authed["headers"]
    ).status_code == 404


# ============================================================
# 跨用户隔离：会话 / 数据集 / 对话
# ============================================================

def test_user_data_isolated_between_users(client, authed, seed_dataset, sample_entries, sample_summary):
    alice = {"headers": authed["headers"], "user_id": authed["user_id"]}
    bob = _register(client, "bob", "bbb222")

    # alice 上传 + 建会话
    did = seed_dataset(sample_entries, sample_summary, alice["user_id"])
    sid = create_session(client, did, alice["headers"])

    # bob 看不到 alice 的会话列表 / 详情
    bob_listing = client.get(
        "/api/sessions", headers=bob["headers"]
    ).json()["sessions"]
    assert all(s["session_id"] != sid for s in bob_listing)
    assert client.get(f"/api/sessions/{sid}", headers=bob["headers"]).status_code == 404

    # bob 读 alice 的数据集 → 404（不泄漏存在性）
    assert client.get(f"/api/datasets/{did}", headers=bob["headers"]).status_code == 404
    assert client.get(f"/api/datasets/{did}/entries", headers=bob["headers"]).status_code == 404

    # bob 不能在 alice 的数据集上建会话
    assert client.post(
        "/api/sessions", json={"dataset_id": did}, headers=bob["headers"]
    ).status_code == 404

    # bob 不能和 alice 的会话对话
    assert client.post(
        "/api/chat", json={"message": "hi", "session_id": sid}, headers=bob["headers"]
    ).status_code == 404

    # bob 删除 alice 的会话 → 404，且 alice 的会话仍在
    assert client.delete(f"/api/sessions/{sid}", headers=bob["headers"]).status_code == 404
    assert client.get(f"/api/sessions/{sid}", headers=alice["headers"]).status_code == 200


# ============================================================
# /api/chat 鉴权
# ============================================================

def test_chat_requires_auth(client):
    # 无 token → 401（鉴权先于业务校验）
    resp = client.post("/api/chat", json={"message": "hi", "session_id": "x"})
    assert resp.status_code == 401


def test_chat_stream_requires_auth(client):
    resp = client.post("/api/chat/stream", json={"message": "hi", "session_id": "x"})
    assert resp.status_code == 401


# ============================================================
# /api/chat —— 降级模式（agent=None，规则匹配）
# ============================================================

def _seed_and_session(authed, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    return did, create_session(authed["client"], did, authed["headers"])


def test_chat_fallback_overview(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)
    resp = client.post(
        "/api/chat",
        json={"message": "概览", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 200
    assert "日志分析概览" in resp.json()["reply"]
    # 降级模式也会把 user/assistant 消息追加到会话
    msgs = client.get(f"/api/sessions/{sid}", headers=authed["headers"]).json()["messages"]
    assert any(m["role"] == "user" for m in msgs)
    assert any(m["role"] == "assistant" for m in msgs)


def test_chat_fallback_error_keyword(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)
    resp = client.post(
        "/api/chat",
        json={"message": "错误", "session_id": sid},
        headers=authed["headers"],
    )
    assert "错误分析" in resp.json()["reply"]


def test_chat_fallback_default(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)
    resp = client.post(
        "/api/chat",
        json={"message": "random query xyz", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["reply"]  # 非空默认回复


def test_chat_empty_message_400(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)
    resp = client.post(
        "/api/chat",
        json={"message": "   ", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 400


def test_chat_missing_session_404(client, authed, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    resp = client.post(
        "/api/chat",
        json={"message": "hi", "session_id": "no-such"},
        headers=authed["headers"],
    )
    assert resp.status_code == 404


def test_chat_missing_dataset_404(client, authed, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    # 直接造一个引用不存在 dataset 的 session（绕过 POST 的 dataset 校验），
    # 并归属当前用户，使 chat 能定位到该会话、再因 dataset 缺失返回 404。
    session = tmp_data["main"].session_manager.create(
        "nonexistent-dataset", authed["user_id"], authed["username"]
    )
    resp = client.post(
        "/api/chat",
        json={"message": "hi", "session_id": session.session_id},
        headers=authed["headers"],
    )
    assert resp.status_code == 404


# ============================================================
# /api/chat —— Agent 模式（FakeLLM，无网络）
# ============================================================

def test_chat_agent_mode_uses_fake_llm(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch, tc):
    from agent.core import Agent

    main = tmp_data["main"]
    fake_agent = Agent(
        llm=FakeLLMAdapter([[tc("get_summary", {})], "智能回复"]),
        session_manager=main.session_manager,
        max_iterations=5,
    )
    monkeypatch.setattr(main, "agent", fake_agent)

    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    sid = create_session(client, did, authed["headers"])
    resp = client.post(
        "/api/chat",
        json={"message": "分析", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "智能回复"
    assert len(fake_agent.llm.calls) == 2  # 工具一轮 + 最终一轮


def test_chat_agent_run_failure_500(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    from agent.core import Agent

    main = tmp_data["main"]
    # 空 responses 队列 → 第一次 chat 即抛 → Agent.run 失败 → 500
    fake_agent = Agent(
        llm=FakeLLMAdapter([]),
        session_manager=main.session_manager,
        max_iterations=5,
    )
    monkeypatch.setattr(main, "agent", fake_agent)

    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    sid = create_session(client, did, authed["headers"])
    resp = client.post(
        "/api/chat",
        json={"message": "分析", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 500


# ============================================================
# /api/chat/stream
# ============================================================

def _sse_events(resp):
    return [json.loads(l[5:].strip()) for l in resp.text.splitlines() if l.startswith("data:")]


def test_chat_stream_agent_mode_order(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch, tc):
    from agent.core import Agent

    main = tmp_data["main"]
    fake_agent = Agent(
        llm=FakeLLMAdapter([[tc("get_summary", {})], "最终答案"]),
        session_manager=main.session_manager,
        max_iterations=5,
    )
    monkeypatch.setattr(main, "agent", fake_agent)

    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    sid = create_session(client, did, authed["headers"])
    resp = client.post(
        "/api/chat/stream",
        json={"message": "分析", "session_id": sid},
        headers=authed["headers"],
    )
    events = _sse_events(resp)
    types = [e["type"] for e in events]
    # tool_progress(start/done) → delta(s) → done
    assert "tool_progress" in types
    assert "delta" in types
    assert types[-1] == "done"
    deltas = [e for e in events if e["type"] == "delta"]
    assert "".join(d.get("text", "") for d in deltas).replace(" ", "") == "最终答案"


def test_chat_stream_fallback_word_deltas(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    sid = create_session(client, did, authed["headers"])
    resp = client.post(
        "/api/chat/stream",
        json={"message": "概览", "session_id": sid},
        headers=authed["headers"],
    )
    events = _sse_events(resp)
    types = [e["type"] for e in events]
    assert "delta" in types
    assert types[-1] == "done"
