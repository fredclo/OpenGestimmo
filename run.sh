#!/bin/bash
cd "$(dirname "$0")"
echo "Démarrage d'Open Gestimmo... (Ctrl+C pour arrêter)"
./venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
