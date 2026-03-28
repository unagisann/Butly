@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv" (
    echo [1/4] Python venvを作成します...
    py -m venv .venv
    if errorlevel 1 (
        echo venv作成に失敗しました。Pythonが入っているか確認してください。
        pause
        exit /b 1
    )
)

echo [2/4] 仮想環境を有効化します...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo 仮想環境の有効化に失敗しました。
    pause
    exit /b 1
)

echo [3/4] pip を更新します...
python -m pip install --upgrade pip

echo [4/4] requirements.txt をインストールします...
pip install -r requirements.txt
if errorlevel 1 (
    echo requirements のインストールに失敗しました。
    pause
    exit /b 1
)

if not exist ".env" if exist ".env.example" (
    echo .env を初期作成します...
    copy ".env.example" ".env" >nul
)

if not exist "user_config.json" if exist "user_config.json.example" (
    echo user_config.json を初期作成します...
    copy "user_config.json.example" "user_config.json" >nul
)

echo.
echo セットアップ完了です。
echo 次は APIkey.env と user_config.json を編集してください。
pause
endlocal