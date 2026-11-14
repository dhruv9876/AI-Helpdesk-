"""Live chat: REST endpoints plus WebSockets.

WebSocket protocol (JSON text frames)
-------------------------------------
Connect to  /api/chats/{id}/ws   (a chat)   or   /api/chats/queue/ws   (staff: the waiting queue).
Browsers cannot set an Authorization header on a WebSocket, and tokens in the URL end up in logs,
so authentication is the FIRST message, within CHAT_AUTH_TIMEOUT_SECONDS:

    client -> {"type": "auth", "token": "<JWT from /api/auth/login>"}
    server -> {"type": "ready", "chat": {...}, "messages": [...]}          (chat socket)
    server -> {"type": "queue", "chats": [...]}                            (queue socket)

Then:
    client -> {"type": "message", "body": "hello"}
    client -> {"type": "ping"}                      server -> {"type": "pong"}
    server -> {"type": "message", "message": {...}}   a message in the chat (bot, agent, system, you)
    server -> {"type": "status", "status": "waiting|active|closed", "agent": {...}|null}
    server -> {"type": "error", "code": 429, "detail": "..."}                (socket stays open)
    queue socket: {"type": "chat_waiting"|"chat_claimed"|"chat_closed", ...}

Close codes: 4401 not authenticated / timed out, 4403 forbidden, 4404 chat not found.
"""
import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool

from app.api.helpers import ticket_out
from app.core.config import settings
from app.core.deps import DB, CurrentUser, SessionFactory, StaffUser, is_staff
from app.core.security import decode_access_token
from app.models import ChatStatus, User
from app.schemas import ChatDetail, ChatMessageOut, ChatOut, SendMessageIn, TicketOut
from app.services import chat as chat_service
from app.services.chat import ChatError, Outcome
from app.services.chat_hub import Conn, hub
from app.services.ai_pipeline import run_ai_pipeline
from app.services.llm import LLM, get_llm

router = APIRouter(prefix="/chats", tags=["chat"])
LLMDep = Annotated[LLM, Depends(get_llm)]


async def _run(fn, *args):
    """Run blocking service code off the event loop and translate rule violations to HTTP."""
    try:
        return await run_in_threadpool(fn, *args)
    except ChatError as exc:
        raise HTTPException(exc.status_code, exc.detail)


def _in_session(factory, fn, *args):
    with factory() as db:
        return fn(db, *args)


# =======================================================================================
# REST
# =======================================================================================
@router.post("", response_model=ChatOut, status_code=status.HTTP_201_CREATED)
async def open_chat(user: CurrentUser, db: DB, response: Response):
    """Start a chat (or get your existing open one: 200 instead of 201)."""
    chat, created, outcome = await _run(chat_service.open_chat, db, user.id)
    if not created:
        response.status_code = status.HTTP_200_OK
    await hub.publish(chat.id, outcome)
    return chat_service.chat_out(chat, include_reason=False)


@router.get("", response_model=list[ChatOut])
async def list_chats(
    user: CurrentUser,
    db: DB,
    status_: ChatStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
):
    """Customers see their own chats; staff see all (use ?status=waiting for the queue)."""
    return await _run(chat_service.list_chats, db, user.id, status_, limit)


@router.get("/{chat_id}", response_model=ChatDetail)
async def get_chat(chat_id: int, user: CurrentUser, db: DB):
    data = await _run(chat_service.snapshot, db, chat_id, user.id)
    return ChatDetail(**data["chat"], messages=data["messages"])


@router.post("/{chat_id}/messages", response_model=list[ChatMessageOut], status_code=status.HTTP_201_CREATED)
async def send_message(chat_id: int, payload: SendMessageIn, user: CurrentUser, db: DB, llm: LLMDep):
    """HTTP fallback for sending a message. Returns everything created (your message, plus the
    bot's reply or hand-off notice). Connected WebSocket clients are notified too."""
    outcome = await _run(chat_service.send_message, db, chat_id, user.id, payload.body, llm, hub.agents_online())
    await hub.publish(chat_id, outcome)
    return outcome.messages


@router.post("/{chat_id}/claim", response_model=ChatOut)
async def claim_chat(chat_id: int, user: StaffUser, db: DB):
    """Staff: take a waiting chat (or take over from the bot). First to claim wins."""
    outcome = await _run(chat_service.claim_chat, db, chat_id, user.id)
    await hub.publish(chat_id, outcome)
    data = await _run(chat_service.snapshot, db, chat_id, user.id)
    return data["chat"]


