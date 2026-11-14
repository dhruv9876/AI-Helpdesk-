import pytest

from app.api.ticket_ai import get_llm, get_triager
from app.core.config import settings
from app.main import app
from app.models import Comment, SuggestedReply, Ticket
from app.services import kb
from app.services.llm import NOT_FOUND
from app.services.triage import TriageResult
from app.models import Sentiment, TicketPriority
from scripts.kb_articles import ARTICLES


def make(client, headers, subject, description):
    res = client.post("/api/tickets", headers=headers, json={"subject": subject, "description": description})
    assert res.status_code == 201, res.text
    return res.json()


def staff_view(client, auth, tid):
    res = client.get(f"/api/tickets/{tid}", headers=auth["agent"])
    assert res.status_code == 200
    return res.json()


# ---------- automatic triage on creation ----------
def test_ticket_is_triaged_in_background(client, auth, ai_on):
    t = make(client, auth["alice"], "App crashes on startup", "Since the update the app closes right after the splash screen.")
    full = staff_view(client, auth, t["id"])
    assert full["ai"]["category"] == "technical" and full["ai"]["priority"] == "high"
    assert full["ai"]["source"] == "rules-v1" and full["ai"]["triaged_at"]
    # first triage fills the empty real fields
    assert (full["category"], full["priority"]) == ("technical", "high")


def test_creation_response_is_not_delayed_or_changed_by_triage(client, auth, ai_on):
    t = make(client, auth["alice"], "App crashes on startup", "closes right after the splash screen")
    assert t["priority"] == "medium" and t["category"] is None  # response is sent before the background task


def test_triage_can_be_switched_off(client, auth):
    t = make(client, auth["alice"], "App crashes on startup", "closes right after the splash screen")
    assert staff_view(client, auth, t["id"])["ai"]["triaged_at"] is None


def test_customers_never_see_ai_fields(client, auth, ai_on):
    t = make(client, auth["alice"], "This is unacceptable", "WORST service. My refund is missing!!!")
    assert staff_view(client, auth, t["id"])["ai"]["sentiment"] == "angry"
    assert client.get(f"/api/tickets/{t['id']}", headers=auth["alice"]).json()["ai"] is None
    listing = client.get("/api/tickets", headers=auth["alice"]).json()["items"]
    assert all(i["ai"] is None for i in listing)
    # filtering by the hidden flag must not work for customers (it would leak it)
    flagged = client.get("/api/tickets?needs_attention=false", headers=auth["alice"]).json()
    assert flagged["total"] == 1


def test_pipeline_failure_does_not_break_ticket_creation(client, auth, ai_on, monkeypatch):
    class Boom:
        name = "boom"
        def triage(self, *a): raise RuntimeError("model is down")
    monkeypatch.setattr("app.services.ai_pipeline.get_triager", lambda: Boom())
    t = make(client, auth["alice"], "Where is my order", "tracking has not updated")
    full = staff_view(client, auth, t["id"])
    assert full["subject"] == "Where is my order" and full["ai"]["triaged_at"] is None


# ---------- attention flags ----------
def test_angry_and_urgent_tickets_are_flagged(client, auth, ai_on):
    calm = make(client, auth["alice"], "Change my email", "how do I change my email address")
    angry = make(client, auth["bob"], "Unacceptable service", "This is the worst. Nobody replies and I'm sick of waiting.")
    urgent = make(client, auth["alice"], "URGENT hacked", "I think someone hacked my account, please help immediately")
    assert staff_view(client, auth, calm["id"])["ai"]["needs_attention"] is False
    a = staff_view(client, auth, angry["id"])["ai"]
    assert a["needs_attention"] and "angry" in a["attention_reason"]
    assert "urgent" in staff_view(client, auth, urgent["id"])["ai"]["attention_reason"]

    flagged = client.get("/api/tickets?needs_attention=true", headers=auth["agent"]).json()
    assert {i["id"] for i in flagged["items"]} == {angry["id"], urgent["id"]}


# ---------- re-triage ----------
class FixedTriager:
    name = "fixed"
    def __init__(self, category, priority):
        self.result = TriageResult(category, priority, Sentiment.neutral, 0.9, "fixed")
    def triage(self, *a): return self.result


