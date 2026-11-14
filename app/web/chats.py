"""Live chat pages: the agent's queue and room, and the customer's chat page.

The pages render the history on the server (so they work before any script runs) and then
chat.js takes over with the WebSocket protocol from app/api/chats.py. Claiming, closing and
converting to a ticket call the same functions the REST API uses.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api import chats as chats_api
from app.core.deps import DB, SessionFactory, is_staff
from app.models import ChatSession, ChatStatus, Ticket
from app.services import chat as chat_service
from app.services.chat import ChatError
from app.web.deps import WebCustomer, WebStaff, WebUser, csrf_guard, page, redirect, render, toast, toast_header
from fastapi import Response

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_guard)])


def _kind(message, my_id: int) -> str:
    """How a message is drawn: mine (right), the bot, the system, or the other person."""
    if message.sender_type.value in ("system", "bot"):
        return message.sender_type.value
    return "mine" if message.sender_id == my_id else "other"


def _room_context(db, user, chat_id: int) -> dict:
    try:
        chat, messages = chat_service.history(db, chat_id, user.id)
    except ChatError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return {
        "chat": chat,
        "messages": [{"m": m, "kind": _kind(m, user.id)} for m in messages],
        "can_send": chat.status == ChatStatus.bot and not is_staff(user)
                    or chat.status == ChatStatus.waiting and not is_staff(user)
                    or chat.status == ChatStatus.active and (chat.agent_id == user.id),
    }


# =======================================================================================
# Agent side
# =======================================================================================
def _lists(db, user) -> dict:
    waiting = chat_service.list_chats(db, user.id, ChatStatus.waiting, limit=20)
    active = chat_service.list_chats(db, user.id, ChatStatus.active, limit=30)
    bot = chat_service.list_chats(db, user.id, ChatStatus.bot, limit=10)
    closed = chat_service.list_chats(db, user.id, ChatStatus.closed, limit=8)
    return {"waiting": waiting, "mine": [c for c in active if c.agent_id == user.id],
            "others": [c for c in active if c.agent_id != user.id], "bot": bot, "closed": closed}


@router.get("/dashboard/chats")
def chats_page(request: Request, user: WebStaff, db: DB):
    return page(request, "chats.html", _lists(db, user), user, db)


@router.get("/dashboard/chats/lists")
def chats_lists(request: Request, user: WebStaff, db: DB):
    return render(request, "partials/_chat_lists.html", _lists(db, user), user=user)


@router.get("/dashboard/chats/{chat_id}")
def chat_room(chat_id: int, request: Request, user: WebStaff, db: DB):
    ctx = _room_context(db, user, chat_id)
    chat = ctx["chat"]
    tickets = db.scalars(select(Ticket).where(Ticket.customer_id == chat.customer_id).order_by(Ticket.created_at.desc()).limit(5)).all()
    return page(request, "chat_room.html", {**ctx, "customer_tickets": tickets, "role": "agent"}, user, db)


@router.get("/dashboard/chats/{chat_id}/actions")
def chat_actions(chat_id: int, request: Request, user: WebStaff, db: DB):
    return render(request, "partials/_chat_actions.html", _room_context(db, user, chat_id), user=user)


@router.post("/dashboard/chats/{chat_id}/claim")
async def claim(chat_id: int, user: WebStaff, db: DB):
    try:
        await chats_api.claim_chat(chat_id, user, db)
    except HTTPException as exc:
        return toast(str(exc.detail), "error", also={"chat-queue": {}})
    return redirect(f"/dashboard/chats/{chat_id}")


@router.post("/dashboard/chats/{chat_id}/close")
async def close(chat_id: int, request: Request, user: WebStaff, db: DB):
    try:
        await chats_api.close_chat(chat_id, user, db)
    except HTTPException as exc:
        return toast(str(exc.detail), "error")
    return render(request, "partials/_chat_actions.html", _room_context(db, user, chat_id), user=user,
                  headers=toast_header("Chat closed."))


@router.post("/dashboard/chats/{chat_id}/ticket")
async def to_ticket(chat_id: int, user: WebStaff, db: DB, background: BackgroundTasks, session_factory: SessionFactory):
    try:
        ticket = await chats_api.chat_to_ticket(chat_id, user, db, background, session_factory)
    except HTTPException as exc:
        return toast(str(exc.detail), "error")
    response = redirect(f"/dashboard/tickets/{ticket.id}")
    response.background = background  # run the AI triage after the response, like the API does
    return response


# =======================================================================================
# Customer side
# =======================================================================================
@router.get("/chat")
def customer_chat(request: Request, user: WebUser, db: DB):
    if is_staff(user):
        return RedirectResponse("/dashboard/chats", status_code=303)
    chat, _, outcome = chat_service.open_chat(db, user.id)
    ctx = _room_context(db, user, chat.id)
    return page(request, "chat_customer.html", {**ctx, "role": "customer"}, user, db)


@router.post("/chat/{chat_id}/close")
async def customer_close(chat_id: int, user: WebCustomer, db: DB):
    try:
        await chats_api.close_chat(chat_id, user, db)
    except HTTPException as exc:
        return toast(str(exc.detail), "error")
    return toast("Chat ended.", "success")


@router.post("/chat/{chat_id}/ticket")
async def customer_ticket(chat_id: int, user: WebCustomer, db: DB, background: BackgroundTasks, session_factory: SessionFactory):
    try:
        ticket = await chats_api.chat_to_ticket(chat_id, user, db, background, session_factory)
    except HTTPException as exc:
        return toast(str(exc.detail), "error")
    response = Response(status_code=200, headers={**toast_header(f"Your ticket is #{ticket.id}. We'll reply there."), "HX-Refresh": "true"})
    response.background = background
    return response
