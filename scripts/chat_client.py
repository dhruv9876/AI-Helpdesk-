"""Terminal chat client: demo live chat before the Phase 5 dashboard exists.

Run the server first (uvicorn app.main:app), then open two terminals:

  python -m scripts.chat_client customer1@example.com     # a customer: chat with the bot
  python -m scripts.chat_client agent1@example.com        # an agent: waits for a customer, claims, chats

Options:  --host localhost:8000   --password password123   --chat ID   --linger SECONDS
Type a line to send it. Commands:  /close  (close the chat)   /ticket  (turn it into a ticket)   /quit

Piped input works too (the client waits --linger seconds for replies after input ends):
  printf 'How do I reset my password?\nagent\n' | python -m scripts.chat_client customer1@example.com
"""
import argparse
import asyncio
import json
import sys

import httpx
from websockets.asyncio.client import connect


def show(event: dict) -> None:
    kind = event.get("type")
    if kind == "message":
        m = event["message"]
        print(f"  [{m['sender_name']}] {m['body']}", flush=True)
    elif kind == "status":
        agent = event.get("agent")
        print(f"  -- chat is now {event['status']}" + (f" (agent: {agent['name']})" if agent else "") + " --", flush=True)
    elif kind == "error":
        print(f"  !! {event['detail']}", flush=True)
    elif kind == "ready":
        for m in event["messages"]:
            show({"type": "message", "message": m})


async def authenticate(ws, token: str) -> None:
    await ws.send(json.dumps({"type": "auth", "token": token}))


async def wait_for_waiting_chat(base_ws: str, base_http: str, token: str, headers: dict) -> int:
    waiting = httpx.get(f"{base_http}/chats?status=waiting", headers=headers).json()
    if waiting:
        return waiting[0]["id"]
    print("Waiting for a customer to ask for a human...", flush=True)
    async with connect(f"{base_ws}/chats/queue/ws") as queue:
        await authenticate(queue, token)
        async for raw in queue:
            event = json.loads(raw)
            if event["type"] == "chat_waiting":
                return event["chat"]["id"]
    raise SystemExit("queue closed")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--host", default="localhost:8000")
    ap.add_argument("--password", default="password123")
    ap.add_argument("--chat", type=int)
    ap.add_argument("--linger", type=float, default=2.0, help="seconds to keep listening after input ends")
    args = ap.parse_args()
    http_url, ws_url = f"http://{args.host}/api", f"ws://{args.host}/api"

    login = httpx.post(f"{http_url}/auth/login", data={"username": args.email, "password": args.password})
    if login.status_code != 200:
        raise SystemExit(f"Login failed: {login.text}")
    token = login.json()["access_token"]
    me = login.json()["user"]
    headers = {"Authorization": f"Bearer {token}"}

    chat_id = args.chat
    if me["role"] == "customer":
        chat_id = chat_id or httpx.post(f"{http_url}/chats", headers=headers).json()["id"]
    else:
        chat_id = chat_id or await wait_for_waiting_chat(ws_url, http_url, token, headers)
        claim = httpx.post(f"{http_url}/chats/{chat_id}/claim", headers=headers)
        if claim.status_code != 200:
            raise SystemExit(f"Could not claim chat {chat_id}: {claim.json().get('detail')}")
    print(f"Connected as {me['full_name']} ({me['role']}) to chat #{chat_id}. /close /ticket /quit", flush=True)

    async with connect(f"{ws_url}/chats/{chat_id}/ws") as ws:
        await authenticate(ws, token)

        async def reader():
            async for raw in ws:
                show(json.loads(raw))

        listener = asyncio.create_task(reader())
        try:
            while True:
                line = (await asyncio.to_thread(sys.stdin.readline))
                if not line:  # end of input
                    await asyncio.sleep(args.linger)
                    break
                line = line.strip()
                if line == "/quit":
                    break
                if line == "/close":
                    httpx.post(f"{http_url}/chats/{chat_id}/close", headers=headers)
                elif line == "/ticket":
                    res = httpx.post(f"{http_url}/chats/{chat_id}/ticket", headers=headers)
                    print(f"  -- ticket #{res.json().get('id')} created --" if res.status_code == 201 else f"  !! {res.json().get('detail')}", flush=True)
                elif line:
                    await ws.send(json.dumps({"type": "message", "body": line}))
        finally:
            listener.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
