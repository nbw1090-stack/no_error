"""
组件管理 API 端点测试（按用户隔离）

依赖 conftest.py：
- tmp_data：组件 DB 重定向到 tmp_path/components.db（不再全局播种）。
- authed：注册 alice 并经注册流程播种 4 个默认组件（enabled=0）。

语义：enabled = 「该组件是否已被当前用户 AST 分析」。本文件覆盖注册表 CRUD
与隔离；enabled 由 AST 流程维护的链路在 test_ast_analysis.py 中验证。
"""

# 预期的 4 个种子组件名
SEED_NAMES = {"pcie_device", "sensor", "hwproxy", "framework"}


def _names(resp_json):
    return {c["name"] for c in resp_json["components"]}


def _get(authed):
    return authed["client"].get(
        "/api/components", headers=authed["headers"]
    )


# ============================================================
# 鉴权
# ============================================================
def test_list_requires_auth(client):
    resp = client.get("/api/components")
    assert resp.status_code == 401


# ============================================================
# 列表（注册即播种，默认 enabled=False）
# ============================================================
def test_list_returns_seeded_components(authed):
    resp = _get(authed)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert SEED_NAMES.issubset(_names(data))
    # 字段集仍含 enabled（语义=已分析），种子默认未分析
    for c in data["components"]:
        assert set(c.keys()) == {"name", "git_url", "branch", "enabled"}
        assert c["enabled"] is False


# ============================================================
# 新建（enabled 不由用户指定，默认 False）
# ============================================================
def test_create_new_component(authed):
    payload = {
        "name": "ipmb",
        "git_url": "https://github.com/bmc-org/ipmb.git",
        "branch": "dev",
    }
    resp = authed["client"].post(
        "/api/components", json=payload, headers=authed["headers"]
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "ipmb"
    assert created["git_url"].endswith("/ipmb.git")
    assert created["branch"] == "dev"
    assert created["enabled"] is False  # 新组件尚未分析

    assert "ipmb" in _names(_get(authed).json())


def test_create_duplicate_returns_409(authed):
    payload = {
        "name": "sensor",
        "git_url": "https://github.com/bmc-org/sensor.git",
        "branch": "main",
    }
    resp = authed["client"].post(
        "/api/components", json=payload, headers=authed["headers"]
    )
    assert resp.status_code == 409
    assert "exists" in resp.json()["detail"].lower()


# ============================================================
# 更新（改源码 git_url/branch 会重置 enabled=False）
# ============================================================
def test_update_existing_reflected_in_list(authed):
    payload = {
        "git_url": "https://github.com/bmc-org/sensor-v2.git",
        "branch": "release/2.0",
    }
    resp = authed["client"].put(
        "/api/components/sensor", json=payload, headers=authed["headers"]
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["git_url"].endswith("sensor-v2.git")
    assert updated["branch"] == "release/2.0"

    sensor = next(
        c for c in _get(authed).json()["components"] if c["name"] == "sensor"
    )
    assert sensor["branch"] == "release/2.0"


def test_update_missing_returns_404(authed):
    payload = {"git_url": "", "branch": "main"}
    resp = authed["client"].put(
        "/api/components/does_not_exist",
        json=payload,
        headers=authed["headers"],
    )
    assert resp.status_code == 404


# ============================================================
# 删除
# ============================================================
def test_delete_then_list_excludes(authed):
    resp = authed["client"].delete(
        "/api/components/hwproxy", headers=authed["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}
    assert "hwproxy" not in _names(_get(authed).json())


def test_delete_missing_returns_404(authed):
    resp = authed["client"].delete(
        "/api/components/ghost", headers=authed["headers"]
    )
    assert resp.status_code == 404


def test_delete_component_also_clears_ast_analysis(authed):
    """删除组件应同时清空其 AST 分析数据，避免残留孤儿结果。"""
    import ast_analysis

    user_id = authed["user_id"]
    headers = authed["headers"]
    client = authed["client"]

    # 直接为 sensor 播种一份 AST 分析数据（无需真实 git clone）
    ast_analysis.db.replace_component(
        user_id,
        "sensor",
        "https://github.com/bmc-org/sensor.git",
        "main",
        "abcdef01",
        "2026-01-01T00:00:00+00:00",
        [],
    )

    # 删除前：注册表含 sensor，AST 结果也含 sensor
    assert "sensor" in _names(_get(authed).json())
    before = client.get("/api/ast/result", headers=headers)
    assert before.status_code == 200
    assert any(c["component"] == "sensor" for c in before.json()["components"])

    # 删除组件
    resp = client.delete("/api/components/sensor", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}

    # 注册表中已无 sensor
    assert "sensor" not in _names(_get(authed).json())

    # sensor 是唯一的 AST 分析数据 → 删除后结果为空 → 404
    after = client.get("/api/ast/result", headers=headers)
    assert after.status_code == 404


# ============================================================
# 用户隔离：不同用户互不可见
# ============================================================
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


def test_components_isolated_between_users(client):
    alice = _register(client, "alice", "aaa111")
    bob = _register(client, "bob", "bbb222")

    # alice 新增一个私有组件
    client.post(
        "/api/components",
        json={"name": "alice_only", "git_url": "", "branch": "main"},
        headers=alice["headers"],
    )

    a_names = _names(client.get("/api/components", headers=alice["headers"]).json())
    b_names = _names(client.get("/api/components", headers=bob["headers"]).json())

    assert "alice_only" in a_names
    assert "alice_only" not in b_names
    # 两人各自都拥有默认种子（注册即播种）
    assert SEED_NAMES.issubset(a_names)
    assert SEED_NAMES.issubset(b_names)


def test_delete_component_isolated_per_user(client):
    """删除 alice 的组件不得影响 bob 的同名组件或其 AST 分析数据。"""
    import ast_analysis

    alice = _register(client, "alice", "aaa111")
    bob = _register(client, "bob", "bbb222")

    # 两人各自的 sensor 都播种一份 AST 分析数据
    for tok in (alice, bob):
        ast_analysis.db.replace_component(
            tok["user_id"],
            "sensor",
            "https://github.com/bmc-org/sensor.git",
            "main",
            "abcdef01",
            "2026-01-01T00:00:00+00:00",
            [],
        )

    # alice 删除自己的 sensor（注册表 + alice 的 AST 数据）
    resp = client.delete("/api/components/sensor", headers=alice["headers"])
    assert resp.status_code == 200, resp.text

    # alice：注册表无 sensor，AST 结果随之清空 → 404
    a_names = _names(client.get("/api/components", headers=alice["headers"]).json())
    assert "sensor" not in a_names
    assert client.get("/api/ast/result", headers=alice["headers"]).status_code == 404

    # bob：sensor 仍在注册表，AST 结果仍含 sensor（未被波及）
    b_names = _names(client.get("/api/components", headers=bob["headers"]).json())
    assert "sensor" in b_names
    bob_result = client.get("/api/ast/result", headers=bob["headers"])
    assert bob_result.status_code == 200
    assert any(c["component"] == "sensor" for c in bob_result.json()["components"])
