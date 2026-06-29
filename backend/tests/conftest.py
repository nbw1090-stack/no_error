"""
测试公共 fixtures

最关键的职责：**在任何项目模块被 import 之前**强制覆盖环境变量，
屏蔽 `.env` 里预置的真实 LLM_API_KEY 与 LANGFUSE_ENABLED=true，
保证整个测试套件离线、无网络、可重复。

打桩能力：
- FakeLLMAdapter：脚本化 ReAct 各轮响应，供 Agent 核心测试
- make_tar_gz：内存构造 tar.gz，供 extractor / parse 端点测试
- tmp_data / client：把 main.py 的模块级全局重定向到 tmp_path，避免污染真实 backend/data
"""

# ============================================================
# 1. 导入前强制环境（必须在 import config / main / agent 之前执行）
# ============================================================
import os
import sys
import pathlib

# 覆盖 .env：config.py 的 _load_dotenv() 仅对 os.environ 中尚不存在的键设值，
# 因此先把这些键写入 os.environ，.env 的真实值就被忽略。用直接赋值消除歧义。
os.environ["LANGFUSE_ENABLED"] = "false"
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = "openai"
os.environ["LLM_API_BASE"] = ""
os.environ["LLM_MODEL"] = "test-model"
os.environ["LLM_TEMPERATURE"] = "0.0"
os.environ["LLM_MAX_TOKENS"] = "128"
os.environ["AGENT_MAX_ITERATIONS"] = "10"
os.environ["AGENT_MAX_HISTORY"] = "40"

# 确保 backend/ 在 import 根上（与 pytest.ini 的 pythonpath=. 双保险）
BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# ============================================================
# 2. 标准库 / 测试依赖
# ============================================================
import io
import json
import tarfile
import uuid

import pytest

# ============================================================
# 3. 数据 fixtures
# ============================================================

# 一份确定性样本：覆盖 ERROR×2(pcie_device) / WARNING(sensor) /
# NOTICE(hwproxy) / LAUNCH(framework, 1970 时间戳) / UNKNOWN
@pytest.fixture
def sample_entries():
    return [
        {
            "id": 1,
            "timestamp": "2025-07-24 11:31:13.714",
            "component": "pcie_device",
            "level": "ERROR",
            "file": "pcie_card.lua",
            "line": 49,
            "message": "PCIe card init failed",
            "source": "app.log",
        },
        {
            "id": 2,
            "timestamp": "2025-07-24 11:31:14.000",
            "component": "pcie_device",
            "level": "ERROR",
            "file": "pcie_card.lua",
            "line": 50,
            "message": "PCIe card init failed again",
            "source": "app.log",
        },
        {
            "id": 3,
            "timestamp": "2025-07-24 11:32:00.000",
            "component": "sensor",
            "level": "WARNING",
            "file": "sensor.lua",
            "line": 10,
            "message": "Temperature high warning",
            "source": "app.log",
        },
        {
            "id": 4,
            "timestamp": "2025-07-24 11:33:00.000",
            "component": "hwproxy",
            "level": "NOTICE",
            "file": "hwproxy.lua",
            "line": 5,
            "message": "Boot notice",
            "source": "app.log",
        },
        {
            "id": 5,
            "timestamp": "1970-01-01 00:00:21.666",
            "component": "framework",
            "level": "LAUNCH",
            "file": None,
            "line": None,
            "message": "snlua bootstrap",
            "source": "framework.log",
        },
        {
            "id": 6,
            "timestamp": None,
            "component": None,
            "level": "UNKNOWN",
            "file": None,
            "line": None,
            "message": "garbage line that matches no pattern",
            "source": "app.log",
        },
    ]


@pytest.fixture
def sample_summary():
    return {
        "totalLines": 6,
        "errorCount": 2,
        "warningCount": 1,
        "noticeCount": 1,
        "launchCount": 1,
        "unknownCount": 1,
        "components": ["framework", "hwproxy", "pcie_device", "sensor"],
        "componentErrors": [{"name": "pcie_device", "count": 2}],
        "timeRange": {
            "start": "1970-01-01 00:00:21.666",
            "end": "2025-07-24 11:33:00.000",
        },
    }


@pytest.fixture
def sample_dataset(sample_entries, sample_summary):
    from agent.dataset import LogDataset

    return LogDataset(entries=sample_entries, summary=sample_summary)


