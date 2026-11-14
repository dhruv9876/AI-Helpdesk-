# Browser tests

Drive the real dashboard in headless Chromium (Puppeteer): htmx swaps, the live chat over WebSockets,
the strict Content-Security-Policy (any CSP violation shows up as a console error and fails the run),
role restrictions, session expiry, and an accessibility / phone-layout audit.

```bash
cd tests/browser
npm install
./run.sh            # reseeds a throwaway database for each script, starts the server, runs, stops
```
Set `CHROME_PATH=/path/to/chrome` to use your own browser instead of the bundled one.
Scripts: `interact1` (tickets and queue), `chat1` (customer and agent chatting live, including an XSS
attempt), `admin1` (users, knowledge base, permissions, sessions), `audit` (labels, headings, tap targets,
horizontal overflow at 390 / 768 / 1280 px).
