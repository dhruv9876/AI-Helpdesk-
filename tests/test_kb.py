import io

import numpy as np
import pytest

from app.models import KBChunk
from app.services import kb
from app.services.chunking import chunk_text
from app.services.embeddings import HashingEmbedder, tokenize
from app.services.llm import NOT_FOUND, AnthropicLLM, MockLLM, build_user_prompt
from app.services.rag import answer_question
from scripts.kb_articles import ARTICLES
from scripts.eval_rag import evaluate


# ---------- pure unit tests ----------
def test_chunking_packs_paragraphs_and_respects_limit():
    text = "\n\n".join(f"Paragraph number {i}. " + "word " * 30 for i in range(10))
    chunks = chunk_text(text, max_chars=400)
    assert len(chunks) > 1
    assert all(len(c) <= 400 for c in chunks)
    assert "Paragraph number 0" in chunks[0] and "Paragraph number 9" in chunks[-1]


def test_chunking_splits_a_single_huge_paragraph():
    chunks = chunk_text("This is a sentence. " * 200, max_chars=300)
    assert len(chunks) > 5 and all(len(c) <= 300 for c in chunks)


def test_chunking_empty_text():
    assert chunk_text("   \n\n  ") == []


def test_tokenizer_drops_stopwords_and_stems():
    assert tokenize("The charges were Charged") == ["charg", "charg"]
    assert tokenize("please help me") == []


def test_embedder_is_deterministic_and_normalised():
    e = HashingEmbedder()
    a, b = e.embed(["reset my password"]), e.embed(["reset my password"])
    assert np.allclose(a, b)
    assert abs(float(np.linalg.norm(a[0])) - 1.0) < 1e-5
    assert float(np.linalg.norm(e.embed(["the and of"])[0])) == 0.0  # only stop words


def test_embedder_similarity_ordering():
    e = HashingEmbedder()
    q, close, far = e.embed(["password reset email"])[0], e.embed(["reset your password"])[0], e.embed(["tracking a parcel"])[0]
    assert q @ close > q @ far


# ---------- LLM layer ----------
def test_mock_llm_extracts_relevant_sentences_or_not_found():
    passages = ["Refunds take five to seven business days. Contact us for help.", "Parcels ship in three days."]
    assert "five to seven" in MockLLM().answer("how long do refunds take", passages)
    assert MockLLM().answer("capital of france", passages) == NOT_FOUND
    # one shared word out of two is not enough ("order" would match any shipping text)
    assert MockLLM().answer("order pizza", ["Your order ships in three days."]) == NOT_FOUND


def test_anthropic_llm_builds_grounded_request():
    class Block: type, text = "text", "Resets take 5 minutes [1]."
    class Resp: content = [Block()]
    class Messages:
        def create(self, **kw):
            self.kw = kw
            return Resp()
    class FakeClient: messages = Messages()

    llm = AnthropicLLM("key", "claude-sonnet-5-5", client=FakeClient())
    out = llm.answer("how long?", ["Resets take 5 minutes."])
    kw = FakeClient.messages.kw
    assert out == "Resets take 5 minutes [1]."
    assert kw["model"] == "claude-sonnet-5-5" and kw["max_tokens"] > 0
    assert NOT_FOUND in kw["system"] and "untrusted" in kw["system"]
    assert kw["messages"][0]["content"] == build_user_prompt("how long?", ["Resets take 5 minutes."])


# ---------- service tests with a database ----------
@pytest.fixture()
def seeded(db_session):
    for title, content in ARTICLES:
        kb.create_article(db_session, title, content, "seed", None)
    return db_session


def test_search_finds_the_right_article(seeded):
    hits = kb.search(seeded, "I never received the password reset email")
    assert hits[0].title == "Resetting your password"
    assert hits == sorted(hits, key=lambda h: h.score, reverse=True)


def test_search_ignores_vectors_from_other_embedders(seeded):
    for chunk in seeded.query(KBChunk).all():
        chunk.embedder = "some-other-model"
    seeded.commit()
    assert kb.search(seeded, "password reset") == []
    assert kb.reindex_all(seeded) == len(ARTICLES)
    assert kb.search(seeded, "password reset")


def test_rag_answers_known_question(seeded):
    result = answer_question(seeded, "How long does a refund take to arrive?")
    assert not result.escalate and result.answer
    assert "business days" in result.answer
    assert result.sources[0].title == "Requesting a refund"


def test_rag_escalates_off_topic_without_calling_llm(seeded):
    class Boom:
        name = "boom"
        def answer(self, *a): raise AssertionError("LLM must not be called for weak retrieval")
    result = answer_question(seeded, "what is the capital of France", llm=Boom())
    assert result.escalate and result.answer is None


def test_rag_escalates_when_llm_says_not_found(seeded):
    class Refuses:
        name = "refuses"
        def answer(self, *a): return NOT_FOUND
    result = answer_question(seeded, "how do I reset my password", llm=Refuses())
    assert result.escalate and result.answer is None


def test_rag_with_empty_kb_escalates(db_session):
    assert answer_question(db_session, "anything at all").escalate


