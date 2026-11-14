"""WebSocket tests. Each test socket runs on its own event loop, like separate real clients.
pytest.ini sets a timeout so a bug can never hang the suite."""
import time
from contextlib import contextmanager

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.services.chat_hub import hub
from tests.conftest import login, make_user
from app.models import UserRole


def token(headers) -> str:
    return headers["Authorization"].split()[1]


@contextmanager
def connect(client, path, headers):
    with client.websocket_connect(path) as ws:
        ws.send_json({"type": "auth", "token": token(headers)})
        yield ws


def open_chat(client, headers):
    return client.post("/api/chats", headers=headers).json()["id"]


def ready(ws):
    frame = ws.receive_json()
    assert frame["type"] == "ready", frame
    return frame


def until(ws, kind, limit=10):
    """Read frames until one of `kind` arrives, returning it (fails if it never does)."""
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame within {limit} frames")


def closes_with(client, path, send, expected_code):
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect(path) as ws:
            send(ws)
            ws.receive_json()
    assert info.value.code == expected_code


# ---------- connecting and authenticating ----------
def test_ready_frame_has_history(client, auth):
    chat_id = open_chat(client, auth["alice"])
    with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as ws:
        frame = ready(ws)
    assert frame["chat"]["id"] == chat_id and frame["chat"]["status"] == "bot"
    assert frame["chat"]["handoff_reason"] is None
    assert [m["sender_type"] for m in frame["messages"]] == ["bot"]


def test_bad_credentials_are_rejected(client, auth):
    chat_id = open_chat(client, auth["alice"])
    path = f"/api/chats/{chat_id}/ws"
    closes_with(client, path, lambda ws: ws.send_json({"type": "auth", "token": "junk"}), 4401)
    closes_with(client, path, lambda ws: ws.send_json({"type": "hello"}), 4401)  # wrong first frame
    closes_with(client, path, lambda ws: ws.send_text("not json"), 4401)
    closes_with(client, path, lambda ws: ws.send_json({"type": "auth"}), 4401)  # no token
    closes_with(client, path, lambda ws: ws.send_json([1, 2]), 4401)  # not an object


def test_must_authenticate_in_time(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "chat_auth_timeout_seconds", 0.3)
    chat_id = open_chat(client, auth["alice"])
    closes_with(client, f"/api/chats/{chat_id}/ws", lambda ws: None, 4401)  # says nothing


def test_disabled_account_cannot_connect(client, auth, users, db_session):
    chat_id = open_chat(client, auth["alice"])
    users["alice"].is_active = False
    db_session.commit()
    closes_with(client, f"/api/chats/{chat_id}/ws", lambda ws: ws.send_json({"type": "auth", "token": token(auth["alice"])}), 4401)


def test_access_control(client, auth):
    chat_id = open_chat(client, auth["alice"])
    for who, path, code in [
        ("bob", f"/api/chats/{chat_id}/ws", 4404),  # someone else's chat looks like it does not exist
        ("alice", "/api/chats/9999/ws", 4404),
        ("alice", "/api/chats/queue/ws", 4403),  # customers cannot watch the staff queue
    ]:
        closes_with(client, path, lambda ws, who=who: ws.send_json({"type": "auth", "token": token(auth[who])}), code)
    with connect(client, f"/api/chats/{chat_id}/ws", auth["agent"]) as ws:  # staff may watch any chat
        assert ready(ws)["chat"]["id"] == chat_id


# ---------- the bot ----------
def test_customer_message_gets_a_live_bot_answer(client, auth, seeded_kb):
    chat_id = open_chat(client, auth["alice"])
    with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as ws:
        ready(ws)
        ws.send_json({"type": "message", "body": "I forgot my password, how do I reset it?"})
        mine = until(ws, "message")["message"]
        bot = until(ws, "message")["message"]
    assert (mine["sender_type"], mine["body"]) == ("customer", "I forgot my password, how do I reset it?")
    assert bot["sender_type"] == "bot" and bot["sources"][0]["title"] == "Resetting your password"


