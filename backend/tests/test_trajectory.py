"""
eval.trajectory.extract_trajectory 单测：从持久化 session 消息重建工具轨迹。

只依赖 SessionManager 的真实读写，全程离线（conftest 已屏蔽 LLM/Langfuse）。
"""

import json

from agent.session import SessionManager
from eval.trajectory import extract_trajectory, call_signature


def _assistant_tool_call(name, args, call_id):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def _tool_result(call_id, payload):
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload)}


def test_extract_trajectory_basic(tmp_path):
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create(dataset_id="d1", user_id=3, username="eval")
    sid = session.session_id

    sm.add_message(sid, {"role": "user", "content": "pcie 报错？"})
    # 第 1 次调用：成功
    sm.add_message(sid, _assistant_tool_call("gather_code_context", {"component": "pcie_device", "line": 49}, "c1"))
    sm.add_message(sid, _tool_result("c1", {"target": {"function": "pcie_oob_mgmt_init"}}))
    # 第 2 次调用：同名同参 → 重复，且结果 error
    sm.add_message(sid, _assistant_tool_call("gather_code_context", {"component": "pcie_device", "line": 49}, "c2"))
    sm.add_message(sid, _tool_result("c2", {"error": "未找到匹配的源码文件"}))
    # 最终回复
    sm.add_message(sid, {"role": "assistant", "content": "根因是 ...", "tool_calls": None})

    traj = extract_trajectory(sm, sid)

    assert traj["tool_names"] == ["gather_code_context", "gather_code_context"]
    assert traj["tool_call_count"] == 2
    assert traj["iterations"] == 3  # 两次工具轮 + 一次最终回复
    assert traj["duplicate_calls"] == 1
    # 结果与 error 标记被正确回填
    assert traj["tool_calls"][0]["error"] is False
    assert traj["tool_calls"][1]["error"] is True
    assert traj["tool_calls"][0]["result"]["target"]["function"] == "pcie_oob_mgmt_init"


def test_extract_trajectory_no_tools(tmp_path):
    sm = SessionManager(str(tmp_path / "sessions"))
    session = sm.create(dataset_id="d1", user_id=3, username="eval")
    sid = session.session_id
    sm.add_message(sid, {"role": "user", "content": "你好"})
    sm.add_message(sid, {"role": "assistant", "content": "你好，请上传日志。", "tool_calls": None})

    traj = extract_trajectory(sm, sid)
    assert traj["tool_call_count"] == 0
    assert traj["tool_names"] == []
    assert traj["iterations"] == 1
    assert traj["duplicate_calls"] == 0


def test_extract_trajectory_missing_session(tmp_path):
    sm = SessionManager(str(tmp_path / "sessions"))
    traj = extract_trajectory(sm, "nonexistent")
    assert traj["tool_call_count"] == 0
    assert traj["iterations"] == 0


def test_call_signature_stable_across_arg_order():
    a = call_signature("t", {"x": 1, "y": 2})
    b = call_signature("t", {"y": 2, "x": 1})
    assert a == b
