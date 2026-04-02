#!/usr/bin/env bash
# Starts the Job Dashboard server and auto-shuts down after 2 hours.
# Usage: ./start_dashboard.sh

DIR="$(cd "$(dirname "$0")" && pwd)"
TIMEOUT=7200 # 2 hours in seconds

echo "Starting Job Dashboard server (auto-shutdown in 2 hours)..."
echo "Open http://localhost:8080 in your browser."
echo ""

python3 "$DIR/server.py" &
SERVER_PID=$!

# Shut down after timeout
(
  sleep $TIMEOUT
  echo ""
  echo "2 hours elapsed — shutting down dashboard server."
  kill $SERVER_PID 2>/dev/null
) &
TIMER_PID=$!

# If the user stops the server manually (Ctrl+C), also kill the timer
trap "kill $SERVER_PID $TIMER_PID 2>/dev/null; echo 'Server stopped.'; exit 0" INT TERM

wait $SERVER_PID
kill $TIMER_PID 2>/dev/null
echo "Server stopped."
