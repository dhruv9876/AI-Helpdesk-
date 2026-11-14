# AI Helpdesk Backend

College project: an AI-powered helpdesk API built with full-stack Python.
Built in phases. **All five phases are complete.**

| Phase | Scope | Status |
|------|-------|--------|
| 1 | Auth (JWT, roles) and ticket CRUD, comments, internal notes | Done |
| 2 | Knowledge-base ingestion and RAG | Done |
| 3 | Auto-triage, suggested replies, duplicate detection | Done |
| 4 | Live chat (WebSockets) with bot-first handoff | Done |
| 5 | Dashboard (Jinja2 + HTMX), customer chat page | Done |

## Stack
FastAPI, SQLAlchemy 2.0, SQLite (swap `DATABASE_URL` for Postgres later), Pydantic v2, PyJWT, bcrypt, numpy, Jinja2 + htmx (vendored, no CDN), pytest.
Optional: `anthropic` (real Claude answers), `sentence-transformers` (semantic embeddings).

## Run it
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then set SECRET_KEY
python -m scripts.seed                                 # dummy users, tickets and 14 KB articles
uvicorn app.main:app --reload
```
Open **http://localhost:8000** and sign in (agents and admins land on the dashboard, customers on the chat page).
The JSON API and its docs are still at http://localhost:8000/docs (click **Authorize**; put the email in `username`).
To show the demo accounts on the login page, set `SHOW_DEMO_LOGINS=true` in `.env` (demo data only).

Seeded accounts (password `password123`): `admin@example.com`, `agent1@example.com`,
`agent2@example.com`, `customer1@example.com` to `customer3@example.com`.

## Tests
```bash
pytest -q
```
Tests use an in-memory SQLite database, so they never touch real data.

## Roles and rules
| Action | Customer | Agent | Admin |
|---|---|---|---|
| Register (public, always a customer) | yes | | |
| Create ticket | yes | yes | yes |
| See tickets | own only | all | all |
| Edit subject/description | own, while `open` | any | any |
| Close ticket | own | any | any |
| Change status/priority/category/assignee | no | yes | yes |
| Internal notes (staff-only comments) | no | yes | yes |
| Delete ticket | no | no | yes |
| Create staff accounts, enable/disable users | no | no | yes |

Design notes worth mentioning in a viva:
- Customers get **404, not 403**, for other people's tickets so ids can't be probed.
- Public registration can never create staff; only an admin can.
- Passwords are hashed with bcrypt; tokens are short-lived JWTs.
- `resolved_at` is recorded now so Phase 5 can show resolution-time metrics.

## API overview (all under `/api`)
```
POST   /auth/register          POST   /auth/login
GET    /users/me               GET/POST /users            PATCH /users/{id}     (admin)
POST   /tickets                GET    /tickets?status=&priority=&category=&assignee_id=&unassigned=&q=&limit=&offset=
GET    /tickets/{id}           PATCH  /tickets/{id}       DELETE /tickets/{id}  (admin)
POST   /tickets/{id}/comments

GET    /kb/articles            GET  /kb/articles/{id}     (any signed-in user)
POST   /kb/articles            PUT  /kb/articles/{id}     POST /kb/upload (.md/.txt)   (staff)
DELETE /kb/articles/{id}       POST /kb/reindex           (admin)
GET    /kb/search?q=&k=        raw retrieval with similarity scores
POST   /kb/ask                 {"question": "..."} -> answer + sources, or escalate=true
```

GET    /tickets?needs_attention=true            (staff: the AI-flagged queue)
POST   /tickets/{id}/triage[?apply=true]        re-run triage (staff)
GET    /tickets/{id}/duplicates                 POST /tickets/{id}/merge {"target_id"}   (staff)
POST   /tickets/{id}/suggest-reply              GET  /tickets/{id}/suggested-replies     (staff)
POST   /tickets/{id}/suggested-replies/{rid}/accept   {"body": optional edit}   POST .../reject
GET    /ai/stats                                reply acceptance rate, flags, triage and chat hand-off counts (staff)

POST   /chats                                   start a chat (customers; returns your open chat if you have one)
GET    /chats[?status=waiting]                  own chats (customers) / all chats (staff)
GET    /chats/{id}                              history     POST /chats/{id}/messages   HTTP fallback for sending
POST   /chats/{id}/claim                        staff: take a waiting chat (first to claim wins)
POST   /chats/{id}/close                        POST /chats/{id}/ticket   turn the chat into a ticket
WS     /chats/{id}/ws                           live chat       WS /chats/queue/ws   staff: live waiting queue

## Phase 2: how the knowledge base / RAG works
```
article --chunk (paragraphs, <=700 chars)--> chunks --embed--> vectors stored in SQLite
question --embed--> cosine similarity vs all chunks --> top-k passages
   |-- best score < KB_MIN_SCORE ----------------------------> escalate (LLM never called)
   |-- LLM answers from the passages only, citing them ------> answer + sources
   `-- LLM replies NOT_FOUND --------------------------------> escalate
```
- **Two guardrails against hallucination:** a similarity pre-filter, and a prompt that forces the
  model to answer only from the retrieved passages or say `NOT_FOUND`. Passages and questions are
  wrapped as untrusted data to resist prompt injection.
