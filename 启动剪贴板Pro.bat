@echo off
chcp 65001 >nul
title ClipboardPro Launcher

rem ==========================================================
rem  ClipboardPro launcher
rem
rem  【改这个文件之前必读】
rem  1. 换行符必须是 CRLF，不能是 LF。
rem     cmd.exe 不认孤立的 LF(0x0A) 行尾；文件里只要含中文这类多字节
rem     字符，cmd 就会把下一行粘到上一行上，结果是 python 启动命令被
rem     吞掉 —— 表现为"双击后黑窗口一闪就没了，程序没起来"。
rem     本项目已经踩过一次，详见 文档/测试报告.md 缺陷 9。
rem     自检命令：python tests\check_launchers.py
rem  2. 中文提示只放在 echo / rem 里，不要写成裸命令。
rem  3. 程序必须"脱离控制台"启动（由 启动剪贴板Pro.py 负责），
rem     这样关掉本窗口后托盘图标与全局快捷键仍然有效。
rem  （以上注释保持纯 ASCII，避免受编码影响。）
rem ==========================================================

cd /d "%~dp0"

echo ============================================
echo   ClipboardPro - 单机剪贴板管理器
echo ============================================
echo.

rem ---- 1. check python ----
where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON
for /f "delims=" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [1/3] 已找到 %PYVER%

rem ---- 2. check dependencies ----
python -c "import PIL, win32clipboard" >nul 2>nul
if errorlevel 1 goto INSTALL_DEPS
echo [2/3] 依赖库检查通过
goto RUN

:INSTALL_DEPS
echo [2/3] 缺少依赖库，正在安装 pillow 与 pywin32 ...
python -m pip install pillow pywin32
if errorlevel 1 goto DEPS_FAILED
echo       依赖安装完成

:RUN
echo [3/3] 正在启动（后台驻留，不占用本窗口）...
echo.
python "启动剪贴板Pro.py" %*
set EXITCODE=%errorlevel%
echo.
if "%EXITCODE%"=="0" goto DONE_OK
echo [启动脚本返回 %EXITCODE%] 请查看上面的提示。
echo 日志位置： %~dp0.data\clipboardpro.log
echo.
pause
exit /b %EXITCODE%

:DONE_OK
echo 本窗口即将自动关闭，程序已在后台运行。
echo.
echo   唤出面板： Ctrl+Shift+V
echo   托盘图标： 右键可暂停监听、清空历史或退出
echo   退出程序： 托盘图标右键 - 退出
echo.
echo 面板带标题栏，可拖动移动、可最小化；点关闭只是收起面板，
echo 程序仍在托盘运行，快捷键随时可以再唤出来。
echo.
rem 等待几秒让用户看清提示。这里用 ping 而不是 timeout：
rem timeout 在"输入被重定向"的环境下会直接报错退出。
ping -n 5 127.0.0.1 >nul
exit /b 0

:NO_PYTHON
echo [错误] 没有找到 python 命令。
echo.
echo   请先安装 Python 3.10 或更高版本：
echo     https://www.python.org/downloads/windows/
echo   安装时务必勾选 "Add python.exe to PATH"
echo.
pause
exit /b 1

:DEPS_FAILED
echo.
echo [错误] 依赖安装失败。请打开命令提示符手动执行：
echo     python -m pip install pillow pywin32
echo.
pause
exit /b 1