@pytest.fixture
def many_error_entries():
    """生成 130 条 pcie_device ERROR + 若干其他，用于验证 max_results 截断（>100 / >200）。"""
    entries = []
    for i in range(130):
        entries.append(
            {
                "id": i + 1,
                "timestamp": "2025-07-24 11:00:%05d" % (i % 60),
                "component": "pcie_device",
                "level": "ERROR",
                "file": "pcie_card.lua",
                "line": i,
                "message": "PCIe card init failed",
                "source": "app.log",
            }
        )
    return entries


# ============================================================
# 4. make_tar_gz：内存构造 tar.gz，复刻 extractor 的匹配规则
# ============================================================

@pytest.fixture
def make_tar_gz():
    def _build(
        app_text=None,
        framework_text=None,
        app_name="Dump/LogDump/app.log",
        framework_name="Dump/LogDump/framework.log",
        extra_members=None,
    ):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def _add_file(name, data):
                payload = data.encode("utf-8")
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

            def _add_dir(name):
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)  # 目录成员无 fileobj

            if app_text is not None:
                _add_file(app_name, app_text)
            if framework_text is not None:
                _add_file(framework_name, framework_text)
            for member in extra_members or []:
                name, data = member
                if data is None:
                    _add_dir(name)
                else:
                    _add_file(name, data)
        return buf.getvalue()

    return _build


# ============================================================
# 5. FakeLLMAdapter：ReAct 循环打桩
# ============================================================

def make_tool_call(name, args_dict, tool_id="call_1"):
    """构造一个标准化 tool_call（arguments 已序列化为合法 JSON 字符串）。"""
    return {
        "id": tool_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args_dict)},
    }


# 暴露为 fixture，方便测试注入
@pytest.fixture
def tc():
    return make_tool_call


class FakeLLMAdapter:
    """
    假 LLM 适配器：按脚本逐轮返回响应。

    responses 中每个元素：
      - str  → 纯文本最终回复
      - list → tool_calls 列表（调用工具）

    chat() / chat_stream() 共享同一个 responses 队列（pop(0)），
    因此 ReAct 循环里交替使用两种调用方式时也按序消费。
    所有调用都会记录到 calls / stream_calls 供断言。

    usages（可选）：与 responses 平行的 token 消耗列表，每个元素
    {"input": int, "output": int, "total": int}；缺省 None → 不带 usage
    （模拟供应商未返回 usage 的场景，现有测试零影响）。
    """

    def __init__(self, responses, usages=None):
        self.responses = list(responses)
        self.usages = list(usages) if usages else None
        self.calls = []
        self.stream_calls = []

    def _next_usage(self):
        """与 responses 同步 pop；未提供 usages 时返回 None。"""
        if self.usages:
            return self.usages.pop(0)
        return None

    async def chat(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("FakeLLM 响应队列已耗尽")
        r = self.responses.pop(0)
        u = self._next_usage()
        if isinstance(r, str):
            return {
                "role": "assistant",
                "content": r,
                "tool_calls": None,
                "usage": u,
            }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": r,
            "usage": u,
        }

    async def chat_stream(self, messages, tools=None):
        self.stream_calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("FakeLLM 响应队列已耗尽")
        r = self.responses.pop(0)
        u = self._next_usage()
        if isinstance(r, str):
            # 按词逐个 yield content_delta（真流式）
            words = r.split(" ")
            for i, word in enumerate(words):
                sep = " " if i < len(words) - 1 else ""
                yield {"type": "content_delta", "text": word + sep}
            yield {
                "type": "done",
                "finish_reason": "stop",
                "content": r,
                "tool_calls": None,
                "usage": u,
            }
        else:
            # 工具调用轮：只 yield 一个 done（带 tool_calls），无文本增量
            yield {
                "type": "done",
                "finish_reason": "tool_calls",
                "content": "",
                "tool_calls": r,
                "usage": u,
            }


@pytest.fixture
def fake_llm_factory():
    """返回 (FakeLLMAdapter 构造函数, make_tool_call)。"""
    return FakeLLMAdapter, make_tool_call


