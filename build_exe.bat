@echo off
REM ============================================================
REM  在 Windows 上把 auto_search 打包成单文件 exe
REM  前置条件: 已安装 Python 3.10+ (勾选 Add to PATH)
REM  用法: 在本目录的命令行中执行  build_exe.bat
REM ============================================================
setlocal
cd /d %~dp0

if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :err

REM --collect-all playwright: 把 playwright 的 node 驱动一并打进 exe
pyinstaller --noconfirm --clean --onefile --name auto_search --collect-all playwright main.py
if errorlevel 1 goto :err

echo.
echo ============================================================
echo  打包完成: dist\auto_search.exe
echo  使用: 把 auto_search.exe 放到任意目录, 双击打开图形界面;
echo        首次运行会在同目录生成 config.yaml 作为默认配置。
echo        纯命令行模式(多任务): auto_search.exe --cli
echo  说明: 默认调用系统自带的 Edge 浏览器 (channel: msedge),
echo        无需额外下载浏览器。
echo ============================================================
pause
exit /b 0

:err
echo.
echo 打包失败，请把上面的错误信息反馈给开发者。
pause
exit /b 1
