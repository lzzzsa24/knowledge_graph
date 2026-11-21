@echo off
setlocal
chcp 65001 >nul
title 混合键合知识库
pushd "%~dp0" || exit /b 1
echo 正在启动混合键合知识库……
echo 启动成功后，在浏览器打开：
echo http://127.0.0.1:8600/#/ask
echo.
echo 使用期间请保持此窗口打开。
echo 关闭系统：按 Ctrl+C；若询问是否终止批处理，输入 Y。
echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_web.ps1"
set "webExit=%errorlevel%"
popd
if not "%webExit%"=="0" (
    echo.
    echo 服务已退出，退出码：%webExit%。请保留上方错误信息以便排查。
    pause
)
exit /b %webExit%
