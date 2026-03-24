@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo 先に 01_setup_requirements.bat を実行してください。
    pause
    exit /b 1
)

echo [1/2] FastAPI を起動します...
start "Butly FastAPI" cmd /k "cd /d ""%~dp0"" && call "".venv\Scripts\activate.bat"" && python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload"

timeout /t 3 /nobreak >nul

echo [2/2] Streamlit を起動します...
start "Butly Streamlit" cmd /k "cd /d ""%~dp0"" && call "".venv\Scripts\activate.bat"" && python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false"

timeout /t 5 /nobreak >nul
start "" "http://127.0.0.1:8501"

endlocal