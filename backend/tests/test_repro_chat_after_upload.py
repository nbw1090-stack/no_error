"""临时复现：先对话 → 上传绑定 → 再对话 的完整链路（run + run_stream）"""

import json

from conftest import FakeLLMAdapter, create_blank_session, create_session


def _sse_events(resp):
    return [json.loads(l[5:].strip()) for l in resp.text.splitlines() if l.startswith("data:")]


def test_chat_then_link_then_chat_stream(
    client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch, tc
):
    from agent.core import Agent

    main = tmp_data["main"]
    fake_agent = Agent(
        llm=FakeLLMAdapter(["通用 BMC 答复"]),  # 第一轮（空会话，纯文本）
        session_manager=main.session_manager,
        max_iterations=5,
    )
    monkeypatch.setattr(main, "agent", fake_agent)

    sid = create_blank_session(client, authed["headers"])

    # ---- 第一轮：空会话对话 ----
    resp = client.post(
        "/api/chat/stream",
        json={"message": "BMC 是什么", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp)
    deltas = "".join(e.get("text", "") for e in events if e["type"] == "delta")
    assert deltas == "通用 BMC 答复", deltas
    print("\n[round1 OK] 空会话回复:", deltas)

    # 查看第一轮落盘的 assistant 消息格式
    msgs1 = client.get(f"/api/sessions/{sid}", headers=authed["headers"]).json()["messages"]
    print("[round1 落盘消息]:", json.dumps(msgs1, ensure_ascii=False))

    # ---- 绑定数据集（先对话后上传）----
    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    resp = client.put(
        f"/api/sessions/{sid}/dataset",
        json={"dataset_id": did},
        headers=authed["headers"],
    )
    assert resp.status_code == 200

    # ---- 第二轮：有 dataset，补充 FakeLLM 响应（工具调用 + 最终回复）----
    fake_agent.llm.responses = [[tc("get_summary", {})], "基于真实日志的分析结果"]

    resp = client.post(
        "/api/chat/stream",
        json={"message": "分析一下错误", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp)
    types = [e["type"] for e in events]
    deltas = "".join(e.get("text", "") for e in events if e["type"] == "delta")
    print("[round2 event types]:", types)
    print("[round2 deltas]:", repr(deltas))
    assert deltas == "基于真实日志的分析结果", deltas
    print("[round2 OK] 绑定后回复:", deltas)


def test_chat_then_link_then_chat_nonstream(
    client, authed, seed_dataset, sample_entries, sample_summary, tmp_data, monkeypatch, tc
):
    from agent.core import Agent

    main = tmp_data["main"]
    fake_agent = Agent(
        llm=FakeLLMAdapter(["通用 BMC 答复"]),
        session_manager=main.session_manager,
        max_iterations=5,
    )
    monkeypatch.setattr(main, "agent", fake_agent)

    sid = create_blank_session(client, authed["headers"])

    resp = client.post(
        "/api/chat",
        json={"message": "BMC 是什么", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 200, resp.text
    print("\n[round1 nonstream OK]:", resp.json()["reply"])

    # 第一轮落盘消息
    msgs1 = client.get(f"/api/sessions/{sid}", headers=authed["headers"]).json()["messages"]
    print("[round1 nonstream 落盘消息]:", json.dumps(msgs1, ensure_ascii=False))

    did = seed_dataset(sample_entries, sample_summary, authed["user_id"])
    client.put(f"/api/sessions/{sid}/dataset", json={"dataset_id": did}, headers=authed["headers"])

    fake_agent.llm.responses = [[tc("get_summary", {})], "基于真实日志的分析结果"]

    resp = client.post(
        "/api/chat",
        json={"message": "分析一下错误", "session_id": sid},
        headers=authed["headers"],
    )
    assert resp.status_code == 200, resp.text
    print("[round2 nonstream status]:", resp.status_code, "body:", resp.text[:300])
    assert resp.json()["reply"] == "基于真实日志的分析结果", resp.text
    print("[round2 nonstream OK]:", resp.json()["reply"])
