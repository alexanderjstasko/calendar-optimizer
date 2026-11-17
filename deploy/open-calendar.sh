#!/bin/sh
# Opens the calendar app in a Firefox kiosk window; waits for the backend first.
URL=http://localhost:8000
for _ in $(seq 1 60); do
    curl -sf -o /dev/null "$URL/health" && break
    sleep 1
done
exec firefox --kiosk --new-window "$URL"
