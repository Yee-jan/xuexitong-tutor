@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple

echo ============================================================
echo   xxtutor 首次安装（只需跑一次）
echo ============================================================
echo.

rem ---------- 1. 找 Python ----------
set "PY="
where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if %errorlevel%==0 set "PY=python"
)
if not defined PY goto :NOPY

echo [1/3] 找到 Python：
%PY% -c "import sys; print('      ' + sys.version.replace(chr(10),' ')); print('      ' + sys.executable)"
if errorlevel 1 goto :NOPY
echo.

rem ---------- 2. 装 playwright ----------
echo [2/3] 安装 playwright（唯一依赖，用清华镜像加速）
%PY% -m pip install -r requirements.txt -i %MIRROR% --progress-bar off
if errorlevel 1 (
  echo.
  echo   镜像安装失败，改用默认 PyPI 重试...
  %PY% -m pip install -r requirements.txt --progress-bar off
  if errorlevel 1 goto :NOPIP
)
echo.

rem ---------- 3. 下载 Chromium 内核 ----------
echo [3/3] 检查 Chromium 内核（约 150MB，只需一次）
rem 注意：不能用 `playwright install --dry-run` 判断是否已装 —— 它无论如何都返回 0。
rem 这里直接问 playwright 要可执行文件路径，再看文件在不在。
%PY% -c "import os,sys;from playwright.sync_api import sync_playwright as sp;p=sp().start();e=p.chromium.executable_path;p.stop();sys.exit(0 if os.path.exists(e) else 1)" >nul 2>nul
if %errorlevel%==0 (
  echo       内核已就绪，跳过下载。
  goto :DONE
)
echo       正在下载...
%PY% -m playwright install chromium
if errorlevel 1 goto :MIRROR_TRY
goto :DONE

:MIRROR_TRY
echo.
echo   默认下载源失败，改用国内镜像重试...
set "PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright"
%PY% -m playwright install chromium
if errorlevel 1 goto :NOKERNEL

:DONE
echo.
echo ============================================================
echo   安装完成
echo ============================================================
echo.
echo   下一步：
echo     run.bat login                       先登录一次（扫码或账号密码）
echo     run.bat doctor                      环境自检
echo     run.bat courses                     看看有哪些课
echo     run.bat homework -c 课程名           看看这门课有哪些作业
echo     run.bat answer -c 课程名 -w 作业名     抓题 - 出答案 - 存盘（不提交）
echo.
pause
exit /b 0

:NOPY
echo.
echo   [错误] 没有找到 Python。
echo.
echo   请先装 Python 3.9 以上版本：https://www.python.org/downloads/
echo   安装时务必勾选 "Add python.exe to PATH"，装完重开一个窗口再跑本脚本。
echo.
pause
exit /b 1

:NOPIP
echo.
echo   [错误] playwright 安装失败。
echo.
echo   可以手动重试（换个源）：
echo     %PY% -m pip install playwright -i %MIRROR%
echo.
pause
exit /b 1

:NOKERNEL
echo.
echo   [错误] Chromium 内核下载失败。
echo.
echo   可以手动重试：
echo     set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
echo     %PY% -m playwright install chromium
echo.
pause
exit /b 1
