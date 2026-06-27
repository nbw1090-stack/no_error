"""
认证 API 端点测试

依赖 conftest.py 的 tmp_data / client：tmp_data 已把 auth.USERS_DB_PATH
重定向到 tmp_path/users.db 并 init，因此每个用例都从空用户表开始，
且永不触碰真实 backend/data。
"""


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 注册
# ============================================================
def test_register_returns_token(client):
    resp = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert set(data.keys()) == {"user_id", "username", "token"}
    assert data["username"] == "alice"
    assert isinstance(data["user_id"], int)
    assert isinstance(data["token"], str) and len(data["token"]) > 10


def test_register_duplicate_returns_409(client):
    payload = {"username": "alice", "password": "secret123"}
    first = client.post("/api/auth/register", json=payload)
    assert first.status_code == 201

    second = client.post("/api/auth/register", json=payload)
    assert second.status_code == 409
    assert "exist" in second.json()["detail"].lower()


def test_register_empty_username_returns_400(client):
    resp = client.post(
        "/api/auth/register", json={"username": "", "password": "secret123"}
    )
    assert resp.status_code == 400


def test_register_short_password_returns_400(client):
    resp = client.post(
        "/api/auth/register", json={"username": "alice", "password": "ab"}
    )
    assert resp.status_code == 400


# ============================================================
# 登录
# ============================================================
def test_login_returns_token(client):
    client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    resp = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "secret123"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data.keys()) == {"user_id", "username", "token"}
    assert data["username"] == "alice"
    assert isinstance(data["token"], str) and len(data["token"]) > 10


def test_login_wrong_password_returns_401(client):
    client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    resp = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "WRONG"},
    )
    assert resp.status_code == 401


def test_login_unknown_user_returns_401(client):
    resp = client.post(
        "/api/auth/login",
        json={"username": "ghost", "password": "secret123"},
    )
    assert resp.status_code == 401


# ============================================================
# /me
# ============================================================
def test_me_with_valid_token(client):
    reg = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "secret123"},
    ).json()
    resp = client.get("/api/auth/me", headers=_bearer(reg["token"]))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"user_id": reg["user_id"], "username": "alice"}


def test_me_without_header_returns_401(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_with_bad_token_returns_401(client):
    resp = client.get("/api/auth/me", headers=_bearer("not-a-real-token"))
    assert resp.status_code == 401


def test_me_with_malformed_header_returns_401(client):
    # 不是 Bearer 前缀
    resp = client.get(
        "/api/auth/me", headers={"Authorization": "Token abc"}
    )
    assert resp.status_code == 401


# ============================================================
# 登出
# ============================================================
def test_logout_revokes_token(client):
    reg = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "secret123"},
    ).json()
    headers = _bearer(reg["token"])

    out = client.post("/api/auth/logout", headers=headers)
    assert out.status_code == 200, out.text
    assert out.json() == {"ok": True}

    # 登出后同一 token 应失效
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 401


def test_logout_requires_auth(client):
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 401


# ============================================================
# 多用户隔离
# ============================================================
def test_two_users_distinct_ids_and_tokens(client):
    a = client.post(
        "/api/auth/register", json={"username": "alice", "password": "aaa111"}
    ).json()
    b = client.post(
        "/api/auth/register", json={"username": "bob", "password": "bbb222"}
    ).json()
    assert a["user_id"] != b["user_id"]
    assert a["token"] != b["token"]
    # 各自的 /me 返回自己的身份
    assert client.get("/api/auth/me", headers=_bearer(a["token"])).json()[
        "username"
    ] == "alice"
    assert client.get("/api/auth/me", headers=_bearer(b["token"])).json()[
        "username"
    ] == "bob"
