"""AI客服工单自动化系统 —— 应用包。

这个包把系统拆成一个个职责单一的模块：
  - config.py      读取配置、初始化日志
  - categories.py  定义 7 个工单分类
  - llm.py         封装 DeepSeek 大模型调用
  - classifier.py  工单自动分类
  - router.py      根据分类结果决定处理方式（路由）
  - responder.py   生成回复（模板回复 / RAG 回复）
  - rag.py         知识库语义检索（ChromaDB）
  - db.py          工单数据库（SQLite）
  - metrics.py     效果指标统计
  - main.py        Web 界面 + API 入口
"""