- **LLM:** if `ANTHROPIC_API_KEY` is set (and `pip install anthropic`), answers come from Claude
  (`ANTHROPIC_MODEL`, default `claude-sonnet-5-5`). Otherwise an offline *extractive* mock is used: it
  quotes the best-matching sentences, so everything is demoable with no key or network.
- **Embeddings:** default is a dependency-free hashing embedder (keyword-style matching).
  Set `EMBEDDING_BACKEND=sentence-transformers`, install it, then `POST /api/kb/reindex` for
  semantic search. Each chunk records which model embedded it, so vectors from different models are
  never compared.
- **Knobs** (`.env`): `KB_MIN_SCORE` (default 0.12), `KB_TOP_K`, `CHUNK_MAX_CHARS`.

### Measured quality (offline mock + hashing embedder, 17 labelled questions)
Run `python -m scripts.eval_rag`. Current results: right article ranked #1 for 94% of questions
and in the top 3 for 100%; 94% of in-domain questions answered; 6/6 off-topic questions escalated.
`tests/test_kb.py` fails if these numbers drop, so retrieval changes can't silently make it worse.

### Known limitations (good viva material)
- The hashing embedder matches **words, not meaning**. "ship my order to a different address" can
  rank "Changing your *email* address" first because both contain "address". Semantic embeddings
  fix this class of error (not measured here: the model download was not possible in the build sandbox).
- Relevant and irrelevant similarity scores **overlap** (a good question scored 0.10; an off-topic
  one scored 0.14 because it shared the word "order"). That is why the LLM `NOT_FOUND` check exists
  on top of the threshold, and why `KB_MIN_SCORE` is a tunable trade-off between answering more and
  escalating more.
- Articles should be written in customers' own words: adding "charged twice / double charged" to the
  duplicate-charge article lifted the answer rate from 88% to 94%.
- A query-side IDF re-weighting experiment made results *worse* on this small corpus and was reverted.
- Search loads all vectors into memory: fine for hundreds of articles, not for millions (use pgvector then).
- Only `.md` and `.txt` uploads; PDF ingestion is not implemented.

## Layout
```
app/
  main.py        app + router wiring
  models.py      User, Ticket, Comment
  schemas.py     request/response models
  core/          config, database, security (hashing, JWT), deps (auth/roles)
  api/           auth.py, users.py, tickets.py, kb.py, ticket_ai.py, chats.py, helpers.py
  services/      chunking, embeddings, kb, llm, rag, triage, duplicates, ai_pipeline, chat, chat_hub, metrics
  web/           the dashboard: deps (sessions, CSRF), auth, overview, tickets, chats, kb, users + templates/
  static/        css, js (chat.js, queue.js, app.js), vendored htmx and fonts
scripts/seed.py  dummy data
scripts/eval_rag.py  retrieval/answer-quality evaluation
scripts/chat_client.py  terminal client to demo live chat
scripts/eval_triage.py  triage accuracy (dev + held-out sets)
scripts/eval_duplicates.py  duplicate-threshold calibration
scripts/kb_articles.py  the dummy help-centre articles
tests/           249 tests (+ tests/browser: real-browser checks)
```


## Phase 3: AI triage, suggested replies, duplicates

**Upgrading from Phase 2:** the ticket table gained columns. Delete `helpdesk.db` and re-run
`python -m scripts.seed` (this project uses `create_all`, not migrations).

```
customer creates ticket --> response returns immediately
        |
        `--> background task (FastAPI BackgroundTasks, no Celery/Redis needed):
              1. triage -> category, priority, sentiment, confidence   (stored in ai_* fields)
              2. embed ticket, compare with other open/pending tickets  (duplicate check)
              3. flag tickets that need attention, with a reason
