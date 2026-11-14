import pytest

from app.core.config import settings
from app.models import ChatMessage, ChatStatus, SenderType, Ticket, UserRole
from app.services import chat as svc
from app.services import kb
from app.services.chat import ChatError, wants_human
from scripts.kb_articles import ARTICLES
from tests.conftest import make_user


def start(db, users, who="alice"):
    chat, created, _ = svc.open_chat(db, users[who].id)
    return chat


def say(db, chat, users, who, text, agents_online=False):
    return svc.send_message(db, chat.id, users[who].id, text, agents_online=agents_online)


def kinds(outcome):
    return [m.sender_type for m in outcome.messages]


# ---------- "talk to a person" detection ----------
@pytest.mark.parametrize("text", [
    "agent", "Human please", "representative", "I want to speak to a human", "can I talk to someone about my invoice",
    "get me a real person", "I need a manager", "connect me to an agent", "this is a live agent, right? no I want a live agent",
    "let me speak with a customer service representative", "Transfer me to support team",
])
def test_wants_human_positive(text):
    assert wants_human(text)


@pytest.mark.parametrize("text", [
    "How do I reset my password?", "I was charged twice", "I spoke to an agent yesterday and they told me to reset it",
    "Does your support team work on weekends?", "What is a human readable invoice?", "where is my order",
    "I can't talk right now, send me an email", "my agent id is 4821",
])
def test_wants_human_negative(text):
    assert not wants_human(text)


# ---------- opening a chat ----------
def test_open_chat_creates_greeting_and_reuses_open_chat(db_session, users):
    chat, created, outcome = svc.open_chat(db_session, users["alice"].id)
    assert created and chat.status == ChatStatus.bot
    assert kinds(outcome) == [SenderType.bot] and "Hi alice" in outcome.messages[0].body
    again, created2, _ = svc.open_chat(db_session, users["alice"].id)
    assert again.id == chat.id and not created2


def test_new_chat_allowed_after_closing_and_staff_cannot_open(db_session, users):
    chat = start(db_session, users)
    svc.close_chat(db_session, chat.id, users["alice"].id)
    assert start(db_session, users).id != chat.id
    with pytest.raises(ChatError) as e:
        svc.open_chat(db_session, users["agent"].id)
    assert e.value.status_code == 403


# ---------- the bot's turn ----------
def test_bot_answers_from_the_knowledge_base_with_sources(db_session, users, seeded_kb):
    chat = start(db_session, users)
    out = say(db_session, chat, users, "alice", "I forgot my password, how do I reset it?")
    assert kinds(out) == [SenderType.customer, SenderType.bot]
    bot = out.messages[1]
    assert "reset" in bot.body.lower() and 'type "agent"' in bot.body
    assert bot.sources and bot.sources[0].title == "Resetting your password"
    assert chat.status == ChatStatus.bot and chat.handoff_reason is None


def test_off_topic_question_is_handed_off(db_session, users, seeded_kb):
    chat = start(db_session, users)
    out = say(db_session, chat, users, "alice", "What is the capital of France?")
    assert kinds(out) == [SenderType.customer, SenderType.system]
    assert chat.status == ChatStatus.waiting and chat.handoff_reason == "Bot could not find a confident answer"
    assert [e["type"] for e in out.queue] == ["chat_waiting"]
    assert out.queue[0]["chat"]["handoff_reason"]  # staff see why


def test_explicit_request_for_a_person_skips_the_bot(db_session, users, seeded_kb):
    chat = start(db_session, users)
    out = say(db_session, chat, users, "alice", "I want to speak to a human about my password")
    assert SenderType.bot not in kinds(out)  # even though the KB could have answered
    assert chat.status == ChatStatus.waiting and chat.handoff_reason == "Customer asked for a human"


def test_upset_customer_goes_straight_to_a_human(db_session, users, seeded_kb):
    chat = start(db_session, users)
    say(db_session, chat, users, "alice", "This is unacceptable, the worst service ever!!!")
    assert chat.status == ChatStatus.waiting and chat.handoff_reason == "Customer seems upset"


def test_frustration_after_a_bot_answer_hands_off_but_not_before(db_session, users, seeded_kb):
    first = start(db_session, users, "alice")
    out = say(db_session, first, users, "alice", "I'm still waiting, third time asking how to reset my password")
    assert first.status == ChatStatus.bot  # no previous bot answer yet: give the bot a chance
    assert SenderType.bot in kinds(out)

    say(db_session, first, users, "alice", "That didn't help, I'm still frustrated and nobody fixes this")
    assert first.status == ChatStatus.waiting
    assert first.handoff_reason == "Customer still frustrated after a bot answer"


