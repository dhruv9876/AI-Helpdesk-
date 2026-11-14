"""Measure retrieval quality on a small labelled question set.

Run:  python -m scripts.eval_rag
Each question has the title of the article that should answer it. We report:
  * retrieval: how often the right article is ranked #1 (top-1) / in the top 3 (top-3)
  * answer rate: in-domain questions the bot answers instead of escalating
  * escalation rate: off-topic questions the bot correctly hands to a human
The full pipeline is tested with the offline mock LLM so results are reproducible.
"""
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

from app.core.config import settings
from app.core.database import Base
from app.services import kb
from app.services.llm import MockLLM
from app.services.rag import answer_question
from scripts.kb_articles import ARTICLES

# (question, expected article title)
QUESTIONS = [
    ("I never got the password reset email", "Resetting your password"),
    ("forgot my password how do I log in again", "Resetting your password"),
    ("I was charged twice this month", "Duplicate or incorrect charges"),
    ("how long does a refund take to arrive", "Requesting a refund"),
    ("my refund was approved but money has not arrived", "Requesting a refund"),
    ("where is my parcel, tracking has not moved", "Tracking your order"),
    ("the app closes immediately after opening", "App crashes or will not open"),
    ("how do I change my account email", "Changing your email address"),
    ("authenticator code says invalid", "Two-factor authentication problems"),
    ("I lost my phone and cannot get the 2FA code", "Two-factor authentication problems"),
    ("I want to cancel my plan", "Cancelling your subscription"),
    ("need an invoice with our GST number", "Downloading invoices and adding tax or GST details"),
    ("I received the wrong jacket", "Wrong or damaged item received"),
    ("can I ship my order to a different address", "Changing your delivery address"),
    ("photo upload keeps failing", "Uploading photos and files"),
    ("how do I delete my account", "Deleting your account and data privacy"),
    ("how fast will support reply", "Contacting support and response times"),
]

OFF_TOPIC = [
    "what is the capital of France",
    "write me a poem about the ocean",
    "who won the cricket world cup",
    "how do I order pizza",
    "what is the weather in Delhi today",
    "best phone to buy",
]


def build_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    for title, content in ARTICLES:
        kb.create_article(db, title, content, "seed", None)
    return db


def evaluate(db=None) -> dict:
    db = db or build_db()
    top1 = top3 = 0
    misses = []
    for question, expected in QUESTIONS:
        titles = []
        for h in kb.search(db, question, k=10):
            if h.title not in titles:
                titles.append(h.title)
        top1 += titles[:1] == [expected]
        top3 += expected in titles[:3]
        if titles[:1] != [expected]:
            misses.append((question, expected, titles[:3]))
    llm = MockLLM()
    answered = sum(not answer_question(db, q, llm).escalate for q, _ in QUESTIONS)
    wrongly_answered = [q for q in OFF_TOPIC if not answer_question(db, q, llm).escalate]
    return {
        "answer_rate": answered / len(QUESTIONS),
        "wrongly_answered": wrongly_answered,
        "n": len(QUESTIONS),
        "top1": top1 / len(QUESTIONS),
        "top3": top3 / len(QUESTIONS),
        "misses": misses,
        "off_topic_escalated": len(OFF_TOPIC) - len(wrongly_answered),
        "off_topic_total": len(OFF_TOPIC),
    }


if __name__ == "__main__":
    r = evaluate()
    print(f"Retrieval  ({r['n']} questions): top-1 {r['top1']:.0%}   top-3 {r['top3']:.0%}")
    for q, expected, got in r["misses"]:
        print(f"  top-1 miss: {q!r}\n    wanted {expected!r}\n    got    {got}")
    print(f"Answer rate (in-domain, not escalated): {r['answer_rate']:.0%}")
    print(f"Off-topic correctly escalated: {r['off_topic_escalated']}/{r['off_topic_total']}   "
          f"(threshold {settings.kb_min_score})")
    for q in r["wrongly_answered"]:
        print(f"  WRONGLY ANSWERED: {q!r}")