def test_retriage_updates_suggestion_but_keeps_human_edits(client, auth):
    t = make(client, auth["alice"], "Refund question", "when will I get it")
    client.patch(f"/api/tickets/{t['id']}", headers=auth["agent"], json={"priority": "low", "category": "billing"})
    app.dependency_overrides[get_triager] = lambda: FixedTriager("shipping", TicketPriority.urgent)

    res = client.post(f"/api/tickets/{t['id']}/triage", headers=auth["agent"]).json()
    assert res["ai"]["category"] == "shipping" and res["ai"]["priority"] == "urgent"
    assert (res["category"], res["priority"]) == ("billing", "low")  # untouched

    applied = client.post(f"/api/tickets/{t['id']}/triage?apply=true", headers=auth["agent"]).json()
    assert (applied["category"], applied["priority"]) == ("shipping", "urgent")
    assert applied["ai"]["needs_attention"] is True


# ---------- duplicates ----------
def test_duplicate_is_detected_and_flagged_as_repeat_contact(client, auth, ai_on):
    first = make(client, auth["alice"], "Charged twice", "I see two charges on my card this month.")
    second = make(client, auth["alice"], "Double charged for my subscription", "My card was billed two times this month.")
    ai = staff_view(client, auth, second["id"])["ai"]
    assert ai["possible_duplicate_of_id"] == first["id"] and ai["duplicate_score"] >= settings.dup_min_score
    assert "repeat contact" in ai["attention_reason"]


def test_unrelated_tickets_are_not_flagged_as_duplicates(client, auth, ai_on):
    make(client, auth["alice"], "Charged twice", "I see two charges on my card this month.")
    other = make(client, auth["alice"], "App crashes on startup", "It closes right after the splash screen.")
    assert staff_view(client, auth, other["id"])["ai"]["possible_duplicate_of_id"] is None


def test_duplicates_endpoint_and_permissions(client, auth, ai_on):
    a = make(client, auth["alice"], "Charged twice", "I see two charges on my card this month.")
    b = make(client, auth["bob"], "Double charged", "My card was billed two times this month.")
    found = client.get(f"/api/tickets/{b['id']}/duplicates", headers=auth["agent"]).json()
    assert [d["ticket_id"] for d in found] == [a["id"]]
    assert found[0]["same_customer"] is False and found[0]["can_merge"] is False
    assert client.get(f"/api/tickets/{b['id']}/duplicates", headers=auth["bob"]).status_code == 403


def test_closed_tickets_are_not_duplicate_candidates(client, auth, ai_on):
    a = make(client, auth["alice"], "Charged twice", "I see two charges on my card this month.")
    client.patch(f"/api/tickets/{a['id']}", headers=auth["agent"], json={"status": "closed"})
    b = make(client, auth["bob"], "Double charged", "My card was billed two times this month.")
    assert client.get(f"/api/tickets/{b['id']}/duplicates", headers=auth["agent"]).json() == []


# ---------- merge ----------
def two_tickets(client, auth, customer="alice"):
    a = make(client, auth[customer], "Charged twice", "I see two charges on my card this month.")
    b = make(client, auth[customer], "Double charged", "My card was billed two times this month.")
    return a, b


def test_merge_closes_duplicate_and_leaves_notes(client, auth):
    original, dup = two_tickets(client, auth)
    res = client.post(f"/api/tickets/{dup['id']}/merge", headers=auth["agent"], json={"target_id": original["id"]})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "closed" and body["merged_into_id"] == original["id"]
    notes = lambda tid: [c["body"] for c in staff_view(client, auth, tid)["comments"]]
    assert any(f"#{original['id']}" in n for n in notes(dup["id"]))
    assert any(f"#{dup['id']}" in n for n in notes(original["id"]))
    # customers see the link but not the internal notes
    cust = client.get(f"/api/tickets/{dup['id']}", headers=auth["alice"]).json()
    assert cust["merged_into_id"] == original["id"] and cust["comments"] == []


def test_merge_rules(client, auth):
    a, b = two_tickets(client, auth)
    other = make(client, auth["bob"], "Something else", "unrelated")
    url = f"/api/tickets/{b['id']}/merge"
    assert client.post(url, headers=auth["agent"], json={"target_id": b["id"]}).status_code == 422  # itself
    assert client.post(url, headers=auth["agent"], json={"target_id": 999}).status_code == 404
    assert client.post(url, headers=auth["agent"], json={"target_id": other["id"]}).status_code == 409  # other customer
    assert client.post(url, headers=auth["alice"], json={"target_id": a["id"]}).status_code == 403
    assert client.post(url, headers=auth["agent"], json={"target_id": a["id"]}).status_code == 200
    assert client.post(url, headers=auth["agent"], json={"target_id": a["id"]}).status_code == 409  # already merged


