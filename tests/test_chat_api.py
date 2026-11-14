import pytest

from tests.conftest import make_user
from app.models import UserRole


def open_chat(client, headers):
    res = client.post("/api/chats", headers=headers)
    assert res.status_code in (200, 201), res.text
    return res.json()


def say(client, headers, chat_id, text):
    res = client.post(f"/api/chats/{chat_id}/messages", headers=headers, json={"body": text})
    assert res.status_code == 201, res.text
    return res.json()


def handed_off(client, auth, who="alice"):
    chat = open_chat(client, auth[who])
    say(client, auth[who], chat["id"], "agent")
    return chat["id"]


# ---------- open / list / read ----------
def test_open_chat_is_idempotent_for_an_open_chat(client, auth):
    first = client.post("/api/chats", headers=auth["alice"])
    again = client.post("/api/chats", headers=auth["alice"])
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["status"] == "bot" and first.json()["customer_name"] == "alice"


def test_only_signed_in_customers_can_open_chats(client, auth):
    assert client.post("/api/chats").status_code == 401
    assert client.post("/api/chats", headers=auth["agent"]).status_code == 403


def test_list_scopes_and_filters(client, auth):
    mine = open_chat(client, auth["alice"])
    waiting = handed_off(client, auth, "bob")
    assert [c["id"] for c in client.get("/api/chats", headers=auth["alice"]).json()] == [mine["id"]]
    assert {c["id"] for c in client.get("/api/chats", headers=auth["agent"]).json()} == {mine["id"], waiting}
    queue = client.get("/api/chats?status=waiting", headers=auth["agent"]).json()
    assert [c["id"] for c in queue] == [waiting]
    assert queue[0]["last_message"] == "agent"  # what the customer said, not the bot's hand-off notice
    assert client.get("/api/chats?status=nonsense", headers=auth["agent"]).status_code == 422


def test_chat_preview_is_the_customers_last_message_with_a_fallback(client, auth):
    chat = open_chat(client, auth["alice"])
    assert client.get("/api/chats", headers=auth["alice"]).json()[0]["last_message"].startswith("Hi alice")  # only the greeting so far
    say(client, auth["alice"], chat["id"], "My invoice amount is wrong")
    client.post(f"/api/chats/{chat['id']}/close", headers=auth["alice"])  # adds a "Chat closed." system message after it
    assert client.get("/api/chats", headers=auth["agent"]).json()[0]["last_message"] == "My invoice amount is wrong"


def test_waiting_queue_is_oldest_first(client, auth, db_session):
    a = handed_off(client, auth, "alice")
    b = handed_off(client, auth, "bob")
    assert [c["id"] for c in client.get("/api/chats?status=waiting", headers=auth["agent"]).json()] == [a, b]


def test_detail_access_and_handoff_reason_visibility(client, auth):
    chat_id = handed_off(client, auth)
    mine = client.get(f"/api/chats/{chat_id}", headers=auth["alice"]).json()
    staff = client.get(f"/api/chats/{chat_id}", headers=auth["agent"]).json()
    assert mine["handoff_reason"] is None  # customers never see why they were handed off
    assert staff["handoff_reason"] == "Customer asked for a human"
    assert [m["sender_type"] for m in staff["messages"]] == ["bot", "customer", "system"]
    assert client.get(f"/api/chats/{chat_id}", headers=auth["bob"]).status_code == 404
    assert client.get(f"/api/chats/{chat_id}").status_code == 401
    assert client.get("/api/chats/999", headers=auth["agent"]).status_code == 404


# ---------- messages ----------
def test_rest_message_returns_what_was_created(client, auth, seeded_kb):
    chat = open_chat(client, auth["alice"])
    created = say(client, auth["alice"], chat["id"], "I forgot my password, how do I reset it?")
    assert [m["sender_type"] for m in created] == ["customer", "bot"]
    assert created[1]["sources"][0]["title"] == "Resetting your password"
    assert created[1]["sender_name"] == "Support Assistant"


def test_message_validation_and_access(client, auth):
    chat = open_chat(client, auth["alice"])
    url = f"/api/chats/{chat['id']}/messages"
    assert client.post(url, headers=auth["alice"], json={"body": ""}).status_code == 422
    assert client.post(url, headers=auth["alice"], json={"body": "x" * 2001}).status_code == 422
    assert client.post(url, headers=auth["alice"], json={}).status_code == 422
    assert client.post(url, headers=auth["bob"], json={"body": "hi"}).status_code == 404
    assert client.post(url, json={"body": "hi"}).status_code == 401


