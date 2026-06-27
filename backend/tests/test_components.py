"""
组件管理 API 端点测试

依赖 conftest.py 的 tmp_data / client：tmp_data 已把 db.COMPONENTS_DB_PATH
重定向到 tmp_path/components.db 并重新播种，因此每个用例都从已播种状态开始，
且永不触碰真实 backend/data。
"""

# 预期的 4 个种子组件名
SEED_NAMES = {"pcie_device", "sensor", "hwproxy", "framework"}


def _names(resp_json):
    return {c["name"] for c in resp_json["components"]}


# ============================================================
# 列表
# ============================================================
def test_list_returns_seeded_components(client):
    resp = client.get("/api/components")
    assert resp.status_code == 200
    data = resp.json()
    assert "components" in data
    names = _names(data)
    assert SEED_NAMES.issubset(names)
    # 每条都有完整字段且 enabled 为 bool
    for c in data["components"]:
        assert set(c.keys()) == {"name", "git_url", "branch", "enabled"}
        assert isinstance(c["enabled"], bool)


# ============================================================
# 新建
# ============================================================
def test_create_new_component(client):
    payload = {
        "name": "ipmb",
        "git_url": "https://github.com/bmc-org/ipmb.git",
        "branch": "dev",
        "enabled": False,
    }
    resp = client.post("/api/components", json=payload)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "ipmb"
    assert created["git_url"].endswith("/ipmb.git")
    assert created["branch"] == "dev"
    assert created["enabled"] is False

    # 后续列表应包含新组件
    listing = client.get("/api/components").json()
    assert "ipmb" in _names(listing)


def test_create_duplicate_returns_409(client):
    payload = {
        "name": "sensor",
        "git_url": "https://github.com/bmc-org/sensor.git",
        "branch": "main",
        "enabled": True,
    }
    resp = client.post("/api/components", json=payload)
    assert resp.status_code == 409
    assert "exists" in resp.json()["detail"].lower()


# ============================================================
# 更新
# ============================================================
def test_update_existing_reflected_in_list(client):
    payload = {
        "git_url": "https://github.com/bmc-org/sensor-v2.git",
        "branch": "release/2.0",
        "enabled": False,
    }
    resp = client.put("/api/components/sensor", json=payload)
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["git_url"].endswith("sensor-v2.git")
    assert updated["branch"] == "release/2.0"
    assert updated["enabled"] is False

    # 列表中也应反映
    listing = client.get("/api/components").json()
    sensor = next(c for c in listing["components"] if c["name"] == "sensor")
    assert sensor["branch"] == "release/2.0"
    assert sensor["enabled"] is False


def test_update_missing_returns_404(client):
    payload = {"git_url": "", "branch": "main", "enabled": True}
    resp = client.put("/api/components/does_not_exist", json=payload)
    assert resp.status_code == 404


# ============================================================
# 切换 enabled
# ============================================================
def test_toggle_flips_enabled(client):
    before = client.get("/api/components").json()
    sensor_before = next(c for c in before["components"] if c["name"] == "sensor")
    assert sensor_before["enabled"] is True  # 种子默认 True

    resp = client.patch("/api/components/sensor/toggle")
    assert resp.status_code == 200, resp.text
    toggled = resp.json()
    assert toggled["enabled"] is False

    # 再次切换 → 回到 True
    resp2 = client.patch("/api/components/sensor/toggle")
    assert resp2.status_code == 200
    assert resp2.json()["enabled"] is True


def test_toggle_missing_returns_404(client):
    resp = client.patch("/api/components/ghost/toggle")
    assert resp.status_code == 404


# ============================================================
# 删除
# ============================================================
def test_delete_then_list_excludes(client):
    resp = client.delete("/api/components/hwproxy")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}

    listing = client.get("/api/components").json()
    assert "hwproxy" not in _names(listing)


def test_delete_missing_returns_404(client):
    resp = client.delete("/api/components/ghost")
    assert resp.status_code == 404
