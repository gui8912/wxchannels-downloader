@echo off
rem 视频号直播 / 回放下载器 启动脚本
rem 只需要本机装了 Python 3.8+，其余依赖见 README
setlocal
cd /d "%~dp0"

rem 优先用 pythonw（无黑框窗口），找不到就退回 python
where pythonw.exe >nul 2>nul && (
    start "" pythonw.exe "%~dp0wxlive_recorder.py"
    goto :eof
)
where python.exe >nul 2>nul && (
    python "%~dp0wxlive_recorder.py"
    goto :eof
)
echo 没有找到 Python。请先安装 Python 3.8 或更高版本，安装时勾选 "Add to PATH"。
pause
endlocal
