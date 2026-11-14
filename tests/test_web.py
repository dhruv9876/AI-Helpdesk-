"""The dashboard, tested over HTTP (cookies, CSRF headers and htmx requests, like a browser sends)."""
import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import decode_access_token
from app.main import app
from app.models import ChatSession, Ticket, TicketStatus, KBArticle
from app.web.deps import COOKIE, csrf_for
from tests.conftest import PASSWORD, make_user
from app.models import UserRole


# ---------------------------------------------------------------- helpers
def sign_in(email: str) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"email": email, "password": PASSWORD})
    assert r.status_code == 303, r.text
    return c


def csrf(c: TestClient) -> str:
    return csrf_for(c.cookies[COOKIE])


def hx(c: TestClient, method: str, url: str, token: str | None = None, **kw):
    """An htmx-style request: HX-Request plus the CSRF header the page would add."""
    headers = {"HX-Request": "true", "X-CSRF-Token": token if token is not None else csrf(c), **kw.pop("headers", {})}
    return c.request(method, url, headers=headers, **kw)


def new_ticket(client, headers, subject="Cannot reset my password", description="The reset email never arrives."):
    res = client.post("/api/tickets", headers=headers, json={"subject": subject, "description": description})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def trigger(res) -> dict:
    import json
    return json.loads(res.headers.get("HX-Trigger", "{}"))


def toast_of(res) -> dict:
    return trigger(res).get("toast", {})


@pytest.fixture()
def agent(client, users):
    return sign_in("agent@example.com")


@pytest.fixture()
def admin(client, users):
    return sign_in("admin@example.com")


@pytest.fixture()
def customer(client, users):
    return sign_in("alice@example.com")


# ================================================================ sign in and sessions
def test_login_page_hides_demo_accounts_by_default(client, monkeypatch):
    page = client.get("/login")
    assert page.status_code == 200 and "Sign in" in page.text and "Demo accounts" not in page.text
    monkeypatch.setattr(settings, "show_demo_logins", True)
    assert "Demo accounts" in client.get("/login").text


def test_successful_login_sets_a_protected_cookie_and_routes_by_role(client, users):
    staff = TestClient(app, follow_redirects=False).post("/login", data={"email": "AGENT@example.com ", "password": PASSWORD})
    assert staff.status_code == 303 and staff.headers["location"] == "/dashboard"  # email is trimmed and case-insensitive
    cookie = staff.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax") and "Path=/" in cookie
    token = re.search(rf"{COOKIE}=([^;]+)", cookie).group(1)
    assert decode_access_token(token)["sub"] == str(users["agent"].id)
    cust = TestClient(app, follow_redirects=False).post("/login", data={"email": "alice@example.com", "password": PASSWORD})
    assert cust.headers["location"] == "/chat"


def test_failed_logins_show_a_message_and_set_no_cookie(client, users, db_session):
    c = TestClient(app, follow_redirects=False)
    bad = c.post("/login", data={"email": "agent@example.com", "password": "wrong-password"})
    assert bad.status_code == 401 and "Incorrect email or password" in bad.text
    assert "set-cookie" not in bad.headers and "wrong-password" not in bad.text
    assert 'value="agent@example.com"' in bad.text  # the email is kept so it can be corrected
    users["alice"].is_active = False
    db_session.commit()
    off = c.post("/login", data={"email": "alice@example.com", "password": PASSWORD})
    assert off.status_code == 403 and "Account is disabled" in off.text


@pytest.mark.parametrize("target", ["//evil.example", "/\\evil.example", "https://evil.example", "/login", "javascript:alert(1)"])
def test_login_redirect_target_cannot_leave_the_site(client, users, target):
    res = TestClient(app, follow_redirects=False).post("/login", data={"email": "agent@example.com", "password": PASSWORD, "next": target})
    assert res.headers["location"] == "/dashboard"


def test_login_remembers_where_you_were_going(client, users):
    res = TestClient(app, follow_redirects=False).post("/login", data={"email": "agent@example.com", "password": PASSWORD, "next": "/dashboard/tickets?status=open"})
    assert res.headers["location"] == "/dashboard/tickets?status=open"


def test_signed_in_users_skip_the_login_page(agent):
    assert agent.get("/login").headers["location"] == "/dashboard"
    assert agent.get("/").headers["location"] == "/dashboard"


def test_protected_pages_need_a_session(client):
    c = TestClient(app, follow_redirects=False)
    res = c.get("/dashboard/tickets?status=open")
    assert res.status_code == 303 and res.headers["location"] == "/login?next=/dashboard/tickets%3Fstatus%3Dopen"
    assert c.get("/").headers["location"] == "/login"
    htmx = c.get("/dashboard/tickets", headers={"HX-Request": "true"})
    assert htmx.status_code == 401 and htmx.headers["HX-Redirect"] == "/login"  # htmx cannot follow a 303 usefully
    api_style = c.get("/ws-token", headers={"Accept": "application/json"})
    assert api_style.status_code == 401 and api_style.json() == {"detail": "Not signed in"}


