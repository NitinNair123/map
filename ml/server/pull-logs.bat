@echo off
rem Copies the server's logger files to this laptop and adds the new rows to ml\data. Windows Task Scheduler runs it
rem every 15 minutes. Put it in the SafeRoute folder (the one with ml\ inside). Server address: data\server_host.txt
setlocal
cd /d "%~dp0"
set /p HOST=<data\server_host.txt
if not exist ml\data\server mkdir ml\data\server
scp -q -B -i "%USERPROFILE%\.ssh\saferoute_pull" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 ^
  logpull@%HOST%:/opt/saferoute-logger/ml/data/*.csv ml\data\server\ >> ml\data\server\pull.log 2>&1
if errorlevel 1 (echo %date% %time% copy failed>> ml\data\server\pull.log & exit /b 1)
py ml\merge_logs.py >> ml\data\server\pull.log 2>&1
echo %date% %time% ok>> ml\data\server\pull.log