def test_merged_tickets_stop_being_duplicate_candidates(client, auth, ai_on):
    a, b = two_tickets(client, auth)
    client.post(f"/api/tickets/{b['id']}/merge", headers=auth["agent"], json={"target_id": a["id"]})
    c = make(client, auth["bob"], "Charged two times", "Two charges appeared on my card this month.")
    assert [d["ticket_id"] for d in client.get(f"/api/tickets/{c['id']}/duplicates", headers=auth["agent"]).json()] == [a["id"]]


def test_deleting_a_ticket_clears_references_to_it(client, auth, db_session):
    a, b = two_tickets(client, auth)
    client.post(f"/api/tickets/{b['id']}/merge", headers=auth["agent"], json={"target_id": a["id"]})
    assert client.delete(f"/api/tickets/{a['id']}", headers=auth["admin"]).status_code == 204
    assert client.get(f"/api/tickets/{b['id']}", headers=auth["agent"]).json()["merged_into_id"] is None


# ---------- suggested replies ----------
def test_suggest_reply_drafts_from_the_knowledge_base(client, auth, seeded_kb):
    t = make(client, auth["alice"], "Cannot reset my password", "The reset email never arrives, I checked spam too.")
    res = client.post(f"/api/tickets/{t['id']}/suggest-reply", headers=auth["agent"])
    assert res.status_code == 200
    body = res.json()
    assert body["escalate"] is False
    draft = body["draft"]
    assert draft["status"] == "pending" and draft["body"].startswith("Hi alice,")
    assert "Support Team" in draft["body"]
    assert draft["sources"][0]["title"] == "Resetting your password"
    # nothing has been sent to the customer yet
    assert client.get(f"/api/tickets/{t['id']}", headers=auth["alice"]).json()["comments"] == []


def test_no_draft_when_knowledge_base_cannot_answer(client, auth, seeded_kb):
    t = make(client, auth["alice"], "What is the capital of France", "Also who won the cricket match")
    body = client.post(f"/api/tickets/{t['id']}/suggest-reply", headers=auth["agent"]).json()
    assert body["draft"] is None and body["escalate"] is True and body["reason"]
    assert client.get(f"/api/tickets/{t['id']}/suggested-replies", headers=auth["agent"]).json() == []


def test_no_draft_when_llm_says_not_found(client, auth, seeded_kb):
    class Refuses:
        name = "refuses"
        def draft_reply(self, *a): return NOT_FOUND
    app.dependency_overrides[get_llm] = lambda: Refuses()
    t = make(client, auth["alice"], "Cannot reset my password", "The reset email never arrives.")
    assert client.post(f"/api/tickets/{t['id']}/suggest-reply", headers=auth["agent"]).json()["draft"] is None


def test_accept_posts_the_draft_as_a_public_comment(client, auth, seeded_kb, db_session):
    t = make(client, auth["alice"], "Cannot reset my password", "The reset email never arrives.")
    draft = client.post(f"/api/tickets/{t['id']}/suggest-reply", headers=auth["agent"]).json()["draft"]
    res = client.post(f"/api/tickets/{t['id']}/suggested-replies/{draft['id']}/accept", headers=auth["agent"], json={})
    assert res.status_code == 200
    comment = res.json()
    assert comment["body"] == draft["body"] and comment["is_internal"] is False
    seen = client.get(f"/api/tickets/{t['id']}", headers=auth["alice"]).json()["comments"]
    assert [c["id"] for c in seen] == [comment["id"]]
    saved = client.get(f"/api/tickets/{t['id']}/suggested-replies", headers=auth["agent"]).json()[0]
    assert saved["status"] == "accepted" and saved["comment_id"] == comment["id"] and saved["resolved_at"]


def test_editing_before_accepting_is_recorded_as_edited(client, auth, seeded_kb):
    t = make(client, auth["alice"], "Cannot reset my password", "The reset email never arrives.")
    draft = client.post(f"/api/tickets/{t['id']}/suggest-reply", headers=auth["agent"]).json()["draft"]
    res = client.post(f"/api/tickets/{t['id']}/suggested-replies/{draft['id']}/accept",
                      headers=auth["agent"], json={"body": "Hi Alice, I have sent you a new reset link."})
    assert res.json()["body"] == "Hi Alice, I have sent you a new reset link."
    assert client.get(f"/api/tickets/{t['id']}/suggested-replies", headers=auth["agent"]).json()[0]["status"] == "edited"


