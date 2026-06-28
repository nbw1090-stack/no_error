"""POST /api/feedback 端点测试 —— 归属校验 + score 调用 + trace_id 回传

全部在 LANGFUSE_ENABLED=false 下运行：ObservabilityClient.enabled=False，
真实 score() no-op 不联网；归属校验逻辑与 score 入参是本测试重点，用记录型
fake tracer 验证 score 调用而非真实上报。chat 走降级模式（agent=None），
不依赖 LLM。
"""

import json

from conftest import create_session


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


class _FakeTracer:
    """记录 score() 调用的假 tracer（替代 LANGFUSE 关闭时的 no-op 客户端）。"""

    def __init__(self):
        self.scored = []

    def score(self, **kwargs):
        self.scored.append(kwargs)

    def flush(self):
        pass


def _seed_and_session(authed, seed_dataset, sample_entries, sample_summary):
    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    return did, create_session(authed["client"], did, authed["headers"])


def _sse_events(resp):
    return [json.loads(l[5:].strip()) for l in resp.text.splitlines() if l.startswith("data:")]


# ============================================================
# 鉴权
# ============================================================
def test_feedback_requires_auth(client):
    resp = client.post(
        "/api/feedback",
        json={"trace_id": "abc", "session_id": "x", "feedback": "like"},
    )
    assert resp.status_code == 401


# ============================================================
# trace_id 回传（chat 响应 / 流式 done）
# ============================================================
def test_chat_response_carries_trace_id(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    """降级 chat 响应回传 trace_id（前端用它关联点赞/踩）。"""
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)
    resp = client.post(
        "/api/chat",
        json={"message": "概览", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["trace_id"]  # 非空


def test_chat_stream_done_carries_trace_id(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    """流式 done 事件回传 trace_id，前端据此挂到那条 assistant 消息。"""
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)
    resp = client.post(
        "/api/chat/stream",
        json={"message": "概览", "session_id": sid},
        headers=authed["headers"],
    )
    events = _sse_events(resp)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["trace_id"]  # 非空


# ============================================================
# 归属校验
# ============================================================
def test_feedback_session_not_owned_404(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    """他人会话 → session_manager.get 归属校验失败 → 404（不泄漏存在性）。"""
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    bob = _register(client, "bob", "bbb222")
    bob_did = seed_dataset(sample_entries, sample_summary, bob["user_id"])
    bob_sid = create_session(client, bob_did, bob["headers"])

    resp = client.post(
        "/api/feedback",
        json={"trace_id": "whatever", "session_id": bob_sid, "feedback": "like"},
        headers=authed["headers"],
    )
    assert resp.status_code == 404


def test_feedback_trace_not_in_session_404(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    """合法 session 但 trace_id 不属于它 → 404（防伪造打分）。"""
    monkeypatch.setattr(tmp_data["main"], "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)

    resp = client.post(
        "/api/feedback",
        json={"trace_id": "0" * 32, "session_id": sid, "feedback": "dislike"},
        headers=authed["headers"],
    )
    assert resp.status_code == 404


def test_feedback_invalid_kind_422(client, authed):
    """feedback 非 like/dislike → Pydantic Literal 校验失败 422（先于业务校验）。"""
    resp = client.post(
        "/api/feedback",
        json={"trace_id": "x", "session_id": "y", "feedback": "meh"},
        headers=authed["headers"],
    )
    assert resp.status_code == 422


# ============================================================
# 正常路径 + score 调用
# ============================================================
def test_feedback_ok_and_scores(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    """合法 feedback → 200，且用幂等键正确调用 tracer.score。"""
    main = tmp_data["main"]
    monkeypatch.setattr(main, "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)

    # chat 得到本轮 trace_id（record_trace 已写入 session.traces）
    chat = client.post(
        "/api/chat",
        json={"message": "概览", "session_id": sid},
        headers=authed["headers"],
    )
    trace_id = chat.json()["trace_id"]

    # 替换为记录型 tracer，验证 score 入参
    fake = _FakeTracer()
    monkeypatch.setattr(main, "get_client", lambda: fake)

    resp = client.post(
        "/api/feedback",
        json={"trace_id": trace_id, "session_id": sid, "feedback": "like"},
        headers=authed["headers"],
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert len(fake.scored) == 1
    s = fake.scored[0]
    assert s["trace_id"] == trace_id
    assert s["name"] == "user_feedback"
    assert s["value"] == "like"
    assert s["data_type"] == "CATEGORICAL"
    assert s["score_id"] == f"{trace_id}-user_feedback"


def test_feedback_idempotent_score_id(client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch):
    """先赞后踩：两次 score 共用同一幂等键，便于 Langfuse 覆盖而非堆叠。"""
    main = tmp_data["main"]
    monkeypatch.setattr(main, "agent", None)
    _, sid = _seed_and_session(authed, seed_dataset, sample_entries, sample_summary)

    chat = client.post(
        "/api/chat",
        json={"message": "概览", "session_id": sid},
        headers=authed["headers"],
    )
    trace_id = chat.json()["trace_id"]

    fake = _FakeTracer()
    monkeypatch.setattr(main, "get_client", lambda: fake)

    for kind in ("like", "dislike"):
        resp = client.post(
            "/api/feedback",
            json={"trace_id": trace_id, "session_id": sid, "feedback": kind},
            headers=authed["headers"],
        )
        assert resp.status_code == 200

    assert len(fake.scored) == 2
    key = f"{trace_id}-user_feedback"
    assert all(s["score_id"] == key for s in fake.scored)
    assert [s["value"] for s in fake.scored] == ["like", "dislike"]
