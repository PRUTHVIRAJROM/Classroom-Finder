@echo off
rem Double-click this file to send a room's people count to your live website.
rem Edit the 4 lines below once. The password is asked each time, so it is never saved in this file.

set SITE=https://YOUR-SITE-NAME.onrender.com
set ROOM=301
set SOURCE=0
set DETECTOR=face

cd /d "%~dp0"
set /p KEY=Password (the CF_API_KEY from Render):
call .venv\Scripts\activate.bat
echo.
echo Sending counts for room %ROOM% to %SITE%  (close this window to stop)
python agent\camera_agent.py --room %ROOM% --source %SOURCE% --detector %DETECTOR% --server %SITE% --api-key %KEY%
pause