def test_garbage_and_expired_cookies_are_rejected(client, users):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(COOKIE, "not-a-token")
    assert c.get("/dashboard").status_code == 303
    from app.core.security import create_access_token
    expired = create_access_token(users["agent"].id, "agent", minutes=-5)
    c.cookies.set(COOKIE, expired)
    assert c.get("/dashboard").status_code == 303


def test_disabled_user_loses_access_immediately(agent, users, db_session):
    assert agent.get("/dashboard").status_code == 200
    users["agent"].is_active = False
    db_session.commit()
    assert agent.get("/dashboard").status_code == 303


def test_logout_needs_csrf_and_clears_the_cookie(agent):
    assert agent.post("/logout").status_code == 403
    res = hx(agent, "POST", "/logout")
    assert res.status_code == 200 and res.headers["HX-Redirect"] == "/login"
    assert "Max-Age=0" in res.headers["set-cookie"]


def test_ws_token_is_short_lived_and_not_cacheable(agent, users):
    res = agent.get("/ws-token", headers={"Accept": "application/json"})
    claims = decode_access_token(res.json()["token"])
    assert claims["sub"] == str(users["agent"].id) and res.headers["cache-control"] == "no-store"
    import time
    assert claims["exp"] - time.time() <= 125  # two minutes, not the hour the session lasts


