#!/bin/bash
# Real-browser tests for the dashboard. Needs Node 18+.
#   cd tests/browser && npm install && ./run.sh
# It reseeds a THROWAWAY database, starts the server on port 8771, runs every script, and stops it.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$HERE/../.."
PORT=8771
cd "$ROOT"
export DATABASE_URL="sqlite:///./browser_tests.db" SHOW_DEMO_LOGINS=true
PY="${PYTHON:-python}"
status=0
for script in interact1 chat1 admin1 audit; do
  rm -f browser_tests.db; $PY -m scripts.seed > /dev/null
  $PY -m uvicorn app.main:app --port $PORT > /tmp/helpdesk_browser_$script.log 2>&1 &
  UV=$!
  for i in $(seq 1 40); do curl -s localhost:$PORT/health > /dev/null && break; sleep 0.3; done
  echo "######## $script"
  (cd "$HERE" && BASE_URL=http://localhost:$PORT node $script.mjs) || status=1
  kill $UV 2>/dev/null; wait $UV 2>/dev/null
done
rm -f browser_tests.db
exit $status