def test_ping_pong_and_bad_frames_do_not_close_the_socket(client, auth):
    chat_id = open_chat(client, auth["alice"])
    with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as ws:
        ready(ws)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_text("this is not json")
        assert ws.receive_json()["code"] == 422
        ws.send_json({"type": "dance"})
        assert ws.receive_json()["detail"] == "Unknown frame type"
        ws.send_json({"type": "message", "body": "   "})
        assert ws.receive_json()["code"] == 422
        ws.send_json({"type": "message", "body": "x" * (settings.chat_max_message_chars + 1)})
        assert ws.receive_json()["code"] == 422
        ws.send_json({"type": "ping"})  # still alive after all of that
        assert ws.receive_json() == {"type": "pong"}


def test_rate_limit_returns_an_error_frame_not_a_disconnect(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "chat_messages_per_minute", 2)
    chat_id = open_chat(client, auth["alice"])
    with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as ws:
        ready(ws)
        ws.send_json({"type": "message", "body": "What is the capital of France?"})
        until(ws, "status")  # handed off to the queue
        ws.send_json({"type": "message", "body": "hello?"})  # a waiting chat just records the message
        assert until(ws, "message")["message"]["body"] == "hello?"
        ws.send_json({"type": "message", "body": "anybody?"})  # third within a minute, limit is 2
        err = until(ws, "error")
        assert err["code"] == 429
        ws.send_json({"type": "ping"})
        assert until(ws, "pong")


# ---------- hand-off, queue, and a full live conversation ----------
def test_handoff_notifies_the_staff_queue_in_real_time(client, auth):
    chat_id = open_chat(client, auth["alice"])
    with connect(client, "/api/chats/queue/ws", auth["agent"]) as queue:
        assert queue.receive_json() == {"type": "queue", "chats": []}
        assert hub.agents_online()
        with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as ws:
            ready(ws)
            ws.send_json({"type": "message", "body": "I want to speak to a human"})
            assert until(ws, "message")["message"]["sender_type"] == "customer"
            system = until(ws, "message")["message"]
            assert system["sender_type"] == "system" and "support ticket" not in system["body"]  # an agent IS online
            assert until(ws, "status")["status"] == "waiting"
        event = until(queue, "chat_waiting")
        assert event["chat"]["id"] == chat_id and event["chat"]["handoff_reason"] == "Customer asked for a human"


def test_queue_socket_starts_with_the_current_queue(client, auth):
    chat_id = open_chat(client, auth["alice"])
    client.post(f"/api/chats/{chat_id}/messages", headers=auth["alice"], json={"body": "agent"})
    with connect(client, "/api/chats/queue/ws", auth["agent"]) as queue:
        assert [c["id"] for c in queue.receive_json()["chats"]] == [chat_id]


def test_full_live_conversation(client, auth):
    chat_id = open_chat(client, auth["alice"])
    path = f"/api/chats/{chat_id}/ws"
    with connect(client, "/api/chats/queue/ws", auth["agent"]) as queue, \
         connect(client, path, auth["alice"]) as customer:
        queue.receive_json()
        ready(customer)
        customer.send_json({"type": "message", "body": "agent"})
        until(queue, "chat_waiting")
        assert until(customer, "status")["status"] == "waiting"

        with connect(client, path, auth["agent"]) as agent:
            ready(agent)
            # the agent clicks "claim" (REST); both sides and the queue hear about it instantly
            assert client.post(f"/api/chats/{chat_id}/claim", headers=auth["agent"]).status_code == 200
            notice = until(customer, "message")["message"]
            assert notice["sender_type"] == "system" and notice["body"] == "agent joined the chat."
            joined = until(customer, "status")
            assert joined["status"] == "active" and joined["agent"]["name"] == "agent"
            assert until(queue, "chat_claimed")["chat_id"] == chat_id
            assert until(agent, "status")["status"] == "active"

            agent.send_json({"type": "message", "body": "Hi Alice, I'm here to help."})
            to_customer = until(customer, "message")["message"]
            assert (to_customer["sender_type"], to_customer["body"]) == ("agent", "Hi Alice, I'm here to help.")
            echo = until(agent, "message")["message"]  # the sender sees their own message too
            assert (echo["sender_type"], echo["body"]) == ("agent", "Hi Alice, I'm here to help.")

            customer.send_json({"type": "message", "body": "My invoice has the wrong amount."})
            to_agent = until(agent, "message")["message"]
            assert (to_agent["sender_type"], to_agent["body"]) == ("customer", "My invoice has the wrong amount.")

            # close from the agent side; everyone is told
            assert client.post(f"/api/chats/{chat_id}/close", headers=auth["agent"]).status_code == 200
            assert until(customer, "status")["status"] == "closed"
            assert until(agent, "status")["status"] == "closed"

    history = client.get(f"/api/chats/{chat_id}", headers=auth["alice"]).json()["messages"]
    bodies = [(m["sender_type"], m["body"]) for m in history]
    assert ("agent", "Hi Alice, I'm here to help.") in bodies
    assert ("customer", "My invoice has the wrong amount.") in bodies
    assert not any(t == "bot" and "invoice" in b.lower() for t, b in bodies)  # the bot stayed out of it


