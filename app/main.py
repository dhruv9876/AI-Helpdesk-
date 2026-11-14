from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import auth, chats, kb, ticket_ai, tickets, users
from app.core.config import settings
from app.core.database import Base, engine
from app.web import install as install_dashboard


@asynccontextmanager
async def lifespan(_: FastAPI):
    # For a college project create_all is enough; use Alembic if the schema starts changing.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title=settings.app_name, version="0.5.0", lifespan=lifespan)

app.include_router(auth.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(tickets.router, prefix="/api")
app.include_router(ticket_ai.router, prefix="/api")
app.include_router(kb.router, prefix="/api")
app.include_router(chats.router, prefix="/api")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


install_dashboard(app)
