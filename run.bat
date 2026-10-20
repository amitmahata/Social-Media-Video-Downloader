@echo off
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
echo Open http://localhost:8000 in your browser
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