def test_ws_token_authenticates_a_chat_socket(client, customer, users):
    chat_id = client.post("/api/chats", headers={"Authorization": "Bearer " + __import__("app.core.security", fromlist=["x"]).create_access_token(users["alice"].id, "customer")}).json()["id"]
    token = customer.get("/ws-token", headers={"Accept": "application/json"}).json()["token"]
    with client.websocket_connect(f"/api/chats/{chat_id}/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "ready"


# ================================================================ CSRF
def test_state_changing_requests_need_the_csrf_header(client, auth, agent):
    tid = new_ticket(client, auth["alice"])
    url = f"/dashboard/tickets/{tid}/fields"
    assert agent.post(url, data={"priority": "high"}, headers={"HX-Request": "true"}).status_code == 403  # no header
    assert hx(agent, "POST", url, token="0" * 64, data={"priority": "high"}).status_code == 403  # wrong token
    other_session = hx(agent, "POST", url, token=csrf_for("someone-elses-session"), data={"priority": "high"})
    assert other_session.status_code == 403  # a token is bound to one session
    forged_status = agent.post(url, data={"priority": "high"}, headers={"HX-Request": "true"})
    assert "reload the page" in toast_of(forged_status)["message"].lower()
    assert hx(agent, "POST", url, data={"priority": "high"}).status_code == 200


def test_pages_embed_the_csrf_token_for_htmx(agent):
    html = agent.get("/dashboard").text
    token = re.search(r'name="csrf-token" content="([0-9a-f]{64})"', html).group(1)
    assert token == csrf(agent) and f'"X-CSRF-Token": "{token}"' in html


# ================================================================ roles
def test_customers_cannot_open_staff_pages(client, customer, auth):
    tid = new_ticket(client, auth["alice"])
    for url in ("/dashboard", "/dashboard/tickets", f"/dashboard/tickets/{tid}", "/dashboard/chats", "/dashboard/kb", "/dashboard/users"):
        res = customer.get(url)
        assert res.status_code == 403 and "have access to this page" in res.text, url
    assert customer.get("/chat").status_code == 200


def test_customers_cannot_use_staff_actions_even_with_a_valid_token(client, customer, auth):
    tid = new_ticket(client, auth["alice"])
    for method, url, data in [
        ("POST", f"/dashboard/tickets/{tid}/fields", {"priority": "urgent"}),
        ("POST", f"/dashboard/tickets/{tid}/comments", {"body": "hi"}),
        ("POST", "/dashboard/kb", {"title": "Sneaky", "content": "x"}),
        ("POST", "/dashboard/chats/1/claim", {}),
    ]:
        assert hx(customer, method, url, data=data).status_code == 403, url


def test_staff_are_sent_away_from_the_customer_chat_page(agent):
    assert agent.get("/chat").headers["location"] == "/dashboard/chats"
    assert hx(agent, "POST", "/chat/1/close").status_code == 403


def test_only_admins_reach_user_management(agent, admin):
    assert agent.get("/dashboard/users").status_code == 403
    assert admin.get("/dashboard/users").status_code == 200
    assert hx(agent, "POST", "/dashboard/users", data={"full_name": "X Y", "email": "x@example.com", "password": "password123", "role": "admin"}).status_code == 403


def test_navigation_matches_the_role(agent, admin, customer):
    assert 'href="/dashboard/users"' not in agent.get("/dashboard").text
    assert 'href="/dashboard/users"' in admin.get("/dashboard").text
    page = customer.get("/chat").text
    assert 'href="/dashboard"' not in page and 'href="/chat"' in page


# ================================================================ security headers, assets, errors
def test_security_headers_on_pages_but_not_on_the_api_docs(agent, client, users):
    res = agent.get("/dashboard")
    csp = res.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "'unsafe-inline'" not in csp and "frame-ancestors 'none'" in csp
    assert res.headers["x-frame-options"] == "DENY" and res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["cache-control"] == "no-store"  # signed-in pages must not sit in a shared cache
    assert "content-security-policy" not in client.get("/docs").headers  # Swagger UI needs inline scripts
    assert "content-security-policy" not in client.get("/health").headers


def test_no_inline_scripts_or_styles_in_any_page(agent, admin, customer, client, auth):
    tid = new_ticket(client, auth["alice"])
    pages = [agent.get(u).text for u in ("/dashboard", "/dashboard/tickets", f"/dashboard/tickets/{tid}", "/dashboard/chats", "/dashboard/kb")]
    pages += [admin.get("/dashboard/users").text, customer.get("/chat").text, client.get("/login").text]
    for html in pages:
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script>"
        assert " style=" not in html and "<style" not in html and "onclick=" not in html and "hx-on" not in html


def test_static_assets_are_served(client):
    for path, kind in [("/static/css/app.css", "text/css"), ("/static/js/chat.js", "javascript"), ("/static/vendor/htmx.min.js", "javascript"),
                       ("/static/fonts/atkinson-400.woff2", "font"), ("/static/fonts/archivo-wdth.woff2", "font")]:
        res = client.get(path)
        assert res.status_code == 200 and len(res.content) > 500, path
        assert kind in res.headers["content-type"] or kind == "font", path


def test_unknown_pages_get_a_helpful_404(agent, client):
    res = agent.get("/dashboard/nope")
    assert res.status_code == 404 and "find that page" in res.text and 'href="/dashboard"' in res.text
    assert agent.get("/dashboard/tickets/9999").status_code == 404
    assert client.get("/api/nope").json() == {"detail": "Not Found"}  # the API keeps JSON errors


def test_api_validation_errors_stay_json_but_ui_errors_are_friendly(client, agent):
    assert client.post("/api/auth/register", json={}).status_code == 422
    assert client.post("/api/auth/register", json={}).json()["detail"]
    res = hx(agent, "POST", "/dashboard/kb/ask", data={})  # question missing entirely
    assert res.status_code == 200 and toast_of(res)["level"] == "error"
    bad_page = agent.get("/dashboard/tickets?page=abc")
    assert bad_page.status_code == 422 and "Check the page field" in bad_page.text


# ================================================================ pages render
def test_overview_lists_flagged_tickets_and_waiting_chats(client, auth, agent, ai_on):
    new_ticket(client, auth["alice"], "URGENT hacked account", "I think my account was hacked, help immediately")
    new_ticket(client, auth["bob"], "How do I change my email", "I want a new email address")
    cid = client.post("/api/chats", headers=auth["alice"]).json()["id"]
    client.post(f"/api/chats/{cid}/messages", headers=auth["alice"], json={"body": "agent"})
    html = agent.get("/dashboard").text
    assert "Needs attention" in html and "URGENT hacked account" in html and "urgent priority" in html
    assert "alice is waiting in chat" in html and "Customer asked for a human" in html
    assert "How do I change my email" not in html.split("How we're doing")[0]  # not flagged, so not in the queue section


def test_overview_empty_state(agent):
    html = agent.get("/dashboard").text
    assert "Nothing is waiting on you" in html and "no data yet" in html


def test_overview_numbers_are_correct(client, auth, agent):
    for i in range(3):
        new_ticket(client, auth["alice"], f"Ticket number {i}", "something")
    html = agent.get("/dashboard").text
    assert re.search(r"Active tickets</dt><dd class=\"num\">3<small>3 open, 0 pending", html)
    assert "3 in total" in html


# ================================================================ queue
def subject_ids(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'class="subject" href="/dashboard/tickets/(\d+)"', html)]


def test_queue_orders_flagged_then_priority_then_oldest(client, auth, agent, ai_on):
    low = new_ticket(client, auth["alice"], "Where is my order", "tracking has not updated")  # medium, not flagged
    angry = new_ticket(client, auth["bob"], "Unacceptable", "This is the worst service ever, nobody replies")  # flagged
    urgent = new_ticket(client, auth["alice"], "URGENT hacked", "my account was hacked, help immediately")  # flagged + urgent
    high = new_ticket(client, auth["bob"], "App crashes", "the app crashes on startup")  # high, not flagged
    html = agent.get("/dashboard/tickets").text
    assert subject_ids(html) == [urgent, angry, high, low]
    assert subject_ids(agent.get("/dashboard/tickets?sort=newest").text) == [high, urgent, angry, low]
    assert subject_ids(agent.get("/dashboard/tickets?sort=oldest").text) == [low, angry, urgent, high]


