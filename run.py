"""一键启动脚本：python run.py

启动后访问 http://127.0.0.1:8000，自动跳转登录页。
演示账号：admin / 123456（客服后台）、user / 123456（聊天界面）。
"""
import uvicorn

from app.config import setup_logging


if __name__ == "__main__":
    setup_logging()
    # reload=False：生产/演示环境关闭热重载；开发时也可改成 True
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