```
- **Suggest-only.** Triage results are always stored as suggestions. They are copied onto the real
  `category`/`priority` once, on first triage, and only if nobody has set them. After that humans
  own those fields; `POST /triage?apply=true` overwrites on request.
- **Customers never see AI fields** (sentiment, flags, duplicate links). Tested.
- **Suggested replies** are grounded in the knowledge base (same guardrails as `/kb/ask`: no
  confident match, or the model replies `NOT_FOUND`, means no draft). A draft is stored as
  `pending`; the customer sees nothing until an agent **accepts** it (optionally edited), which posts
  it as a normal comment. Accepted / edited / rejected are recorded, giving `/ai/stats` an
  acceptance rate for the Phase 5 dashboard.
- **Attention flags:** urgent priority, angry sentiment, or *repeat contact* (the same customer has a
  similar open ticket).
- **Merging** is manual and only between tickets from the same customer, so one customer's
  conversation is never attached to another's. Duplicates across customers are still shown to staff
  (useful for spotting a widespread problem) but cannot be merged.
- **Claude vs offline:** with `ANTHROPIC_API_KEY` set, triage and reply drafting use Claude. Triage asks
  for strict JSON and validates every field; unusable output falls back to
  `general / medium / neutral` with confidence 0 instead of crashing. Ticket text is passed as
  untrusted data, never in the system prompt. Without a key, transparent keyword rules are used.
- If the AI step fails, ticket creation still succeeds and the ticket stays untriaged.
  Set `AI_AUTO_TRIAGE=false` to turn the background step off.

### Measured quality (offline rules, `python -m scripts.eval_triage`)
The rules were tuned on a 20-ticket development set. A 16-ticket held-out set was written first and
not used for tuning, so it is the honest estimate:

| | Dev (tuned on) | Held-out |
|---|---|---|
| Category | 100% | **88%** |
| Sentiment | 100% | **94%** |
| Priority, exact | 80% | **44%** |
| Priority, within one level | 100% | **88%** |

Duplicate detection (`python -m scripts.eval_duplicates`): the threshold was **calibrated on labelled
pairs** (true duplicates scored 0.18 to 0.46, non-duplicates up to 0.07), giving `DUP_MIN_SCORE=0.15`.
My first guess of 0.45 would have found only 12% of duplicates. On the held-out test pairs, precision
and recall are both 100%.

Reply drafting: 12/12 seeded tickets get a draft from the correct article (in-domain only).

### Bugs found by testing (and fixed)
- A hyphenated keyword ("two-factor") was split so the word "two" made any ticket look like an
  account issue.
- "the app closes **immediately**" was marked urgent: urgency words now count only when they modify a
  request ("help me immediately").
- Repeat-contact looked only at the single best match, so another customer's ticket could hide the
  same customer's duplicate. Reproduced with a failing test first.
- Two safety properties (customers can't see AI fields; auto-apply never overwrites human edits) were
  checked by deliberately breaking them and confirming the tests fail.

### Known limitations
- **Priority is the weak spot of the offline rules** (44% exact on held-out): words like "refund" or
  "error" push priority up even when the customer is just asking a question. Claude should do much
  better; this is not measured here (no API key in the build sandbox).
- The held-out set is small (16) and was written by the same person who wrote the rules, so treat the
  numbers as indicative, not precise. The held-out set was re-run once after the "immediately" fix
  (scores unchanged); that fix came from a live test, not from the held-out tickets.
- Duplicate calibration used mostly different-topic negatives; same-topic-different-problem pairs
  (e.g. "refund not received" vs "refund timeline") are under-tested and may be flagged.
- Similarity matching loads all active tickets into memory: fine at college scale, not for millions.


## Phase 4: live chat with bot-first handoff

```
customer opens chat --> BOT answers from the knowledge base (cites the article)
        |
        |-- customer asks for a person ("agent", "speak to a human", ...)   --> WAITING
        |-- customer sounds upset (angry wording / shouting)                --> WAITING
        |-- frustrated AFTER the bot already answered once                  --> WAITING
        |-- bot has no confident answer (same guardrails as /kb/ask)        --> WAITING
        |
   WAITING: customer messages are saved, the bot stays silent, staff queue is notified live
        |-- an agent claims it (atomic: first one wins)                     --> ACTIVE (agent <-> customer)
        |-- nobody online? the customer is offered "turn this chat into a ticket"
   ACTIVE / WAITING / BOT --> CLOSED (customer, assigned agent, or admin) --> optional ticket