def test_queue_filters(client, auth, agent, users, db_session, ai_on):
    a = new_ticket(client, auth["alice"], "App crashes", "the app crashes on startup")
    b = new_ticket(client, auth["bob"], "Where is my parcel", "tracking stuck for days")
    client.patch(f"/api/tickets/{a}", headers=auth["agent"], json={"assignee_id": users["agent"].id})
    client.patch(f"/api/tickets/{b}", headers=auth["agent"], json={"status": "resolved"})
    get = lambda qs: subject_ids(agent.get("/dashboard/tickets?" + qs).text)
    assert get("") == [a]  # "active" hides resolved tickets by default
    assert set(get("status=all")) == {a, b} and get("status=resolved") == [b]
    assert get("category=technical") == [a] and get("category=shipping&status=all") == [b]
    assert get("assignee=me") == [a] and get("assignee=none&status=all") == [b]
    assert get(f"assignee={users['agent'].id}") == [a]
    assert get("priority=high") == [a] and get("q=parcel&status=all") == [b] and get(f"q=%23{a}") == [a]
    assert get("attention=1") == []
    assert set(get("status=banana")) == {a, b}  # an unknown status falls back to "all"
    assert get("priority=banana") == [a] and get("assignee=banana") == [a]  # other nonsense is ignored, not an error


def test_queue_pagination(client, auth, agent):
    ids = [new_ticket(client, auth["alice"], f"Ticket {i:02d}", "something") for i in range(20)]
    page1 = agent.get("/dashboard/tickets?sort=oldest").text
    assert subject_ids(page1) == ids[:15] and "1 to 15 of 20" in page1 and 'href="/dashboard/tickets?sort=oldest&amp;page=2"' in page1
    page2 = agent.get("/dashboard/tickets?sort=oldest&page=2").text
    assert subject_ids(page2) == ids[15:] and "16 to 20 of 20" in page2 and "Previous" in page2
    assert subject_ids(agent.get("/dashboard/tickets?sort=oldest&page=99").text) == ids[15:]  # clamped, not an error
    assert agent.get("/dashboard/tickets?page=0").status_code == 422  # a nonsense page number is refused, not guessed


