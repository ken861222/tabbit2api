#!/bin/bash
# Launch Tabbit with CDP enabled and GPU disabled for stability
pkill -f "MacOS/Tabbit" 2>/dev/null
sleep 2

/Applications/Tabbit.app/Contents/MacOS/Tabbit \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --no-first-run \
  --disable-gpu \
  2>/dev/null &

echo "Tabbit starting (PID=$!)..."
# Wait for CDP
for i in $(seq 1 15); do
    sleep 2
    if curl -s http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
        echo "CDP ready after $((i*2))s"
        exit 0
    fi
done
echo "CDP not ready after 30s"
exit 1
