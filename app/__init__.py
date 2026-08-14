"""AI 客服工单系统 —— 应用包。

这个包把系统拆成一个个职责单一的模块：
  - config.py      读取配置、初始化日志
  - categories.py  定义 7 个工单分类与路由规则
  - auth.py        登录鉴权、会话管理
  - llm.py         封装 DeepSeek 大模型调用
  - classifier.py  工单自动分类
  - router.py      三级路由（RAG 优先 → 大模型 → 转人工）
  - agent.py       工具调用 Agent（订单/物流/退款/时间/天气）
  - rag.py         知识库语义检索（ChromaDB，含 RAG 优先 best_answer）
  - responder.py   闲聊回复
  - privacy.py     敏感信息脱敏
  - memory.py      多轮对话记忆
  - daily.py       日常问答工具（时间/天气）
  - cache.py       响应缓存
  - wechat.py      微信公众号接入
  - db.py          工单数据库（MySQL）
  - metrics.py     效果指标统计
  - main.py        Web 界面 + API 入口
"""