```
**Try it** (needs the server running, two terminals; the seeded accounts all use `password123`):
```bash
python -m scripts.chat_client agent1@example.com       # waits for a customer, claims, chats
python -m scripts.chat_client customer1@example.com    # ask a question, then type "agent"
```

**WebSocket protocol** (full detail in the docstring of `app/api/chats.py`):
- Connect to `/api/chats/{id}/ws`, then send `{"type":"auth","token":"<JWT>"}` as the **first message**
  (browsers cannot set headers on a WebSocket, and tokens in URLs leak into logs). No auth within 10 s
  closes the socket with code 4401.
- Send `{"type":"message","body":"..."}`; receive `ready`, `message`, `status`, `error`, `pong` events.
  Bad input gets an `error` frame and the socket stays open. Close codes: 4401 unauthenticated,
  4403 forbidden, 4404 not found.
- Staff can also open `/api/chats/queue/ws` for a live feed of chats entering and leaving the queue.

**Design notes**
- The rules live in `services/chat.py` as plain synchronous code that returns events; the WebSocket
  layer only delivers them. So every rule is tested without opening a socket, and the same logic
  serves both REST and WebSocket clients.
- Customers cannot see other customers' chats (404, not 403) or the internal hand-off reason.
  Only the agent who claimed a chat may speak in it; other staff can watch.
- Per-user rate limit (20 messages/minute/chat) and a 2,000-character message cap protect the bot.
- Disabled accounts are checked on every message, not only at connect time.

### Bugs found by testing (and fixed)
- **Offline bot was too strict (also affected Phase 2).** "How do I reset my password?" was handed off
  because the mock demanded two matching words *within one sentence*, but the article says "reset" and
  "password" in different sentences. It now counts matches across the best sentences together. The
  Phase 2 evaluation is unchanged (94% answered, 6/6 off-topic still escalated).
- **A closing tab could swallow the staff notification.** The hub delivered to the customer's socket
  first and the staff queue last; if the customer disconnected right after a hand-off, the queue
  never heard about the waiting chat. Staff are now notified first.
- **Stale message history.** Reading `chat.messages` (a cached relationship) could miss newly added
  messages when a session was reused; history is now always queried explicitly.
- Six safety properties were checked by breaking them on purpose and confirming a test fails: atomic
  claim, bot silent once a human is involved, customer isolation, hidden hand-off reason, claim-before-
  speaking, staff-only queue.

### Known limitations
- **Single process only.** The hub keeps connections in memory, so it works with one server worker.
  Several workers would need something like Redis pub/sub between them.
- **The bot has no conversation memory.** Each customer message is answered on its own, so a follow-up
  like "and what about refunds?" may not be understood and will be handed to a human.
- Frustration/anger detection in chat always uses the offline keyword rules (it runs on every message,
  so it does not call an LLM), even when an Anthropic key is set. It misses sarcasm and calm complaints.
- "Wants a human" detection is a set of patterns tested on 19 example phrases: indicative, not exhaustive.
- A WebSocket is authenticated once, at connect. If the JWT expires mid-chat the socket keeps working
  until it reconnects (the account being disabled is still caught on the next message).
- No typing indicators, read receipts, idle timeouts or agent presence beyond "someone is on the queue".
- Message text is stored and sent as-is. The Phase 5 dashboard must HTML-escape it.


## Phase 5: the dashboard

Open `/` and sign in. Staff get the dashboard, customers get a chat page.

| Page | What it is for |
|---|---|
| **Overview** | Leads with *what needs you now*: flagged tickets (urgent, upset customer, repeat contact) and chats waiting for an agent. Beside it: active tickets, median first reply and time to resolve (30 days), how often agents use the AI drafts, how many chats the bot finishes alone. Then tickets per day and active tickets by priority and category. |
| **Tickets** | The work queue: flagged first, then priority, then longest-waiting. Filters (status, priority, category, assignee, needs-attention, search) update without a page reload and keep a clean, shareable URL. |
| **Ticket detail** | The conversation, internal notes, change status / priority / category / assignee, AI triage with "use these values", **suggested replies you edit before posting**, and possible duplicates with a merge button. |
| **Chats** | Live queue of customers waiting for a person (a toast and the top-bar badge update the moment one arrives), claim, then a live room with the transcript, hand-off reason and the customer's other tickets. |
| **Knowledge base** | Add / edit / upload articles, see how each is cut into pieces, and **test the assistant** with a customer question to see what it would answer or that it would hand over. |
| **Users** (admin) | Create agent/admin accounts, change roles, disable accounts. |
| **/chat** (customer) | Chat with the bot, ask for a person, turn the chat into a ticket. |

### Design
One idea: the **triage tag**. Every ticket carries a coloured edge *and* its priority written out, so urgency reads at a glance and never depends on colour alone. Everything else is deliberately flat (1px borders, no shadows or gradients). Atkinson Hyperlegible for reading (designed for legibility, which matters on an all-day tool) and Archivo's width axis for headings. Light and dark follow the OS setting. Colours are CSS variables in `static/css/app.css`.

### How it is built
- **Server-rendered pages enhanced with htmx.** Each action returns the HTML for the part of the page that changed. No build step, no JavaScript framework.
- **One set of rules.** Dashboard actions call the same Python functions as the JSON API, so permissions and validation exist once. (Calling an API function directly skips the role check FastAPI normally applies through dependencies, so each dashboard route declares its own role requirement. Tests break that on purpose to prove it.)
- **Sessions:** the JWT lives in an `HttpOnly`, `SameSite=Lax` cookie, so page scripts cannot read it. **CSRF:** every state-changing request must carry an `X-CSRF-Token` header, an HMAC of the session cookie (htmx adds it from `<body>`).
- **WebSockets:** the cookie is unreadable by scripts, so `chat.js` asks `/ws-token` for a 2-minute token and sends it as the first frame.
- **Strict Content-Security-Policy** (`script-src 'self'; style-src 'self'`): no inline scripts or styles anywhere, so a CSP violation shows up as a failing test. `/docs` is exempt (Swagger needs inline scripts). Signed-in pages are `Cache-Control: no-store`, and htmx's history cache is disabled so page contents are not left in the browser's storage after sign-out.
- **Escaping:** Jinja auto-escapes everything; `chat.js` builds messages with `textContent` only. Tested with `<script>` / `<img onerror>` payloads in tickets, comments, chats, categories and knowledge-base text.
- Charts are plain server-rendered SVG (no chart library).

### Testing
- **249 pytest tests** (`pytest -q`), including 58 for the dashboard over HTTP and 22 for the metrics maths.
- **Real-browser tests** in `tests/browser` (Puppeteer + headless Chromium; see its README): **88 checks** across ticket actions, a live customer-and-agent chat in two browsers (including an XSS attempt), admin pages, forged requests and session expiry, plus an audit of 8 pages at 390 / 768 / 1280 px for missing labels, duplicate ids, heading structure, tap targets and horizontal overflow (0 issues).
- **Protections were checked by breaking them on purpose** (CSRF check, staff gate, admin gate, HTML escaping, open-redirect guard, CSP header, disabled-account check, admin-only delete, no-store caching): each break made a test fail.
- **Contrast** of every text/background pair in both themes was calculated with the WCAG formula from the CSS colour values (all at least 4.5:1 after the two fixes below). Calculated from the declared colours, not measured on rendered pixels.

### Bugs found by testing (and fixed)
- **"Unassigned" and category "None" silently did nothing.** FastAPI treats an empty form value as *missing*, so clearing a field was read as "leave it alone". Found by a unit test, then confirmed in the browser. Clearing now sends an explicit `none`.
- **A hidden button was visible.** "Start a new chat" showed during an active chat because `.btn { display: inline-flex }` overrides the `hidden` attribute. Found by looking at a screenshot.
- **Two colour pairs failed contrast:** the red "Angry" chip in light mode (4.35:1) and the alert badge in dark mode (2.49:1). Found only by calculating, not by eye.
- **Header chips went stale** after changing status or priority in the sidebar; they now refresh together.
- **The bot cited three articles when one answered.** Weakly related extras are no longer cited.
- **The chat queue previewed the bot's own notice** ("connecting you with an agent") instead of what the customer said.
- **The add-user form kept the typed password** after success; the server now returns a blank form.
- The triage-tag edge appeared on tables with no priority; and one help text claimed articles split "at blank lines" (they pack up to ~700 characters).

### Known limitations
- **Customers have no web page for tickets**, only chat (their tickets are reachable through the JSON API). Agents can see a customer's tickets from the chat room.
- **Login has no rate limiting or lockout.** Sessions are 60-minute JWTs. Signing out deletes the cookie but cannot revoke a copied token (there is no server-side session store); disabling an account takes effect on the next request.
- **Live updates cover chats and the top-bar counts.** Other pages refresh when you act or reload (the chat lists also poll every 20 s).
- Browser-tested in **headless Chromium only**, not Firefox or Safari, and with no screen-reader testing. The accessibility audit is automated checks plus my own review.
- "Median first reply" counts a staff reply posted on the ticket; a reply given in a chat is not counted. Metrics are computed on each request, which is fine for thousands of tickets, not millions.
- Chart bars have no touch tooltips (the numbers are printed on the bars and available to screen readers as a list). Dark mode follows the OS; there is no toggle. Atkinson draws a slashed zero by design.
- No article versioning or approval flow in the knowledge base; uploads are `.md` and `.txt` only.
- The chat hub is in-memory, so the whole app needs a single server process (see Phase 4).
