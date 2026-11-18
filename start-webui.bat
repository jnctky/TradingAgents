@echo off
rem Compatibility shim: Service Hub TA_START historically points at this path.
rem Real launcher lives in my_tools\start-webui.bat (moved 2026-11).
call "%~dp0my_tools\start-webui.bat" %*
exit /b %ERRORLEVEL%