# ============================================================
# 6. main.py 端点隔离 harness
# ============================================================

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """
    导入 main（此时 env 已强制覆盖，安全），
    把 DATASETS_DIR / session_manager 重定向到 tmp_path，
    避免污染真实 backend/data。
    """
    import main
    import db
    import auth
    import ast_analysis
    import wiki

    datasets_dir = tmp_path / "datasets"
    sessions_dir = tmp_path / "sessions"
    datasets_dir.mkdir()
    sessions_dir.mkdir()

    monkeypatch.setattr(main, "DATASETS_DIR", str(datasets_dir))
    monkeypatch.setattr(
        main, "session_manager", main.SessionManager(str(sessions_dir))
    )

    # 组件 DB 同样重定向到 tmp_path，避免污染真实 backend/data/components.db。
    # 注意：init_db 不再全局播种——默认组件改为按用户在注册时 ensure_seeded
    # （authed fixture 注册 alice 时即播种）。
    components_db_path = str(tmp_path / "components.db")
    db.set_db_path(components_db_path)
    db.init_db()

    # 认证 / AST 分析 DB 同样重定向到 tmp_path，避免污染真实 backend/data
    auth_db_path = str(tmp_path / "users.db")
    auth.set_db_path(auth_db_path)
    auth.init_auth_db()

    ast_db_path = str(tmp_path / "ast.db")
    ast_analysis.set_db_path(ast_db_path)
    ast_analysis.init_ast_db()

    # AST 源码快照目录（保留 clone 的源码树）同样重定向到 tmp_path
    source_dir = str(tmp_path / "source")
    os.makedirs(source_dir, exist_ok=True)
    ast_analysis.set_source_dir(source_dir)

    # LLM Wiki 目录（全局共享）同样重定向到 tmp_path，避免污染真实 backend/data/wiki
    wiki_dir = str(tmp_path / "wiki")
    wiki.set_wiki_dir(wiki_dir)
    wiki.init_wiki_dir()

    return {
        "main": main,
        "datasets": datasets_dir,
        "sessions": sessions_dir,
        "components_db": components_db_path,
        "auth_db": auth_db_path,
        "ast_db": ast_db_path,
        "source_dir": source_dir,
        "wiki_dir": wiki_dir,
    }


@pytest.fixture
def client(tmp_data):
    from fastapi.testclient import TestClient

    return TestClient(tmp_data["main"].app)


@pytest.fixture
def seed_dataset(tmp_data):
    """返回一个写入数据集 JSON 的辅助函数，返回 dataset_id。

    user_id 为必填（数据集按用户隔离）：测试中传 authed["user_id"]。
    """
    main = tmp_data["main"]

    def _seed(entries, summary, user_id, dataset_id=None):
        dataset_id = dataset_id or uuid.uuid4().hex[:12]
        main.atomic_write_json(
            os.path.join(main.DATASETS_DIR, f"{dataset_id}.json"),
            {
                "dataset_id": dataset_id,
                "entries": entries,
                "summary": summary,
                "user_id": user_id,
            },
        )
        return dataset_id

    return _seed


def create_session(client, dataset_id, headers):
    """POST /api/sessions 创建会话（需带鉴权头），返回 session_id。"""
    resp = client.post(
        "/api/sessions",
        json={"dataset_id": dataset_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def create_blank_session(client, headers):
    """POST /api/sessions 创建空数据集会话（不携带 dataset_id），返回 session_id。"""
    resp = client.post(
        "/api/sessions",
        json={},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


# ============================================================
# 7. 认证 fixture：注册一个测试用户，返回 (client, token, headers, user_id)
# ============================================================
@pytest.fixture
def authed(client):
    """
    注册 alice/secret123 并返回认证上下文。

    Returns:
        {"client", "token", "headers", "user_id", "username"}
    """
    resp = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    token = data["token"]
    return {
        "client": client,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
        "user_id": data["user_id"],
        "username": data["username"],
    }


# ============================================================
# 8. SSE 解析工具：从 TestClient 响应里抽取 data: 事件
# ============================================================
def parse_sse_events(response):
    """
    把 TestClient 的流式响应解析成事件 dict 列表。

    TestClient 会把 StreamingResponse 的全部 chunk 拼到 response.text，
    因此可以一次性按 'data: ' 前缀切分。
    """
    events = []
    for chunk in response.iter_lines():
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        if chunk.startswith("data:"):
            payload = chunk[len("data:"):].strip()
            if payload:
                events.append(json.loads(payload))
    return events

