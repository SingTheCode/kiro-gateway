#!/bin/bash

PID=$(lsof -ti :8000)
if [ -n "$PID" ]; then
  echo "Killing process on port 8000 (PID: $PID)"
  kill -9 $PID
  sleep 1
fi

mkdir -p debug_logs

echo "Syncing dependencies..."
uv sync

nohup uv run python main.py > debug_logs/server.log 2>&1 &
echo "Server started (PID: $!)"