def test_agent_cannot_speak_before_claiming(client, auth):
    chat_id = open_chat(client, auth["alice"])
    client.post(f"/api/chats/{chat_id}/messages", headers=auth["alice"], json={"body": "agent"})
    with connect(client, f"/api/chats/{chat_id}/ws", auth["agent"]) as ws:
        ready(ws)
        ws.send_json({"type": "message", "body": "hello"})
        err = ws.receive_json()
        assert err["code"] == 403 and "Claim" in err["detail"]
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}  # still connected


def test_messages_over_rest_reach_websocket_clients(client, auth):
    chat_id = open_chat(client, auth["alice"])
    client.post(f"/api/chats/{chat_id}/messages", headers=auth["alice"], json={"body": "agent"})
    client.post(f"/api/chats/{chat_id}/claim", headers=auth["agent"])
    with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as customer:
        ready(customer)
        client.post(f"/api/chats/{chat_id}/messages", headers=auth["agent"], json={"body": "Sent over plain HTTP"})
        assert until(customer, "message")["message"]["body"] == "Sent over plain HTTP"


def test_a_closed_chat_rejects_messages_with_an_error_frame(client, auth):
    chat_id = open_chat(client, auth["alice"])
    with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as ws:
        ready(ws)
        client.post(f"/api/chats/{chat_id}/close", headers=auth["alice"])
        assert until(ws, "status")["status"] == "closed"
        ws.send_json({"type": "message", "body": "anyone there?"})
        assert until(ws, "error")["code"] == 409


def test_account_disabled_mid_chat_is_disconnected(client, auth, users, db_session):
    chat_id = open_chat(client, auth["alice"])
    with pytest.raises(WebSocketDisconnect) as info:
        with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as ws:
            ready(ws)
            users["alice"].is_active = False
            db_session.commit()
            ws.send_json({"type": "message", "body": "hello"})
            until(ws, "error")
            ws.receive_json()
    assert info.value.code == 4403


def test_two_customers_never_receive_each_others_chat(client, auth):
    a, b = open_chat(client, auth["alice"]), open_chat(client, auth["bob"])
    with connect(client, f"/api/chats/{a}/ws", auth["alice"]) as alice, connect(client, f"/api/chats/{b}/ws", auth["bob"]) as bob:
        ready(alice), ready(bob)
        alice.send_json({"type": "message", "body": "my secret: hunter2 is my pin"})
        until(alice, "message")
        bob.send_json({"type": "ping"})
        assert bob.receive_json() == {"type": "pong"}  # the next frame bob sees is his pong, not alice's message


def test_hub_forgets_sockets_after_disconnect(client, auth):
    chat_id = open_chat(client, auth["alice"])
    with connect(client, f"/api/chats/{chat_id}/ws", auth["alice"]) as ws:
        ready(ws)
        assert hub.room_size(chat_id) == 1
    for _ in range(40):  # cleanup runs on the socket's own loop: give it a moment
        if hub.room_size(chat_id) == 0:
            break
        time.sleep(0.05)
    assert hub.room_size(chat_id) == 0
