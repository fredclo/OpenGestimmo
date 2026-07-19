@echo off
echo Installation de l'environnement Python...
python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
echo Installation terminee.
pause
