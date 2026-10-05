@echo off
rem ── 后台常驻启动器 ──────────────────────────────────────────────
rem  等微信起来 → 跑 bot → 挂了等 10 秒重来
rem  日志：logs\wechat-bot.out.log
rem
rem  真要有用，得用 WMI 拉起（见 README「后台常驻」一节），
rem  否则从 IDE / 终端直接跑，父进程一退就被一起杀掉。

setlocal
cd /d "%~dp0"

set PY=python
if exist "%~dp0.venv\Scripts\python.exe" set PY=%~dp0.venv\Scripts\python.exe

if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%~dp0data" mkdir "%~dp0data"

:loop
  tasklist /fi "imagename eq Weixin.exe" 2>nul | findstr /i "Weixin.exe" >nul
  if errorlevel 1 (
    echo [%date% %time%] 微信还没起来，等 15 秒 >> "%~dp0logs\wechat-bot.out.log"
    timeout /t 15 /nobreak >nul
    goto loop
  )
  echo [%date% %time%] starting wechat-bot >> "%~dp0logs\wechat-bot.out.log"
  "%PY%" "%~dp0wechat-bot.py" >> "%~dp0logs\wechat-bot.out.log" 2>&1
  echo [%date% %time%] exited, restart in 10s >> "%~dp0logs\wechat-bot.out.log"
  timeout /t 10 /nobreak >nul
goto loop
