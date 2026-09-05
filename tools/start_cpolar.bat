@echo off
chcp 65001 >nul
title AI客服系统 - cpolar 隧道（固定域名，推荐）
rem 本机专用脚本：需要先安装 cpolar，并设置 CPOLAR_EXE 或加入 PATH。
set "CPOLAR_EXE=%CPOLAR_EXE%"
if "%CPOLAR_EXE%"=="" set "CPOLAR_EXE=cpolar"
echo ================================================
echo   AI 客服系统 - cpolar 公网隧道（推荐）
echo ================================================
echo.
echo   【首次使用，只需做一次】
echo     1. 浏览器打开  https://dashboard.cpolar.com/auth
echo        用手机号/邮箱注册一个免费账号（1 分钟）
echo     2. 登录后，在「验证」页复制你的 Authtoken
echo     3. 回到这里，运行下面这一行（把 TOKEN 换成你的）：
echo         "%%CPOLAR_EXE%%" authtoken TOKEN
echo.
echo   【以后每次】直接双击本脚本，就能得到一个固定网址。
echo   按 Ctrl+C 关闭隧道。
echo.
"%CPOLAR_EXE%" http 8000
pause
