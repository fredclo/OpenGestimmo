#!/bin/bash
cd "$(dirname "$0")"
nohup ./venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 > /dev/null 2>&1 &
sleep 2
xdg-open http://127.0.0.1:8000
