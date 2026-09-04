"""准备本地演示账号和订单数据。

用法：python scripts/seed_demo_data.py
重复执行安全，只会补充缺少的固定订单号。
"""
from pathlib import Path
import sys

# 允许从项目根目录直接执行此脚本。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, db
from app.config import setup_logging


if __name__ == "__main__":
    setup_logging()
    db.init_db()
    auth.seed_users()
    db.seed_orders()
    db.seed_profile_facts()
    print("演示账号、订单和画像数据已准备完成：user / demo_user，密码均为 123456")