def test_handoff_message_depends_on_whether_agents_are_online(db_session, users, seeded_kb):
    offline, online = start(db_session, users, "alice"), start(db_session, users, "bob")
    o1 = say(db_session, offline, users, "alice", "agent", agents_online=False)
    o2 = say(db_session, online, users, "bob", "agent", agents_online=True)
    assert "support ticket" in o1.messages[1].body
    assert "support ticket" not in o2.messages[1].body


def test_bot_stays_silent_once_a_human_is_involved(db_session, users, seeded_kb):
    chat = start(db_session, users)
    say(db_session, chat, users, "alice", "agent")
    out = say(db_session, chat, users, "alice", "How do I reset my password?")  # waiting
    assert kinds(out) == [SenderType.customer]
    svc.claim_chat(db_session, chat.id, users["agent"].id)
    out = say(db_session, chat, users, "alice", "How do I reset my password?")  # active
    assert kinds(out) == [SenderType.customer]


def test_message_validation(db_session, users, monkeypatch):
    chat = start(db_session, users)
    for bad in ("", "   \n "):
        with pytest.raises(ChatError) as e:
            say(db_session, chat, users, "alice", bad)
        assert e.value.status_code == 422
    with pytest.raises(ChatError) as e:
        say(db_session, chat, users, "alice", "x" * (settings.chat_max_message_chars + 1))
    assert e.value.status_code == 422
    svc.close_chat(db_session, chat.id, users["alice"].id)
    with pytest.raises(ChatError) as e:
        say(db_session, chat, users, "alice", "hello")
    assert e.value.status_code == 409


def test_rate_limit(db_session, users, seeded_kb, monkeypatch):
    monkeypatch.setattr(settings, "chat_messages_per_minute", 3)
    chat = start(db_session, users)
    for _ in range(3):
        say(db_session, chat, users, "alice", "How do I reset my password?")
    with pytest.raises(ChatError) as e:
        say(db_session, chat, users, "alice", "How do I reset my password?")
    assert e.value.status_code == 429


def test_customers_cannot_see_or_use_each_others_chats(db_session, users):
    chat = start(db_session, users, "alice")
    for fn in (lambda: say(db_session, chat, users, "bob", "hi"), lambda: svc.snapshot(db_session, chat.id, users["bob"].id),
               lambda: svc.close_chat(db_session, chat.id, users["bob"].id)):
        with pytest.raises(ChatError) as e:
            fn()
        assert e.value.status_code == 404  # 404, not 403: ids cannot be probed


def test_only_relevant_articles_are_cited(db_session, users, seeded_kb):
    chat = start(db_session, users)
    out = say(db_session, chat, users, "alice", "I forgot my password, how do I reset it?")
    sources = out.messages[1].sources
    assert sources[0].title == "Resetting your password"
    best = sources[0].score
    assert all(s.score >= 0.6 * best for s in sources)  # no weakly related extras


# ---------- agents ----------
def waiting_chat(db, users, seeded=True):
    chat = start(db, users)
    say(db, chat, users, "alice", "agent")
    return chat


def test_agent_must_claim_before_replying(db_session, users):
    chat = waiting_chat(db_session, users)
    with pytest.raises(ChatError) as e:
        say(db_session, chat, users, "agent", "hello")
    assert e.value.status_code == 403
    out = svc.claim_chat(db_session, chat.id, users["agent"].id)
    assert chat.status == ChatStatus.active and chat.agent_id == users["agent"].id
    assert "joined the chat" in out.messages[0].body
    assert [e["type"] for e in out.queue] == ["chat_claimed"]
    reply = say(db_session, chat, users, "agent", "Hi, how can I help?")
    assert kinds(reply) == [SenderType.agent] and reply.messages[0].sender_name == "agent"


def test_only_one_agent_can_claim_a_chat(db_session, users):
    chat = waiting_chat(db_session, users)
    other = make_user(db_session, "agent2@example.com", UserRole.agent)
    svc.claim_chat(db_session, chat.id, users["agent"].id)
    with pytest.raises(ChatError) as e:
        svc.claim_chat(db_session, chat.id, other.id)
    assert e.value.status_code == 409
    with pytest.raises(ChatError) as e:  # and the loser cannot speak in the chat either
        svc.send_message(db_session, chat.id, other.id, "hello")
    assert e.value.status_code == 403


def test_customers_cannot_claim_and_missing_chats_404(db_session, users):
    chat = waiting_chat(db_session, users)
    with pytest.raises(ChatError) as e:
        svc.claim_chat(db_session, chat.id, users["alice"].id)
    assert e.value.status_code == 403
    with pytest.raises(ChatError) as e:
        svc.claim_chat(db_session, 999, users["agent"].id)
    assert e.value.status_code == 404


