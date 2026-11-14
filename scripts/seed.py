"""Fill the database with dummy users and tickets for demos.

Run from the project root:  python -m scripts.seed
Safe to re-run: it does nothing if users already exist.
"""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import Base, SessionLocal, engine
from app.core.security import hash_password
from app.models import Comment, KBArticle, Ticket, TicketStatus, User, UserRole
from app.services import chat as chat_service
from app.services import kb
from app.services.ai_pipeline import run_ai_pipeline
from scripts.kb_articles import ARTICLES

PASSWORD = "password123"

USERS = [
    ("admin@example.com", "Priya Admin", UserRole.admin),
    ("agent1@example.com", "Rahul Agent", UserRole.agent),
    ("agent2@example.com", "Sneha Agent", UserRole.agent),
    ("customer1@example.com", "Amit Customer", UserRole.customer),
    ("customer2@example.com", "Neha Customer", UserRole.customer),
    ("customer3@example.com", "Karan Customer", UserRole.customer),
]

# (subject, description)
TICKETS = [
    ("Cannot reset my password", "The reset email never arrives, I checked spam too."),
    ("Charged twice for my subscription", "I see two charges of the same amount on my card this month."),
    ("Where is my order?", "Order placed 8 days ago and tracking has not updated."),
    ("App crashes on startup", "Since the last update the app closes right after the splash screen."),
    ("How do I change my email address?", "I want to move my account to my new email."),
    ("Refund not received", "My refund was approved two weeks ago but nothing has arrived."),
    ("Cannot upload profile photo", "Upload spins forever for any image over 1 MB."),
    ("Invoice needs company GST number", "Please re-issue invoice #4821 with our GST details."),
    ("Wrong item delivered", "I ordered a blue jacket and received a black one."),
    ("Two-factor codes not working", "The 6 digit code is always rejected as invalid."),
    ("Need to cancel my plan", "Please cancel at the end of the billing period."),
    ("Delivery address change", "Can I change the address on an order that has not shipped?"),
]


# (customer email, subject, description): extra tickets that show off the AI features.
SHOWCASE = [
    ("customer1@example.com", "URGENT: someone hacked my account",
     "I think my account was hacked, my password was changed and I cannot log in. Please help immediately."),
    ("customer2@example.com", "Refund still not received!!",
     "I was promised a refund two weeks ago and nobody has replied. This is unacceptable."),
    ("customer3@example.com", "Charged twice on my card",
     "I see two charges for the same subscription on my card this month."),
    ("customer3@example.com", "Double charged for my plan",
     "My card was billed two times this month for the same plan. Please fix this."),
]


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if not db.scalar(select(KBArticle).limit(1)):
            for title, content in ARTICLES:
                kb.create_article(db, title, content, "seed", None)
            print(f"Seeded {len(ARTICLES)} knowledge-base articles.")

        if db.scalar(select(User).limit(1)):
            print("Database already has users, skipping user/ticket seed.")
            return

        rng = random.Random(42)
        users = {}
        for email, name, role in USERS:
            u = User(email=email, full_name=name, role=role, hashed_password=hash_password(PASSWORD))
            db.add(u)
            users[email] = u
        db.commit()

        customers = [u for u in users.values() if u.role == UserRole.customer]
        agents = [u for u in users.values() if u.role == UserRole.agent]
        now = datetime.now(timezone.utc)

        for i, (subject, description) in enumerate(TICKETS):
            created = now - timedelta(days=rng.randint(0, 14), hours=rng.randint(0, 23))
            status = rng.choice(list(TicketStatus))
            t = Ticket(
                subject=subject,
                description=description,
                customer_id=customers[i % len(customers)].id,
                status=status,
                assignee_id=rng.choice(agents).id if rng.random() < 0.6 else None,
                created_at=created,
                updated_at=created,
                resolved_at=created + timedelta(hours=6)
                if status in (TicketStatus.resolved, TicketStatus.closed)
                else None,
            )
            db.add(t)
            db.flush()
            if t.assignee_id:
                db.add(Comment(ticket_id=t.id, author_id=t.assignee_id,
                               body="Thanks for reaching out, we're looking into this.",
                               created_at=created + timedelta(minutes=30)))
        by_email = {u.email: u for u in users.values()}
        for email, subject, description in SHOWCASE:
            created = now - timedelta(hours=rng.randint(1, 30))
            db.add(Ticket(subject=subject, description=description, customer_id=by_email[email].id,
                          created_at=created, updated_at=created))
            db.flush()
        db.commit()

        # Give every ticket its AI triage, embedding and duplicate check (oldest first).
        ids = [t.id for t in db.scalars(select(Ticket).order_by(Ticket.created_at, Ticket.id))]
        for ticket_id in ids:
            run_ai_pipeline(ticket_id, SessionLocal)

        # Two chats so the Chats page has something to show: one the bot finished, one waiting for an agent.
        one, two = by_email["customer1@example.com"], by_email["customer2@example.com"]
        done, _, _ = chat_service.open_chat(db, one.id)
        chat_service.send_message(db, done.id, one.id, "How do I reset my password?")
        chat_service.close_chat(db, done.id, one.id)
        waiting, _, _ = chat_service.open_chat(db, two.id)
        chat_service.send_message(db, waiting.id, two.id, "I was charged twice and I want to talk to a person about it")

        print(f"Seeded {len(USERS)} users, {len(ids)} tickets (all AI-triaged) and 2 chats. Password for all: {PASSWORD}")
        print("Start the server (uvicorn app.main:app --reload), open http://localhost:8000 and sign in as:")
        print("  admin@example.com / agent1@example.com (dashboard)   customer1@example.com (customer chat)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