@router.post("/{chat_id}/close", response_model=ChatOut)
async def close_chat(chat_id: int, user: CurrentUser, db: DB):
    outcome = await _run(chat_service.close_chat, db, chat_id, user.id)
    await hub.publish(chat_id, outcome)
    data = await _run(chat_service.snapshot, db, chat_id, user.id)
    return data["chat"]


@router.post("/{chat_id}/ticket", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
async def chat_to_ticket(
    chat_id: int, user: CurrentUser, db: DB, background: BackgroundTasks, session_factory: SessionFactory
):
    """Turn the chat into a ticket and close it. The new ticket goes through AI triage like any other."""
    ticket, outcome = await _run(chat_service.convert_to_ticket, db, chat_id, user.id)
    await hub.publish(chat_id, outcome)
    if settings.ai_auto_triage:
        background.add_task(run_ai_pipeline, ticket.id, session_factory)
    return ticket_out(ticket, user)


# =======================================================================================
# WebSockets
# =======================================================================================
async def _receive_frame(websocket: WebSocket) -> dict | None:
    """Read one JSON object frame. Returns None for garbage (invalid JSON, binary, not an object).
    Raises WebSocketDisconnect if the client has gone."""
    try:
        frame = json.loads(await websocket.receive_text())
    except (ValueError, KeyError):  # invalid JSON, or a binary frame (no "text" key)
        return None
    return frame if isinstance(frame, dict) else None


async def _authenticate(websocket: WebSocket, factory) -> User | None:
    """Wait for the {"type": "auth", "token": ...} frame. Closes the socket and returns None on failure."""
    try:
        frame = await asyncio.wait_for(_receive_frame(websocket), timeout=settings.chat_auth_timeout_seconds)
        payload = decode_access_token(frame["token"]) if frame and frame.get("type") == "auth" else None
        user_id = int(payload["sub"]) if payload else None
    except (asyncio.TimeoutError, KeyError, TypeError, ValueError, WebSocketDisconnect):
        user_id = None
    user = await run_in_threadpool(_in_session, factory, lambda db, uid: db.get(User, uid), user_id) if user_id else None
    if user is None or not user.is_active:
        await _close(websocket, 4401, "Not authenticated")
        return None
    return user


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:
        pass  # already closed


@router.websocket("/queue/ws")
async def queue_socket(websocket: WebSocket, session_factory: SessionFactory):
    """Staff only: live feed of chats entering and leaving the waiting queue."""
    await websocket.accept()
    user = await _authenticate(websocket, session_factory)
    if user is None:
        return
    if not is_staff(user):
        return await _close(websocket, 4403, "Staff only")
    conn = Conn(websocket, asyncio.get_running_loop(), user.id, True)
    hub.join_queue(conn)
    try:
        waiting = await run_in_threadpool(_in_session, session_factory, chat_service.waiting_queue)
        await websocket.send_json({"type": "queue", "chats": waiting})
        while True:
            frame = await _receive_frame(websocket)
            if frame and frame.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        hub.leave_queue(conn)


@router.websocket("/{chat_id}/ws")
async def chat_socket(websocket: WebSocket, chat_id: int, session_factory: SessionFactory, llm: LLMDep):
    await websocket.accept()
    user = await _authenticate(websocket, session_factory)
    if user is None:
        return
    try:
        ready = await run_in_threadpool(_in_session, session_factory, chat_service.snapshot, chat_id, user.id)
    except ChatError as exc:
        return await _close(websocket, 4000 + exc.status_code, exc.detail)

    conn = Conn(websocket, asyncio.get_running_loop(), user.id, is_staff(user))
    hub.join(chat_id, conn)
    try:
        await websocket.send_json({"type": "ready", **ready})
        while True:
            frame = await _receive_frame(websocket)
            if frame is None:
                await websocket.send_json({"type": "error", "code": 422, "detail": "Frames must be JSON objects"})
                continue
            kind = frame.get("type")
            if kind == "ping":
                await websocket.send_json({"type": "pong"})
            elif kind == "message":
                try:
                    outcome: Outcome = await run_in_threadpool(
                        _in_session, session_factory, chat_service.send_message,
                        chat_id, user.id, str(frame.get("body", "")), llm, hub.agents_online(),
                    )
                except ChatError as exc:
                    await websocket.send_json({"type": "error", "code": exc.status_code, "detail": exc.detail})
                    if exc.status_code == 403 and exc.detail == "Account is disabled":
                        return await _close(websocket, 4403, exc.detail)
                    continue
                await hub.publish(chat_id, outcome)
            else:
                await websocket.send_json({"type": "error", "code": 422, "detail": "Unknown frame type"})
    except WebSocketDisconnect:
        pass  # the client left
    finally:
        hub.leave(chat_id, conn)
