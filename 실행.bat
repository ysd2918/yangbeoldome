@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 이미 실행 중이면 브라우저만 열기
python -c "import socket,sys;sys.exit(0 if socket.socket().connect_ex((\"127.0.0.1\",8765))==0 else 1)"
if %errorlevel%==0 (
  start "" http://localhost:8765
  exit /b
)
title 양벌 테니스 돔 빈 코트 찾기 - 이 창을 닫으면 종료됩니다
python server.py --open
pause
