@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"
if not defined PY set "PY=python"

rem No arguments: print help and stop.
if "%~1"=="" goto :HELP

rem First run (or a broken environment): playwright is not importable yet.
%PY% -c "import playwright" >nul 2>nul
if errorlevel 1 goto :NEEDSETUP
goto :RUN

:NEEDSETUP
echo.
echo   依赖还没装好（缺 playwright 或浏览器内核）。
echo   请先双击运行同目录下的 setup.bat —— 只需一次。
echo.
echo   装完可以跑 run.bat doctor 做一次环境自检。
echo.
pause
exit /b 1

:HELP
echo.
echo   xxtutor - no arguments given. Common commands:
echo.
echo     run.bat login                       log in once (QR or password)
echo     run.bat courses                     list my courses
echo     run.bat homework -c COURSE          list assignments of a course
echo     run.bat answer -c COURSE -w WORK    fetch - solve - save (no submit)
echo     run.bat answer --url PAGE_URL       go straight to an answer page
echo     run.bat bank                        local answer bank stats
echo     run.bat doctor                      environment self-check
echo.
echo   Chinese course and assignment names are fine, e.g.
echo     run.bat answer -c [course] -w [work]
echo.
echo   First time here? Run setup.bat once to install playwright + Chromium.
echo.
pause
exit /b 0

:RUN
%PY% -X utf8 -u -m xxtutor %*
set RC=%errorlevel%
echo.
echo [exit code %RC%]
pause
exit /b %RC%