def test_agent_can_take_over_a_bot_chat_and_it_counts_as_a_handoff(db_session, users):
    chat = start(db_session, users)
    svc.claim_chat(db_session, chat.id, users["agent"].id)
    assert chat.status == ChatStatus.active and chat.handoff_reason == "Agent took over from the bot"


def test_closing_rules(db_session, users):
    chat = waiting_chat(db_session, users)
    svc.claim_chat(db_session, chat.id, users["agent"].id)
    stranger = make_user(db_session, "agent2@example.com", UserRole.agent)
    with pytest.raises(ChatError) as e:
        svc.close_chat(db_session, chat.id, stranger.id)  # an agent who is not assigned
    assert e.value.status_code == 403
    out = svc.close_chat(db_session, chat.id, users["agent"].id)
    assert chat.status == ChatStatus.closed and chat.closed_at and out.events[-1]["status"] == "closed"
    with pytest.raises(ChatError) as e:
        svc.close_chat(db_session, chat.id, users["admin"].id)
    assert e.value.status_code == 409
    other = start(db_session, users, "bob")
    assert svc.close_chat(db_session, other.id, users["admin"].id).events  # admins can close any chat


def test_closing_a_waiting_chat_removes_it_from_the_queue(db_session, users):
    chat = waiting_chat(db_session, users)
    out = svc.close_chat(db_session, chat.id, users["alice"].id)
    assert {"type": "chat_closed", "chat_id": chat.id} in out.queue


# ---------- chat -> ticket ----------
def test_conversion_builds_a_ticket_from_the_transcript(db_session, users, seeded_kb):
    chat = start(db_session, users)
    say(db_session, chat, users, "alice", "How do I reset my password?")
    say(db_session, chat, users, "alice", "agent")
    svc.claim_chat(db_session, chat.id, users["agent"].id)
    say(db_session, chat, users, "agent", "I'll send you a link now.")
    ticket, out = svc.convert_to_ticket(db_session, chat.id, users["alice"].id)

    assert ticket.subject == "Chat: How do I reset my password?" and ticket.customer_id == users["alice"].id
    lines = ticket.description.splitlines()
    assert lines[0] == "Chat transcript:"
    assert any(l.startswith("Customer: How do I reset") for l in lines)
    assert any(l.startswith("Support bot:") for l in lines)
    assert "Agent (agent): I'll send you a link now." in lines
    assert "System" not in ticket.description and "joined the chat" not in ticket.description
    assert chat.status == ChatStatus.closed and chat.ticket_id == ticket.id
    assert f"ticket #{ticket.id}" in out.messages[0].body


def test_conversion_rules(db_session, users):
    chat = start(db_session, users)  # only the bot greeting so far
    with pytest.raises(ChatError) as e:
        svc.convert_to_ticket(db_session, chat.id, users["alice"].id)
    assert e.value.status_code == 422
    say(db_session, chat, users, "alice", "x" * 200)
    with pytest.raises(ChatError) as e:
        svc.convert_to_ticket(db_session, chat.id, users["bob"].id)
    assert e.value.status_code == 404
    ticket, _ = svc.convert_to_ticket(db_session, chat.id, users["agent"].id)  # staff may convert too
    assert len(ticket.subject) <= 86 and ticket.subject.endswith("...")
    with pytest.raises(ChatError) as e:
        svc.convert_to_ticket(db_session, chat.id, users["alice"].id)
    assert e.value.status_code == 409


def test_very_long_transcripts_are_truncated(db_session, users, monkeypatch):
    monkeypatch.setattr(settings, "chat_messages_per_minute", 1000)
    chat = start(db_session, users)
    for _ in range(8):
        svc.send_message(db_session, chat.id, users["alice"].id, "word " * 390)
    ticket, _ = svc.convert_to_ticket(db_session, chat.id, users["alice"].id)
    assert len(ticket.description) <= 5000 and ticket.description.endswith("(truncated)")


# ---------- stats ----------
def test_chat_stats(db_session, users, seeded_kb):
    assert svc.chat_stats(db_session) == {"chats_total": 0, "chats_handed_off": 0, "handoff_rate": None}
    solved = start(db_session, users, "alice")
    say(db_session, solved, users, "alice", "How do I reset my password?")
    escalated = start(db_session, users, "bob")
    say(db_session, escalated, users, "bob", "agent")
    assert svc.chat_stats(db_session) == {"chats_total": 2, "chats_handed_off": 1, "handoff_rate": 0.5}