def test_htmx_gets_a_fragment_with_a_clean_url_and_history_restore_gets_the_page(client, auth, agent):
    new_ticket(client, auth["alice"], "Needle in haystack", "x")
    frag = agent.get("/dashboard/tickets?q=Needle&priority=&category=", headers={"HX-Request": "true"})
    assert "<html" not in frag.text and "Needle in haystack" in frag.text and frag.headers["vary"] == "HX-Request"
    assert frag.headers["HX-Push-Url"] == "/dashboard/tickets?q=Needle"  # empty parameters are dropped
    full = agent.get("/dashboard/tickets?q=Needle", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert "<html" in full.text  # the back button needs the whole page


def test_empty_queue_states(client, auth, agent):
    assert "No tickets here yet" in agent.get("/dashboard/tickets").text
    new_ticket(client, auth["alice"])
    res = agent.get("/dashboard/tickets?q=zzzz")
    assert "No tickets match these filters" in res.text and "Clear filters" in res.text


# ================================================================ XSS
def test_user_written_text_is_escaped_everywhere(client, auth, agent, admin, db_session):
    payload = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    tid = new_ticket(client, auth["alice"], payload, payload)
    client.post(f"/api/tickets/{tid}/comments", headers=auth["alice"], json={"body": payload})
    client.patch(f"/api/tickets/{tid}", headers=auth["agent"], json={"category": '"><b>x</b>'})
    cid = client.post("/api/chats", headers=auth["alice"]).json()["id"]
    client.post(f"/api/chats/{cid}/messages", headers=auth["alice"], json={"body": payload})
    kb = admin.post("/dashboard/kb", headers={"HX-Request": "true", "X-CSRF-Token": csrf(admin)}, data={"title": "Evil", "content": "</textarea>" + payload})
    kb_id = int(kb.headers["HX-Redirect"].rsplit("/", 1)[1])
    pages = [agent.get("/dashboard/tickets?status=all").text, agent.get(f"/dashboard/tickets/{tid}").text, agent.get(f"/dashboard/chats/{cid}").text,
             agent.get("/dashboard/chats").text, admin.get(f"/dashboard/kb/{kb_id}").text, admin.get("/dashboard/kb").text]
    for html in pages:
        assert "<script>alert" not in html and "<img src=x" not in html and "<b>x</b>" not in html and "</textarea><script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages[1]


# ================================================================ ticket actions
def test_changing_fields_returns_the_sidebar_and_refreshes_the_header_chips(client, auth, agent, users, db_session):
    tid = new_ticket(client, auth["alice"])
    res = hx(agent, "POST", f"/dashboard/tickets/{tid}/fields", data={"status": "pending", "priority": "high", "category": "account", "assignee": str(users["agent"].id)})
    assert res.status_code == 200 and toast_of(res) == {"message": "Ticket updated.", "level": "success"}
    assert 'id="t-sidebar"' in res.text and 'id="t-chips"' in res.text and 'hx-swap-oob="true"' in res.text  # chips refresh out-of-band
    ticket = db_session.get(Ticket, tid)
    assert (ticket.status, ticket.priority.value, ticket.category, ticket.assignee_id) == (TicketStatus.pending, "high", "account", users["agent"].id)


def test_fields_not_sent_are_left_alone_and_empty_means_clear(client, auth, agent, users, db_session):
    tid = new_ticket(client, auth["alice"])
    hx(agent, "POST", f"/dashboard/tickets/{tid}/fields", data={"priority": "urgent", "category": "billing", "assignee": str(users["agent"].id)})
    hx(agent, "POST", f"/dashboard/tickets/{tid}/fields", data={"assignee": str(users["admin"].id)})  # only the assignee
    t = db_session.get(Ticket, tid)
    assert (t.priority.value, t.category, t.assignee_id) == ("urgent", "billing", users["admin"].id)
    form = agent.get(f"/dashboard/tickets/{tid}").text
    assert '<option value="none"' in form and '<option value="">' not in form  # an empty value can never mean "clear"
    hx(agent, "POST", f"/dashboard/tickets/{tid}/fields", data={"assignee": "none", "category": "none"})
    db_session.refresh(t)
    assert (t.assignee_id, t.category, t.priority.value) == (None, None, "urgent")
    # Regression: choosing "Unassigned" in the browser used to do nothing, because FastAPI treats ""
    # as a missing form value. An empty string must therefore be harmless, not a hidden "clear".
    hx(agent, "POST", f"/dashboard/tickets/{tid}/fields", data={"assignee": str(users["agent"].id)})
    hx(agent, "POST", f"/dashboard/tickets/{tid}/fields", data={"assignee": ""})
    db_session.refresh(t)
    assert t.assignee_id == users["agent"].id


def test_rejected_changes_show_an_error_and_leave_the_ticket_alone(client, auth, agent, users, db_session):
    tid = new_ticket(client, auth["alice"])
    for data, fragment in [({"status": "banana"}, "status"), ({"assignee": str(users["alice"].id)}, "agent or admin")]:
        res = hx(agent, "POST", f"/dashboard/tickets/{tid}/fields", data=data)
        assert res.status_code == 200 and toast_of(res)["level"] == "error" and fragment in toast_of(res)["message"]
        assert 'id="t-sidebar"' in res.text  # re-rendered so the form snaps back to what is saved
    t = db_session.get(Ticket, tid)
    db_session.refresh(t)
    assert (t.status, t.assignee_id) == (TicketStatus.open, None)


def test_replies_and_internal_notes(client, auth, agent):
    tid = new_ticket(client, auth["alice"])
    res = hx(agent, "POST", f"/dashboard/tickets/{tid}/comments", data={"body": "Hello Alice"})
    assert toast_of(res)["message"] == "Reply sent to the customer." and "Hello Alice" in res.text and "msg-agent" in res.text
    note = hx(agent, "POST", f"/dashboard/tickets/{tid}/comments", data={"body": "Customer is on the old plan", "internal": "1"})
    assert toast_of(note)["message"] == "Note saved." and "msg-note" in note.text and "hidden from the customer" in note.text
    seen = client.get(f"/api/tickets/{tid}", headers=auth["alice"]).json()["comments"]
    assert [c["body"] for c in seen] == ["Hello Alice"]  # the customer never sees the note


def test_empty_reply_and_closed_ticket_replies_are_refused(client, auth, agent):
    tid = new_ticket(client, auth["alice"])
    empty = hx(agent, "POST", f"/dashboard/tickets/{tid}/comments", data={"body": "   "})
    assert toast_of(empty)["level"] == "error" and "<div" not in empty.text
    hx(agent, "POST", f"/dashboard/tickets/{tid}/fields", data={"status": "closed"})
    closed = hx(agent, "POST", f"/dashboard/tickets/{tid}/comments", data={"body": "too late"})
    assert toast_of(closed)["level"] == "error" and "closed" in toast_of(closed)["message"].lower()
    page = agent.get(f"/dashboard/tickets/{tid}").text
    assert "This ticket is closed" in page and 'id="reply-body"' not in page


def test_suggested_reply_draft_edit_and_post(client, auth, agent, seeded_kb, db_session):
    from app.models import ReplyStatus, SuggestedReply
    tid = new_ticket(client, auth["alice"])
    res = hx(agent, "POST", f"/dashboard/tickets/{tid}/suggest")
    assert 'class="draft' in res.text and "Resetting your password" in res.text and "Nothing is sent until you post it" in res.text
    draft = db_session.query(SuggestedReply).one()
    assert draft.status == ReplyStatus.pending
    assert "Support Team" in draft.body
    assert client.get(f"/api/tickets/{tid}", headers=auth["alice"]).json()["comments"] == []  # nothing sent yet
    posted = hx(agent, "POST", f"/dashboard/tickets/{tid}/replies/{draft.id}/accept", data={"body": "Hi Alice, I have sent you a new link."})
    assert "I have sent you a new link" in posted.text and 'id="t-suggestions"' in posted.text and 'class="draft' not in posted.text  # thread + OOB panel
    db_session.refresh(draft)
    assert draft.status == ReplyStatus.edited
    db_session.expire_all()  # these tests share one session across requests; a real request gets a fresh one
    assert [c["body"] for c in client.get(f"/api/tickets/{tid}", headers=auth["alice"]).json()["comments"]] == ["Hi Alice, I have sent you a new link."]
    assert toast_of(hx(agent, "POST", f"/dashboard/tickets/{tid}/replies/{draft.id}/accept", data={"body": "again"}))["level"] == "error"  # already decided


def test_suggested_reply_can_be_discarded(client, auth, agent, seeded_kb, db_session):
    from app.models import ReplyStatus, SuggestedReply
    tid = new_ticket(client, auth["alice"])
    hx(agent, "POST", f"/dashboard/tickets/{tid}/suggest")
    draft = db_session.query(SuggestedReply).one()
    res = hx(agent, "POST", f"/dashboard/tickets/{tid}/replies/{draft.id}/reject")
    assert 'class="draft' not in res.text and toast_of(res)["message"] == "Draft discarded."
    db_session.refresh(draft)
    assert draft.status == ReplyStatus.rejected
    assert client.get(f"/api/tickets/{tid}", headers=auth["alice"]).json()["comments"] == []


def test_no_draft_when_the_knowledge_base_cannot_answer(client, auth, agent, seeded_kb, db_session):
    from app.models import SuggestedReply
    tid = new_ticket(client, auth["alice"], "What is the capital of France", "Also who won the cricket match")
    res = hx(agent, "POST", f"/dashboard/tickets/{tid}/suggest")
    assert "Write the reply yourself" in res.text and 'class="draft' not in res.text
    assert db_session.query(SuggestedReply).count() == 0


def test_triage_can_be_rerun_and_applied(client, auth, agent, db_session, ai_on):
    tid = new_ticket(client, auth["alice"], "App crashes on startup", "closes right after the splash screen")
    client.patch(f"/api/tickets/{tid}", headers=auth["agent"], json={"priority": "low", "category": "billing"})
    page = agent.get(f"/dashboard/tickets/{tid}").text
    assert "Use these values" in page and "Technical" in page  # the suggestion differs from what is set
    same = hx(agent, "POST", f"/dashboard/tickets/{tid}/triage")
    assert toast_of(same)["message"] == "Triage refreshed."
    t = db_session.get(Ticket, tid)
    db_session.refresh(t)
    assert (t.category, t.priority.value) == ("billing", "low")  # re-running only updates the suggestion
    applied = hx(agent, "POST", f"/dashboard/tickets/{tid}/triage?apply=1")
    db_session.refresh(t)
    assert (t.category, t.priority.value) == ("technical", "high") and "Use these values" not in applied.text


def test_merging_duplicates(client, auth, agent, db_session, ai_on):
    first = new_ticket(client, auth["alice"], "Charged twice", "I see two charges on my card this month.")
    second = new_ticket(client, auth["alice"], "Double charged for my plan", "My card was billed two times this month.")
    other = new_ticket(client, auth["bob"], "Charged two times", "Two charges appeared on my card this month.")
    page = agent.get(f"/dashboard/tickets/{second}").text
    assert f"Merge into #{first}" in page and "Another customer" in page  # only same-customer duplicates can be merged
    refused = hx(agent, "POST", f"/dashboard/tickets/{second}/merge", data={"target_id": str(other)})
    assert toast_of(refused)["level"] == "error" and "HX-Redirect" not in refused.headers
    done = hx(agent, "POST", f"/dashboard/tickets/{second}/merge", data={"target_id": str(first)})
    assert done.headers["HX-Redirect"] == f"/dashboard/tickets/{first}"
    t = db_session.get(Ticket, second)
    db_session.refresh(t)
    assert t.status == TicketStatus.closed and t.merged_into_id == first
    merged = agent.get(f"/dashboard/tickets/{second}").text
    assert f"merged into" in merged and 'id="reply-body"' not in merged and "Merge into" not in merged


# ================================================================ chats
def test_customer_chat_page_opens_one_chat_and_greets(client, customer, db_session):
    first = customer.get("/chat")
    assert first.status_code == 200 and "support assistant" in first.text and "How can we help?" in first.text
    customer.get("/chat")
    assert db_session.query(ChatSession).count() == 1  # reloading never opens a second chat
    assert 'data-bot-only' in first.text and "Start a new chat" in first.text and re.search(r"data-new-chat\s+hidden", first.text)


def test_agent_queue_claim_and_room(client, auth, agent, customer, users, db_session):
    cid = client.post("/api/chats", headers=auth["alice"]).json()["id"]
    client.post(f"/api/chats/{cid}/messages", headers=auth["alice"], json={"body": "agent"})
    lists = agent.get("/dashboard/chats/lists").text
    assert "alice" in lists and "Customer asked for a human" in lists and "Claim" in lists
    assert "claim" in agent.get(f"/dashboard/chats/{cid}").text.lower()
    claimed = hx(agent, "POST", f"/dashboard/chats/{cid}/claim")
    assert claimed.headers["HX-Redirect"] == f"/dashboard/chats/{cid}"
    room = agent.get(f"/dashboard/chats/{cid}").text
    assert f'data-chat-id="{cid}"' in room and "agent is handling this chat" in room and "Close chat" in room
    assert "Claim and reply" not in room and "disabled" not in re.search(r"<textarea[^>]*>", room).group(0)
    rival = make_user(db_session, "agent2@example.com", UserRole.agent)
    other = sign_in(rival.email)
    taken = hx(other, "POST", f"/dashboard/chats/{cid}/claim")
    assert toast_of(taken)["level"] == "error" and "chat-queue" in trigger(taken) and "HX-Redirect" not in taken.headers
    watching = other.get(f"/dashboard/chats/{cid}").text
    assert "You can read along but not reply" in watching and re.search(r"<textarea[^>]*disabled", watching)


def test_customers_cannot_open_the_agent_room_or_someone_elses_chat(client, auth, customer, users, db_session):
    cid = client.post("/api/chats", headers=auth["alice"]).json()["id"]
    assert customer.get(f"/dashboard/chats/{cid}").status_code == 403
    bob = sign_in("bob@example.com")
    assert hx(bob, "POST", f"/chat/{cid}/close").status_code == 200 and toast_of(hx(bob, "POST", f"/chat/{cid}/close"))["level"] == "error"
    assert db_session.get(ChatSession, cid).status.value == "bot"  # bob's attempts changed nothing


def test_agent_closes_a_chat_and_the_actions_panel_updates(client, auth, agent):
    cid = client.post("/api/chats", headers=auth["alice"]).json()["id"]
    client.post(f"/api/chats/{cid}/messages", headers=auth["alice"], json={"body": "agent"})
    hx(agent, "POST", f"/dashboard/chats/{cid}/claim")
    closed = hx(agent, "POST", f"/dashboard/chats/{cid}/close")
    assert toast_of(closed)["message"] == "Chat closed." and "This chat has ended" in closed.text and 'id="chat-actions"' in closed.text


def test_customer_turns_a_chat_into_a_ticket(client, auth, customer, agent, db_session, ai_on):
    cid = client.post("/api/chats", headers=auth["alice"]).json()["id"]
    client.post(f"/api/chats/{cid}/messages", headers=auth["alice"], json={"body": "My app crashes every time I open it"})
    res = hx(customer, "POST", f"/chat/{cid}/ticket")
    assert res.headers["HX-Refresh"] == "true" and "Your ticket is #" in toast_of(res)["message"]
    ticket = db_session.query(Ticket).one()
    assert ticket.subject.startswith("Chat: My app crashes") and ticket.triaged_at is not None  # triaged in the background
    assert f"Open ticket #{ticket.id}" in agent.get(f"/dashboard/chats/{cid}").text
    again = hx(customer, "POST", f"/chat/{cid}/ticket")
    assert toast_of(again)["level"] == "error" and "already converted" in toast_of(again)["message"]


# ================================================================ knowledge base
def test_kb_create_edit_delete_and_permissions(client, agent, admin, db_session):
    res = hx(agent, "POST", "/dashboard/kb", data={"title": "Gift cards", "content": "Gift cards never expire.\n\nThey cannot be exchanged for cash."})
    article_id = int(res.headers["HX-Redirect"].rsplit("/", 1)[1])
    edit = agent.get(f"/dashboard/kb/{article_id}").text
    assert "Gift cards never expire" in edit and "Piece 1" in edit and "Delete article" not in edit  # agents cannot delete
    assert hx(agent, "POST", f"/dashboard/kb/{article_id}", data={"title": "Gift cards (updated)", "content": "Updated text."}).headers["HX-Redirect"] == "/dashboard/kb"
    assert db_session.get(KBArticle, article_id).content == "Updated text."
    assert hx(agent, "POST", f"/dashboard/kb/{article_id}/delete").status_code == 403  # forged request refused
    assert "Delete article" in admin.get(f"/dashboard/kb/{article_id}").text
    assert hx(admin, "POST", f"/dashboard/kb/{article_id}/delete").headers["HX-Redirect"] == "/dashboard/kb"
    assert db_session.get(KBArticle, article_id) is None


def test_kb_validation_messages(agent):
    short = hx(agent, "POST", "/dashboard/kb", data={"title": "x", "content": "text"})
    assert toast_of(short)["level"] == "error" and "title" in toast_of(short)["message"].lower()
    assert toast_of(hx(agent, "POST", "/dashboard/kb", data={"title": "A fine title", "content": ""}))["level"] == "error"
    assert toast_of(hx(agent, "POST", "/dashboard/kb/ask", data={"question": "hi"}))["level"] == "error"
    assert agent.get("/dashboard/kb/9999").status_code == 404


def test_kb_upload(agent, db_session):
    res = hx(agent, "POST", "/dashboard/kb/upload", files={"file": ("return_policy.md", b"# Returns\n\nReturns are accepted for 30 days.", "text/markdown")})
    article = db_session.query(KBArticle).one()
    assert article.title == "return policy" and res.headers["HX-Redirect"] == f"/dashboard/kb/{article.id}"
    bad = hx(agent, "POST", "/dashboard/kb/upload", files={"file": ("scan.pdf", b"%PDF", "application/pdf")})
    assert toast_of(bad)["level"] == "error" and ".md" in toast_of(bad)["message"]


def test_kb_ask_shows_the_answer_or_a_handover(agent, seeded_kb):
    ok = hx(agent, "POST", "/dashboard/kb/ask", data={"question": "How do I reset my password?"})
    assert "Customers would see" in ok.text and "Resetting your password" in ok.text
    no = hx(agent, "POST", "/dashboard/kb/ask", data={"question": "What is the capital of France?"})
    assert "hand this to a person" in no.text and "Customers would see" not in no.text


# ================================================================ users
def test_admin_creates_edits_and_disables_accounts(client, admin, users, db_session):
    from app.models import User
    made = hx(admin, "POST", "/dashboard/users", data={"full_name": "Meera Support", "email": "Meera@Example.com", "password": "welcome-123", "role": "agent"})
    assert toast_of(made)["message"] == "Account created." and "Meera Support" in made.text
    assert 'id="new-user"' in made.text and 'hx-swap-oob="true"' in made.text  # a fresh form comes back too
    fresh_form = made.text[made.text.index('id="new-user"'):]
    assert "welcome-123" not in fresh_form and "meera@example.com" not in fresh_form.lower()  # nothing typed is left behind
    meera = db_session.query(User).filter_by(email="meera@example.com").one()
    assert meera.role == UserRole.agent
    hx(admin, "POST", f"/dashboard/users/{meera.id}", data={"role": "admin"})
    hx(admin, "POST", f"/dashboard/users/{meera.id}", data={"active": "0"})
    db_session.refresh(meera)
    assert meera.role == UserRole.admin and meera.is_active is False
    assert 'Enable' in admin.get("/dashboard/users").text
    assert TestClient(app, follow_redirects=False).post("/login", data={"email": "meera@example.com", "password": "welcome-123"}).status_code == 403


def test_user_form_errors_and_self_protection(admin, users, db_session):
    dup = hx(admin, "POST", "/dashboard/users", data={"full_name": "Dup", "email": "alice@example.com", "password": "welcome-123", "role": "agent"})
    assert toast_of(dup)["level"] == "error" and "already registered" in toast_of(dup)["message"]
    weak = hx(admin, "POST", "/dashboard/users", data={"full_name": "Weak", "email": "weak@example.com", "password": "short", "role": "agent"})
    assert toast_of(weak)["level"] == "error"
    mine = hx(admin, "POST", f"/dashboard/users/{users['admin'].id}", data={"role": "agent"})
    assert toast_of(mine)["level"] == "error" and "yourself" in toast_of(mine)["message"]
    db_session.refresh(users["admin"])
    assert users["admin"].role == UserRole.admin
    assert toast_of(hx(admin, "POST", f"/dashboard/users/{users['admin'].id}", data={"active": "0"}))["level"] == "error"
