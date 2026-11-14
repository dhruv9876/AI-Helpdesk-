"""Who is connected to which chat, and delivery of events to them.

In-process only: it works with one server process (e.g. a single `uvicorn` worker). With several
workers, a message handled by worker A could not reach a socket held by worker B; you would put
Redis pub/sub between them. That is deliberately out of scope for this project.

Rule: a socket is only ever written to on the event loop that owns it. In production there is a
single loop so this is just `await ws.send_json()`. The cross-loop branch matters because test
clients run every WebSocket on its own loop, and because it keeps the hub correct if it is ever
used from another thread.
"""
import asyncio
import logging
from dataclasses import dataclass

from fastapi import WebSocket

from app.services.chat import Outcome

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Conn:
    ws: WebSocket
    loop: asyncio.AbstractEventLoop
    user_id: int
    is_staff: bool


class ChatHub:
    def __init__(self) -> None:
        self._rooms: dict[int, set[Conn]] = {}
        self._queue: set[Conn] = set()

    # --- membership ---
    def join(self, chat_id: int, conn: Conn) -> None:
        self._rooms.setdefault(chat_id, set()).add(conn)

    def leave(self, chat_id: int, conn: Conn) -> None:
        room = self._rooms.get(chat_id)
        if room:
            room.discard(conn)
            if not room:
                self._rooms.pop(chat_id, None)

    def join_queue(self, conn: Conn) -> None:
        self._queue.add(conn)

    def leave_queue(self, conn: Conn) -> None:
        self._queue.discard(conn)

    def agents_online(self) -> bool:
        return bool(self._queue)

    def room_size(self, chat_id: int) -> int:
        return len(self._rooms.get(chat_id, ()))

    def reset(self) -> None:
        self._rooms.clear()
        self._queue.clear()

    # --- delivery ---
    async def _send(self, conn: Conn, payload: dict) -> bool:
        """Send one event to one socket. Returns False if the socket is dead."""
        try:
            if conn.loop is asyncio.get_running_loop():
                await conn.ws.send_json(payload)
            else:
                future = asyncio.run_coroutine_threadsafe(conn.ws.send_json(payload), conn.loop)
                await asyncio.wrap_future(future)
            return True
        except Exception:  # closed socket, closed loop, network error...
            return False

    async def _broadcast(self, targets: set[Conn] | list[Conn], payload: dict, on_dead) -> None:
        for conn in list(targets):
            if not await self._send(conn, payload):
                on_dead(conn)

    async def publish(self, chat_id: int, outcome: Outcome) -> None:
        # Staff queue first. The queue event is what gets a waiting customer help, and this
        # coroutine can be cancelled mid-way if the customer's socket drops right after the
        # hand-off, so the more important delivery must not be the last one.
        for event in outcome.queue:
            await self._broadcast(self._queue, event, self.leave_queue)
        for event in outcome.events:
            await self._broadcast(self._rooms.get(chat_id, ()), event, lambda c: self.leave(chat_id, c))

    async def send_to(self, conn: Conn, payload: dict) -> None:
        await self._send(conn, payload)


hub = ChatHub()
