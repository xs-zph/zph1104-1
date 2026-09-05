@echo off
chcp 65001 >nul
title AI客服系统 - 公网隧道
rem 本机专用脚本：需要本机安装 OpenSSH，并确保 ssh 已加入 PATH。
echo ================================================
echo   AI 客服系统 - 公网隧道（免注册，临时用）
echo ================================================
echo.
echo   连接成功后，会出现一行网址，例如：
echo     https://xxxxxxxx.lhr.life
echo   把这个网址发给任何人，对方就能访问你的系统。
echo.
echo   【注意】这个网址每次重启都会变，只适合临时演示。
echo   想要固定网址，请用 start_cpolar.bat（推荐）。
echo   按 Ctrl+C 关闭隧道。
echo.
ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -R 80:localhost:8000 nokey@localhost.run
pause
