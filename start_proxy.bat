@echo off
cd /d C:\Users\bot\proxy
powershell -ExecutionPolicy Bypass -File start_proxy.ps1 %*
pause
