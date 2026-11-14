def new_ticket(client, headers, subject="Cannot log in", description="Password reset email never arrives"):
    res = client.post("/api/tickets", headers=headers, json={"subject": subject, "description": description})
    assert res.status_code == 201, res.text
    return res.json()


def test_create_ticket_defaults(client, auth, users):
    t = new_ticket(client, auth["alice"])
    assert t["status"] == "open"
    assert t["priority"] == "medium"
    assert t["customer_id"] == users["alice"].id
    assert t["assignee_id"] is None


def test_create_requires_auth_and_valid_body(client, auth):
    assert client.post("/api/tickets", json={"subject": "abc", "description": "x"}).status_code == 401
    res = client.post("/api/tickets", headers=auth["alice"], json={"subject": "a", "description": ""})
    assert res.status_code == 422


def test_customer_only_sees_own_tickets(client, auth):
    mine = new_ticket(client, auth["alice"], "Alice issue")
    theirs = new_ticket(client, auth["bob"], "Bob issue")

    listing = client.get("/api/tickets", headers=auth["alice"]).json()
    assert [t["id"] for t in listing["items"]] == [mine["id"]]

    assert client.get(f"/api/tickets/{mine['id']}", headers=auth["alice"]).status_code == 200
    # 404, not 403, so ids can't be probed
    assert client.get(f"/api/tickets/{theirs['id']}", headers=auth["alice"]).status_code == 404
    assert client.patch(f"/api/tickets/{theirs['id']}", headers=auth["alice"], json={"subject": "hax"}).status_code == 404


def test_staff_sees_all_tickets(client, auth):
    new_ticket(client, auth["alice"])
    new_ticket(client, auth["bob"])
    assert client.get("/api/tickets", headers=auth["agent"]).json()["total"] == 2


def test_filters_search_and_pagination(client, auth, users):
    for i in range(5):
        new_ticket(client, auth["alice"], f"Billing question {i}", "invoice")
    shipping = new_ticket(client, auth["bob"], "Where is my parcel", "shipping delay")
    client.patch(f"/api/tickets/{shipping['id']}", headers=auth["agent"], json={"priority": "urgent", "category": "shipping"})

    def get(qs):
        return client.get(f"/api/tickets?{qs}", headers=auth["agent"]).json()

    assert get("priority=urgent")["total"] == 1
    assert get("category=shipping")["items"][0]["id"] == shipping["id"]
    assert get("q=invoice")["total"] == 5
    assert get("unassigned=true")["total"] == 6
    page = get("limit=2&offset=2")
    assert page["total"] == 6 and len(page["items"]) == 2
    assert client.get("/api/tickets?limit=1000", headers=auth["agent"]).status_code == 422


def test_staff_can_triage_and_assign(client, auth, users):
    t = new_ticket(client, auth["alice"])
    res = client.patch(
        f"/api/tickets/{t['id']}",
        headers=auth["agent"],
        json={"priority": "high", "category": "account", "assignee_id": users["agent"].id, "status": "pending"},
    )
    assert res.status_code == 200
    body = res.json()
    assert (body["priority"], body["category"], body["status"]) == ("high", "account", "pending")
    assert body["assignee_id"] == users["agent"].id

    # explicit null unassigns
    res = client.patch(f"/api/tickets/{t['id']}", headers=auth["agent"], json={"assignee_id": None})
    assert res.json()["assignee_id"] is None


def test_cannot_assign_to_customer_or_missing_user(client, auth, users):
    t = new_ticket(client, auth["alice"])
    for bad in (users["bob"].id, 9999):
        res = client.patch(f"/api/tickets/{t['id']}", headers=auth["agent"], json={"assignee_id": bad})
        assert res.status_code == 422


def test_null_on_required_field_rejected(client, auth):
    t = new_ticket(client, auth["alice"])
    res = client.patch(f"/api/tickets/{t['id']}", headers=auth["agent"], json={"status": None})
    assert res.status_code == 422


def test_customer_update_rules(client, auth):
    t = new_ticket(client, auth["alice"])
    url = f"/api/tickets/{t['id']}"
    # can edit content while open
    ok = client.patch(url, headers=auth["alice"], json={"subject": "Cannot log in (updated)"})
    assert ok.status_code == 200 and ok.json()["subject"].endswith("(updated)")
    # cannot touch staff fields
    assert client.patch(url, headers=auth["alice"], json={"priority": "urgent"}).status_code == 403
    assert client.patch(url, headers=auth["alice"], json={"assignee_id": 1}).status_code == 403
    # cannot reopen / resolve, only close
    assert client.patch(url, headers=auth["alice"], json={"status": "resolved"}).status_code == 403
    closed = client.patch(url, headers=auth["alice"], json={"status": "closed"})
    assert closed.status_code == 200 and closed.json()["resolved_at"] is not None
    # closed tickets are no longer editable
    assert client.patch(url, headers=auth["alice"], json={"subject": "too late"}).status_code == 409


def test_resolved_at_tracking(client, auth):
    t = new_ticket(client, auth["alice"])
    url = f"/api/tickets/{t['id']}"
    resolved = client.patch(url, headers=auth["agent"], json={"status": "resolved"}).json()
    assert resolved["resolved_at"] is not None
    reopened = client.patch(url, headers=auth["agent"], json={"status": "open"}).json()
    assert reopened["resolved_at"] is None


def test_comments_and_internal_notes(client, auth):
    t = new_ticket(client, auth["alice"])
    base = f"/api/tickets/{t['id']}"
    assert client.post(f"{base}/comments", headers=auth["alice"], json={"body": "Any update?"}).status_code == 201
    assert client.post(f"{base}/comments", headers=auth["agent"], json={"body": "Looking into it"}).status_code == 201
    note = client.post(
        f"{base}/comments", headers=auth["agent"], json={"body": "Customer is on the old plan", "is_internal": True}
    )
    assert note.status_code == 201

    # customers cannot write internal notes
    res = client.post(f"{base}/comments", headers=auth["alice"], json={"body": "sneaky", "is_internal": True})
    assert res.status_code == 403

    customer_view = client.get(base, headers=auth["alice"]).json()
    staff_view = client.get(base, headers=auth["agent"]).json()
    assert len(customer_view["comments"]) == 2
    assert all(not c["is_internal"] for c in customer_view["comments"])
    assert len(staff_view["comments"]) == 3


def test_comment_access_and_closed_tickets(client, auth):
    t = new_ticket(client, auth["alice"])
    base = f"/api/tickets/{t['id']}"
    assert client.post(f"{base}/comments", headers=auth["bob"], json={"body": "hi"}).status_code == 404
    client.patch(base, headers=auth["agent"], json={"status": "closed"})
    assert client.post(f"{base}/comments", headers=auth["alice"], json={"body": "hi"}).status_code == 409


def test_only_admin_can_delete(client, auth):
    t = new_ticket(client, auth["alice"])
    client.post(f"/api/tickets/{t['id']}/comments", headers=auth["alice"], json={"body": "note"})
    url = f"/api/tickets/{t['id']}"
    assert client.delete(url, headers=auth["alice"]).status_code == 403
    assert client.delete(url, headers=auth["agent"]).status_code == 403
    assert client.delete(url, headers=auth["admin"]).status_code == 204
    assert client.get(url, headers=auth["admin"]).status_code == 404
    assert client.delete(url, headers=auth["admin"]).status_code == 404
