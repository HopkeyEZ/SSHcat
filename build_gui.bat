@echo off
chcp 65001 >nul
title SSH Terminal GUI 打包工具
echo ============================================
echo   SSH Terminal GUI - 打包为独立可执行程序
echo ============================================
echo.

cd /d "%~dp0"

echo [1/3] 检查依赖...
python -c "import PySide6, paramiko, pyte, PyInstaller" 2>nul
if errorlevel 1 (
    echo 正在安装缺失依赖...
    pip install PySide6 paramiko pyte pyinstaller
)

echo [2/3] 开始打包 (可能需要几分钟)...
echo.

pyinstaller --noconfirm --clean ^
    --name "SSH Terminal" ^
    --windowed ^
    --onedir ^
    --add-data "icon.ico;." ^
    --hidden-import paramiko ^
    --hidden-import pyte ^
    --hidden-import pyte.screens ^
    --hidden-import pyte.streams ^
    --hidden-import cffi ^
    --hidden-import nacl ^
    --hidden-import bcrypt ^
    --collect-all paramiko ^
    --collect-all pyte ^
    ssh_terminal.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败！
    pause
    exit /b 1
)

echo.
echo [3/3] 打包完成！
echo.
echo 输出目录: %cd%\dist\SSH Terminal\
echo 可执行文件: %cd%\dist\SSH Terminal\SSH Terminal.exe
echo.
echo 你可以将 "dist\SSH Terminal" 整个文件夹复制到任意位置运行。
echo.
pause
