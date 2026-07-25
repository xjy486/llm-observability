#!/bin/bash
set -e

# Start Core (FastAPI) in background
cd /app/core
uvicorn api.main:app --host 0.0.0.0 --port ${CORE_PORT:-8001} &
CORE_PID=$!

# Start Proxy in background
cd /app/proxy
PROXY_HOST=0.0.0.0 \
PROXY_PORT=${PROXY_PORT:-8082} \
UPSTREAM_URL=${UPSTREAM_URL:-http://localhost:3000} \
OBSERVABILITY_ENDPOINT=http://localhost:${CORE_PORT:-8001} \
PAYLOAD_STRATEGY=${PAYLOAD_STRATEGY:-masked} \
GATEWAY_NAME=${GATEWAY_NAME:-one-api-proxy} \
python main.py &
PROXY_PID=$!

# Wait for Core to be ready
echo "Waiting for Core to start..."
for i in $(seq 1 30); do
    if curl -s http://localhost:${CORE_PORT:-8001}/api/v1/health > /dev/null 2>&1; then
        echo "Core is ready!"
        break
    fi
    sleep 1
done

# Start a simple HTTP server for the built UI
cd /app/ui/dist
python -m http.server ${UI_PORT:-3000} --bind 0.0.0.0 &
UI_PID=$!

echo "All services started:"
echo "  Core:   http://0.0.0.0:${CORE_PORT:-8001}"
echo "  Proxy:  http://0.0.0.0:${PROXY_PORT:-8082}"
echo "  UI:     http://0.0.0.0:${UI_PORT:-3000}"

# Wait for any process to exit
wait -n $CORE_PID $PROXY_PID $UI_PID

# If one exits, kill the others
kill $CORE_PID $PROXY_PID $UI_PID 2>/dev/null
exit 1
