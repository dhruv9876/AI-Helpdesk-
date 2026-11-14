from tests.conftest import PASSWORD


def test_register_creates_customer(client):
    res = client.post(
        "/api/auth/register",
        json={"email": "New@Example.com", "full_name": "New User", "password": PASSWORD},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["email"] == "new@example.com"  # normalised
    assert body["role"] == "customer"
    assert "password" not in body and "hashed_password" not in body


def test_register_cannot_choose_role(client):
    res = client.post(
        "/api/auth/register",
        json={"email": "x@example.com", "full_name": "X", "password": PASSWORD, "role": "admin"},
    )
    assert res.status_code == 201
    assert res.json()["role"] == "customer"


def test_register_duplicate_email(client, users):
    res = client.post(
        "/api/auth/register",
        json={"email": "alice@example.com", "full_name": "Dup", "password": PASSWORD},
    )
    assert res.status_code == 409


def test_register_validation(client):
    res = client.post(
        "/api/auth/register", json={"email": "not-an-email", "full_name": "A", "password": "short"}
    )
    assert res.status_code == 422


def test_login_success_and_me(client, users):
    res = client.post(
        "/api/auth/login", data={"username": "alice@example.com", "password": PASSWORD}
    )
    assert res.status_code == 200
    token = res.json()["access_token"]
    me = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


def test_login_wrong_password(client, users):
    res = client.post(
        "/api/auth/login", data={"username": "alice@example.com", "password": "wrong-password"}
    )
    assert res.status_code == 401


def test_login_unknown_user(client):
    res = client.post("/api/auth/login", data={"username": "ghost@example.com", "password": PASSWORD})
    assert res.status_code == 401


def test_protected_route_requires_token(client):
    assert client.get("/api/users/me").status_code == 401
    bad = client.get("/api/users/me", headers={"Authorization": "Bearer garbage"})
    assert bad.status_code == 401


def test_disabled_user_cannot_login_or_use_token(client, auth, users, db_session):
    users["alice"].is_active = False
    db_session.commit()
    assert client.get("/api/users/me", headers=auth["alice"]).status_code == 401
    res = client.post(
        "/api/auth/login", data={"username": "alice@example.com", "password": PASSWORD}
    )
    assert res.status_code == 403


def test_admin_manages_users(client, auth, users):
    # admin creates an agent
    res = client.post(
        "/api/users",
        headers=auth["admin"],
        json={"email": "newagent@example.com", "full_name": "N", "password": PASSWORD, "role": "agent"},
    )
    assert res.status_code == 201 and res.json()["role"] == "agent"
    # list + filter
    agents = client.get("/api/users?role=agent", headers=auth["admin"]).json()
    assert {a["email"] for a in agents} == {"agent@example.com", "newagent@example.com"}
    # disable a customer
    res = client.patch(f"/api/users/{users['bob'].id}", headers=auth["admin"], json={"is_active": False})
    assert res.json()["is_active"] is False


def test_non_admin_cannot_manage_users(client, auth):
    body = {"email": "z@example.com", "full_name": "Z", "password": PASSWORD, "role": "admin"}
    assert client.post("/api/users", headers=auth["alice"], json=body).status_code == 403
    assert client.post("/api/users", headers=auth["agent"], json=body).status_code == 403
    assert client.get("/api/users", headers=auth["agent"]).status_code == 403


def test_admin_cannot_demote_self(client, auth, users):
    res = client.patch(f"/api/users/{users['admin'].id}", headers=auth["admin"], json={"role": "agent"})
    assert res.status_code == 400
