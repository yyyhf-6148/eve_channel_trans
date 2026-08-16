@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import requests" 2>nul
if errorlevel 1 (
    echo 正在通过国内镜像源安装依赖 requests ...
    python -m pip install --timeout 120 -i https://pypi.tuna.tsinghua.edu.cn/simple requests
)
start "" pythonw main.py