# ---------- claim / close ----------
def test_claim_flow_and_conflicts(client, auth, db_session):
    chat_id = handed_off(client, auth)
    other = make_user(db_session, "agent2@example.com", UserRole.agent)
    from tests.conftest import login
    other_headers = login(client, other.email)

    assert client.post(f"/api/chats/{chat_id}/claim", headers=auth["alice"]).status_code == 403
    res = client.post(f"/api/chats/{chat_id}/claim", headers=auth["agent"])
    assert res.status_code == 200 and res.json()["status"] == "active" and res.json()["agent_name"] == "agent"
    assert client.post(f"/api/chats/{chat_id}/claim", headers=other_headers).status_code == 409
    assert client.post("/api/chats/999/claim", headers=auth["agent"]).status_code == 404
    assert client.get("/api/chats?status=waiting", headers=auth["agent"]).json() == []


def test_agent_conversation_over_rest(client, auth):
    chat_id = handed_off(client, auth)
    assert client.post(f"/api/chats/{chat_id}/messages", headers=auth["agent"], json={"body": "hi"}).status_code == 403  # not claimed
    client.post(f"/api/chats/{chat_id}/claim", headers=auth["agent"])
    reply = say(client, auth["agent"], chat_id, "Hello Alice, how can I help?")
    assert [(m["sender_type"], m["sender_name"]) for m in reply] == [("agent", "agent")]
    after = say(client, auth["alice"], chat_id, "My invoice is wrong")
    assert [m["sender_type"] for m in after] == ["customer"]  # the bot stays out of it


def test_close_chat(client, auth):
    chat_id = handed_off(client, auth)
    assert client.post(f"/api/chats/{chat_id}/close", headers=auth["bob"]).status_code == 404
    assert client.post(f"/api/chats/{chat_id}/close", headers=auth["agent"]).status_code == 403  # not assigned
    res = client.post(f"/api/chats/{chat_id}/close", headers=auth["alice"])
    assert res.status_code == 200 and res.json()["status"] == "closed" and res.json()["closed_at"]
    assert client.post(f"/api/chats/{chat_id}/close", headers=auth["alice"]).status_code == 409
    assert client.post(f"/api/chats/{chat_id}/messages", headers=auth["alice"], json={"body": "hi"}).status_code == 409
    assert client.get("/api/chats?status=waiting", headers=auth["agent"]).json() == []


# ---------- chat -> ticket ----------
def test_convert_chat_to_a_triaged_ticket(client, auth, users, ai_on, seeded_kb):
    chat = open_chat(client, auth["alice"])
    say(client, auth["alice"], chat["id"], "My app crashes every time I open it, please help")
    say(client, auth["alice"], chat["id"], "agent")
    res = client.post(f"/api/chats/{chat['id']}/ticket", headers=auth["alice"])
    assert res.status_code == 201
    ticket = res.json()
    assert ticket["subject"].startswith("Chat: My app crashes")
    assert ticket["customer_id"] == users["alice"].id
    assert ticket["ai"] is None  # the customer's view never includes AI fields

    staff = client.get(f"/api/tickets/{ticket['id']}", headers=auth["agent"]).json()
    assert staff["ai"]["category"] == "technical" and staff["ai"]["triaged_at"]  # background triage ran
    assert "Customer: My app crashes" in staff["description"] and "Support bot:" in staff["description"]

    closed = client.get(f"/api/chats/{chat['id']}", headers=auth["alice"]).json()
    assert closed["status"] == "closed" and closed["ticket_id"] == ticket["id"]
    assert client.post(f"/api/chats/{chat['id']}/ticket", headers=auth["alice"]).status_code == 409
    assert client.get(f"/api/tickets/{ticket['id']}", headers=auth["alice"]).status_code == 200  # it is the customer's own ticket


def test_convert_requires_a_customer_message_and_access(client, auth):
    chat = open_chat(client, auth["alice"])
    assert client.post(f"/api/chats/{chat['id']}/ticket", headers=auth["alice"]).status_code == 422
    say(client, auth["alice"], chat["id"], "hello there")
    assert client.post(f"/api/chats/{chat['id']}/ticket", headers=auth["bob"]).status_code == 404
    assert client.post(f"/api/chats/{chat['id']}/ticket").status_code == 401


# ---------- stats ----------
def test_stats_include_chat_numbers(client, auth, seeded_kb):
    empty = client.get("/api/ai/stats", headers=auth["agent"]).json()
    assert (empty["chats_total"], empty["handoff_rate"]) == (0, None)
    solved = open_chat(client, auth["alice"])
    say(client, auth["alice"], solved["id"], "How do I reset my password?")
    handed_off(client, auth, "bob")
    s = client.get("/api/ai/stats", headers=auth["agent"]).json()
    assert (s["chats_total"], s["chats_handed_off"], s["handoff_rate"]) == (2, 1, 0.5)
