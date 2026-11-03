@echo off
rem ---------------------------------------------------------------------------
rem One-click entry: start the local collector console and open it in a browser.
rem
rem Every comment in this file is ASCII on purpose. A non-ASCII `rem` line makes
rem cmd.exe's read pointer drift: besides printing a bogus
rem "not recognized as an internal or external command" on every double-click,
rem it also *swallows the echo lines after it*. Putting the codepage switch first
rem does not help. Measured during development - see collector/collector/launcher.py
rem for the numbers.
rem Chinese is fine inside echo arguments, so that is where it lives.
rem ---------------------------------------------------------------------------
"%SystemRoot%\System32\chcp.com" 65001 >nul

rem The console listens on 127.0.0.1 only; other machines on the same network
rem cannot reach it. Results are written under <project>\runs\.
setlocal
cd /d "%~dp0collector"

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 找不到虚拟环境：%~dp0collector\.venv
  echo.
  echo 重新创建（需要联网装依赖）：
  echo     cd /d "%~dp0collector"
  echo     py -3.13 -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo 正在启动本地采集控制台： http://127.0.0.1:8757/
echo 结果会写到： %~dp0runs\
echo 关闭这个窗口或按 Ctrl+C 就停止界面（已跑完的结果不受影响）。
echo.
.venv\Scripts\python.exe -m collector ui --base-dir ..

echo.
echo 界面已停止。
pause