def test_retrieval_quality_threshold(seeded):
    """Regression guard: fails if a change to chunking/embeddings makes search noticeably worse."""
    r = evaluate(seeded)
    assert r["top3"] >= 0.9 and r["top1"] >= 0.8
    assert r["answer_rate"] >= 0.8  # don't escalate questions we can answer
    assert r["off_topic_escalated"] == r["off_topic_total"], r["wrongly_answered"]


# ---------- API tests ----------
def article(client, headers, title="Library opening hours", content="The library opens at 9 AM and closes at 5 PM."):
    return client.post("/api/kb/articles", headers=headers, json={"title": title, "content": content})


def test_staff_creates_article_customer_cannot(client, auth):
    assert article(client, auth["alice"]).status_code == 403
    res = article(client, auth["agent"])
    assert res.status_code == 201
    body = res.json()
    assert body["chunk_count"] == 1 and body["source"] == "manual"
    assert article(client, {}).status_code == 401


def test_customers_can_read_and_search(client, auth):
    art = article(client, auth["agent"]).json()
    assert [a["id"] for a in client.get("/api/kb/articles", headers=auth["alice"]).json()] == [art["id"]]
    assert client.get(f"/api/kb/articles/{art['id']}", headers=auth["alice"]).json()["content"].startswith("The library")
    hits = client.get("/api/kb/search?q=library hours", headers=auth["alice"]).json()
    assert hits and hits[0]["article_id"] == art["id"]
    assert client.get("/api/kb/articles/999", headers=auth["alice"]).status_code == 404


def test_update_rechunks_and_changes_search_results(client, auth):
    art = article(client, auth["agent"], "Parking", "Parking is free on weekends.").json()
    assert client.get("/api/kb/search?q=parking weekend", headers=auth["alice"]).json()
    res = client.put(f"/api/kb/articles/{art['id']}", headers=auth["agent"],
                     json={"content": "Bicycle storage is available in the basement."})
    assert res.status_code == 200
    hits = client.get("/api/kb/search?q=parking weekend", headers=auth["alice"]).json()
    assert all("Bicycle" in h["text"] or h["score"] < 0.3 for h in hits)
    assert client.put(f"/api/kb/articles/{art['id']}", headers=auth["agent"], json={"title": None}).status_code == 422
    assert client.put(f"/api/kb/articles/{art['id']}", headers=auth["alice"], json={"title": "Hax hax"}).status_code == 403


def test_delete_is_admin_only_and_removes_chunks(client, auth, db_session):
    art = article(client, auth["agent"]).json()
    assert client.delete(f"/api/kb/articles/{art['id']}", headers=auth["agent"]).status_code == 403
    assert client.delete(f"/api/kb/articles/{art['id']}", headers=auth["admin"]).status_code == 204
    assert db_session.query(KBChunk).count() == 0
    assert client.get(f"/api/kb/articles/{art['id']}", headers=auth["admin"]).status_code == 404


def test_upload_markdown_file(client, auth):
    files = {"file": ("refund_policy.md", io.BytesIO(b"# Refunds\n\nRefunds take 5 days."), "text/markdown")}
    res = client.post("/api/kb/upload", headers=auth["agent"], files=files)
    assert res.status_code == 201
    assert res.json()["title"] == "refund policy" and res.json()["source"] == "upload:refund_policy.md"


def test_upload_validation(client, auth):
    def up(name, data, **kw):
        return client.post("/api/kb/upload", headers=auth["agent"], files={"file": (name, io.BytesIO(data))}, **kw)
    assert up("doc.pdf", b"%PDF").status_code == 415
    assert up("empty.txt", b"   ").status_code == 422
    assert up("bin.txt", b"\xff\xfe\x00bad").status_code == 422
    assert up("big.txt", b"a" * 1_000_001).status_code == 413
    assert client.post("/api/kb/upload", headers=auth["alice"],
                       files={"file": ("a.txt", io.BytesIO(b"hello there"))}).status_code == 403


def test_ask_endpoint_answers_with_sources(client, auth):
    for title, content in ARTICLES:
        client.post("/api/kb/articles", headers=auth["agent"], json={"title": title, "content": content})
    res = client.post("/api/kb/ask", headers=auth["alice"], json={"question": "I forgot my password, how do I reset it?"})
    assert res.status_code == 200
    body = res.json()
    assert body["escalate"] is False and body["answer"]
    assert body["sources"][0]["title"] == "Resetting your password"
    assert body["llm"] == "mock-extractive" and body["confidence"] > 0.12


def test_ask_endpoint_escalates_unknown_question(client, auth):
    for title, content in ARTICLES[:3]:
        client.post("/api/kb/articles", headers=auth["agent"], json={"title": title, "content": content})
    body = client.post("/api/kb/ask", headers=auth["alice"], json={"question": "Who won the football match yesterday?"}).json()
    assert body["escalate"] is True and body["answer"] is None


def test_ask_requires_auth_and_valid_input(client, auth):
    assert client.post("/api/kb/ask", json={"question": "hello there"}).status_code == 401
    assert client.post("/api/kb/ask", headers=auth["alice"], json={"question": "hi"}).status_code == 422


def test_reindex_admin_only(client, auth):
    article(client, auth["agent"])
    assert client.post("/api/kb/reindex", headers=auth["agent"]).status_code == 403
    assert client.post("/api/kb/reindex", headers=auth["admin"]).json() == {"reindexed_articles": 1}