def test_reject_and_state_rules(client, auth, seeded_kb):
    t = make(client, auth["alice"], "Cannot reset my password", "The reset email never arrives.")
    draft = client.post(f"/api/tickets/{t['id']}/suggest-reply", headers=auth["agent"]).json()["draft"]
    base = f"/api/tickets/{t['id']}/suggested-replies/{draft['id']}"
    res = client.post(f"{base}/reject", headers=auth["agent"], json={"reason": "wrong tone"})
    assert res.json()["status"] == "rejected" and res.json()["reject_reason"] == "wrong tone"
    assert client.get(f"/api/tickets/{t['id']}", headers=auth["alice"]).json()["comments"] == []
    # a decided draft cannot be decided again
    assert client.post(f"{base}/accept", headers=auth["agent"], json={}).status_code == 409
    assert client.post(f"{base}/reject", headers=auth["agent"], json={}).status_code == 409
    assert client.post(f"/api/tickets/{t['id']}/suggested-replies/999/accept", headers=auth["agent"], json={}).status_code == 404


def test_cannot_draft_or_accept_on_a_closed_ticket(client, auth, seeded_kb):
    t = make(client, auth["alice"], "Cannot reset my password", "The reset email never arrives.")
    draft = client.post(f"/api/tickets/{t['id']}/suggest-reply", headers=auth["agent"]).json()["draft"]
    client.patch(f"/api/tickets/{t['id']}", headers=auth["agent"], json={"status": "closed"})
    assert client.post(f"/api/tickets/{t['id']}/suggest-reply", headers=auth["agent"]).status_code == 409
    assert client.post(f"/api/tickets/{t['id']}/suggested-replies/{draft['id']}/accept", headers=auth["agent"], json={}).status_code == 409


# ---------- permissions ----------
def test_ai_endpoints_are_staff_only(client, auth, seeded_kb):
    t = make(client, auth["alice"], "Cannot reset my password", "The reset email never arrives.")
    base = f"/api/tickets/{t['id']}"
    calls = [
        ("post", f"{base}/triage", None), ("get", f"{base}/duplicates", None),
        ("post", f"{base}/suggest-reply", None), ("get", f"{base}/suggested-replies", None),
        ("post", f"{base}/merge", {"target_id": t["id"]}), ("get", "/api/ai/stats", None),
    ]
    for method, url, body in calls:
        res = getattr(client, method)(url, headers=auth["alice"], **({"json": body} if body else {}))
        assert res.status_code == 403, (method, url, res.status_code)
        anon = getattr(client, method)(url, **({"json": body} if body else {}))
        assert anon.status_code == 401, (method, url)


# ---------- stats ----------
def test_stats_track_acceptance_rate(client, auth, seeded_kb, ai_on):
    stats = client.get("/api/ai/stats", headers=auth["agent"]).json()
    assert stats["replies_suggested"] == 0 and stats["acceptance_rate"] is None

    ids = [make(client, auth["alice"], "Cannot reset my password", "The reset email never arrives.")["id"] for _ in range(1)]
    ids += [make(client, auth["bob"], "Need to cancel my plan", "Please cancel at the end of the billing period.")["id"]]
    ids += [make(client, auth["bob"], "App crashes on startup", "closes right after the splash screen")["id"]]
    drafts = [client.post(f"/api/tickets/{i}/suggest-reply", headers=auth["agent"]).json()["draft"] for i in ids]
    assert all(drafts)
    client.post(f"/api/tickets/{ids[0]}/suggested-replies/{drafts[0]['id']}/accept", headers=auth["agent"], json={})
    client.post(f"/api/tickets/{ids[1]}/suggested-replies/{drafts[1]['id']}/accept", headers=auth["agent"], json={"body": "edited text"})
    client.post(f"/api/tickets/{ids[2]}/suggested-replies/{drafts[2]['id']}/reject", headers=auth["agent"], json={})

    s = client.get("/api/ai/stats", headers=auth["agent"]).json()
    assert (s["replies_suggested"], s["replies_accepted"], s["replies_edited"], s["replies_rejected"], s["replies_pending"]) == (3, 1, 1, 1, 0)
    assert s["acceptance_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert s["tickets_total"] == 3 and s["tickets_triaged"] == 3


def test_repeat_contact_is_found_even_when_another_customers_ticket_matches_better(client, auth, ai_on):
    # Regression: repeat-contact used only the single best match, so a closer ticket from a
    # DIFFERENT customer hid the same customer's earlier duplicate.
    mine = make(client, auth["alice"], "Charged twice for my subscription", "I see two charges on my card this month.")
    make(client, auth["bob"], "Double charged for the plan", "My card was billed two times this month. Please fix.")
    again = make(client, auth["alice"], "Double charged for the plan", "My card was billed two times this month. Please fix.")
    ai = staff_view(client, auth, again["id"])["ai"]
    assert ai["possible_duplicate_of_id"] != mine["id"]  # the best match is bob's ticket (identical text)
    assert ai["needs_attention"] and "repeat contact" in ai["attention_reason"]
