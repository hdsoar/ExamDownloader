@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo [1/3] 安装依赖...
python -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo [2/3] 打包 Windows EXE...
python -m PyInstaller --noconfirm --clean --windowed --onefile --name "初中免费试卷下载器" app.py
if errorlevel 1 goto fail
echo [3/3] 完成！
echo EXE 文件位置：dist\初中免费试卷下载器.exe
pause
exit /b 0
:fail
echo 打包失败。请确认已安装 Python 3.10 或更高版本，并且网络正常。
pause
exit /b 1
