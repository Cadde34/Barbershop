@echo off
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto run_py

where python >nul 2>nul
if not errorlevel 1 goto run_python

echo.
echo A foglalasi rendszerhez Python 3.10 vagy ujabb verzio szukseges.
echo Letoltes: https://www.python.org/downloads/
echo A telepitesnel jelold be az "Add Python to PATH" lehetoseget.
echo.
pause
exit /b 1

:open_browser
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:8000/"
exit /b 0

:run_py
call :open_browser
py -3 server.py
goto end

:run_python
call :open_browser
python server.py

:end
pause
