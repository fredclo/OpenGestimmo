@echo off
if not exist "immobilisations.db" (
    python setup_db.py
)
echo Demarrage de l'application... (Fermez cette fenetre pour arreter)
start http://127.0.0.1:8000
call venv\Scripts\activate.bat
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
