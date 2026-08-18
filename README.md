# AI 客服工单自动化系统

一个基于大模型的电商客服工单自动化系统，以**聊天式 AI 客服**的形式处理客户问题：**登录 → 对话 → 工单自动分类 → 条件路由 → Agent 工具回复 / RAG 知识库回复 / 人工升级**。

系统能把重复性咨询交给 AI 自动处理，把退款纠纷、复杂投诉等需要人工介入的问题自动升级给客服后台跟进，从而提升客服效率。

> 技术栈：Python + FastAPI + DeepSeek API + MySQL + ChromaDB（RAG 检索增强）

## ✨ 功能特性

- **登录鉴权**：账号密码登录 + Bearer Token 会话，区分「客户」和「客服」两种角色
- **聊天式 AI 客服**：客户在聊天界面提问，AI 像真人客服一样自动回复
- **7 类工单自动分类**：物流查询、退货申请、商品咨询、售后维修、发票问题、退款纠纷、人工处理工单
  - 采用 **Few-shot + 思维链（Chain-of-Thought）** 提示词，同一次调用输出「分类 + 情绪 + 多诉求」
- **情绪识别与安抚**：识别负面/中性/正面情绪，极端负面（辱骂/强烈愤怒/持续讽刺/扬言投诉）强制转人工先安抚
- **条件路由（核心）**：根据「分类 + 置信度 + 情绪 + 多诉求」决定自动处理还是转人工
  - 投诉/纠纷意图、极端负面情绪、多诉求混杂 → 转人工
  - 置信度 < 0.85 → 转人工
  - 退款纠纷 / 人工处理工单 → 转人工
  - 其余可自动类别（物流/商品/发票/退货/售后）→ **Agent 调工具 + RAG 知识库**回复
- **RAG 防幻觉**：知识库检索 + 大模型生成，提示词强制「只依据知识库回答，不编造」
- **人工升级**：转人工的工单进入客服后台，客服可「标记已处理」，形成闭环
- **效果可量化**：自动处理率、分类准确率、平均耗时、情绪分布、满意度一目了然
- **深空玻璃拟态主题界面**：登录页 + 聊天界面 + 客服后台三套页面
- **日志系统**：运行日志写入 `logs/` 文件夹（不记录用户隐私信息）

## 🏗️ 系统架构

```
客户登录 ──▶ 聊天式 AI 客服
                 │
                 ▼
        ┌────────────────┐
        │  分类器          │  大模型分类（7 类 + 情绪 + 多诉求）
        │  输出类别+置信度  │
        └───────┬────────┘
                │
                ▼
        ┌────────────────┐     投诉/极端情绪/多诉求 或
        │  路由 Router     │──▶  置信度<0.85/退款纠纷 ──▶ 转人工（客服后台）
        └───────┬────────┘
                │ 可自动处理
                ▼
   Agent 工具调用 + RAG 知识库
  (物流/商品/发票/退货/售后)
                │
                ▼
        ┌────────────────┐
        │ MySQL 落库 + 日志 │
        └────────────────┘
```

## 📁 项目结构

```
ai_ticket_system/
├── app/                    # 核心业务代码
│   ├── main.py             # Web 入口（登录/聊天/后台 + API）
│   ├── config.py           # 配置读取 + 日志初始化
│   ├── auth.py             # 登录鉴权、会话管理、密码哈希
│   ├── categories.py       # 7 类工单定义 + 路由规则
│   ├── llm.py              # DeepSeek 大模型调用封装
│   ├── classifier.py       # 工单自动分类
│   ├── router.py           # 条件路由（核心业务流程）
│   ├── responder.py        # 闲聊回复（问候 / 寒暄 / 自我介绍）
│   ├── rag.py              # ChromaDB 知识库检索（bge 语义向量 + 哈希兜底）
│   ├── db.py               # MySQL 数据存储（工单 + 用户）
│   ├── schemas.py          # 接口数据模型
│   └── metrics.py          # 效果指标统计
├── prompts/                # 提示词（分类 / 回复 / 统计 / 复盘）
├── templates/              # 前端页面
│   ├── login.html          # 登录页
│   ├── index.html          # 聊天式客服界面
│   └── admin.html          # 客服后台
├── static/                 # 前端样式与脚本（深空玻璃拟态主题）
│   ├── style.css
│   ├── login.js / app.js / admin.js
├── data/                   # 数据文件
│   ├── faq.md              # FAQ 知识库（RAG 种子数据，23 条）
│   └── chroma/             # 向量数据库（运行生成，已 gitignore）
├── scripts/                # 独立脚本
│   ├── seed_faq.py         # 初始化知识库
│   ├── gen_tickets.py      # 生成模拟工单
│   ├── run_demo.py         # 端到端演示
│   ├── analyze.py          # 批量统计
│   └── review_errors.py    # 错例复盘
├── logs/                   # 系统日志（运行生成，已 gitignore）
├── requirements.txt        # 依赖清单
├── run.py                  # 一键启动
├── .env.example            # 配置示例
└── README.md
```

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入 DeepSeek API 密钥和 MySQL 连接信息：

```bash
# Windows
copy .env.example .env
# Linux / macOS
cp .env.example .env
```

编辑 `.env`：

```ini
# DeepSeek 大模型
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com

# MySQL 数据库（需先创建好 MySQL 服务）
MYSQL_HOST=localhost
MYSQL_PORT=3308
MYSQL_USER=root
MYSQL_PASSWORD=你的数据库密码
MYSQL_DB=ai_ticket

# 分类置信度阈值
CONFIDENCE_THRESHOLD=0.85
```

> 密钥申请：https://platform.deepseek.com
> 数据库会由程序自动创建（`ai_ticket` 库 + `tickets`/`users` 表）。

### 3. 初始化知识库（RAG）

```bash
python scripts/seed_faq.py
```

> 使用中文语义向量模型 **BAAI/bge-small-zh-v1.5**（首次运行自动下载，走 hf-mirror 镜像，国内友好）；模型不可用时自动回退本地哈希向量化兜底，保证服务不中断。

### 4. 启动系统

```bash
python run.py
```

打开浏览器访问 **http://127.0.0.1:8000**，自动跳转到登录页。

## 🔐 演示账号

首次启动会自动创建两个演示账号（密码均为 `123456`）：

| 账号 | 密码 | 角色 | 用途 |
|------|------|------|------|
| `user` | `123456` | 客户 | 登录聊天界面，体验 AI 客服 |
| `admin` | `123456` | 客服 | 登录客服后台，查看/处理升级工单 |

## 🧪 端到端演示（可选）

生成模拟工单并跑通完整流程，输出核心指标：

```bash
python scripts/gen_tickets.py   # 生成模拟工单
python scripts/run_demo.py      # 跑 20 张，打印分类准确率 / 自动处理率 / 平均耗时
```

## 📡 API 接口

| 方法 | 路径 | 是否需要登录 | 说明 |
|------|------|------------|------|
| POST | `/api/login` | 否 | 登录，返回 token |
| POST | `/api/logout` | 是 | 登出 |
| GET  | `/api/me` | 是 | 当前用户信息 |
| POST | `/api/tickets` | 是 | 发送客服消息 / 提交工单 |
| GET  | `/api/tickets` | 是 | 工单列表 |
| GET  | `/api/tickets/{id}` | 是 | 工单详情 |
| GET  | `/api/escalations` | 是（仅 admin） | 升级工单列表 |
| POST | `/api/escalations/{id}/resolve` | 是（仅 admin） | 标记已处理 |
| GET  | `/api/metrics` | 是 | 核心指标 |
| GET  | `/health` | 否 | 健康检查 |

### 登录 + 提交工单示例

```bash
# 1. 登录获取 token
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"user","password":"123456"}' | python -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 2. 发送一条客服消息
curl -X POST http://127.0.0.1:8000/api/tickets \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"ticket_text": "我的快递三天没更新物流了，帮我查查"}'
```

## 📊 指标说明

| 指标 | 计算方式 | 说明 |
|------|----------|------|
| 分类准确率 | 分类正确数 / 有真实标签工单数 | `scripts/run_demo.py` 跑 20 条模拟工单后打印 |
| 自动处理率 | 自动处理工单数 / 工单总数 | 同上，随模拟数据分布变化 |
| 平均耗时 | 平均每张工单处理耗时 | RAG 命中毫秒级；分类 + 转人工约 1 秒 |

## 🧠 设计说明（答辩重点）

- **为什么用 MySQL 而不是 SQLite**：工单系统需要持久化、多用户并发读写，MySQL 更适合生产环境
- **为什么用 bge-small-zh 语义向量 + 哈希兜底**：chromadb 默认的 MiniLM 模型需要从国外下载、国内网络常超时；改用中文语义模型 bge-small-zh（走 hf-mirror 镜像）保证同义改写也能检索到，同时保留本地哈希向量化作为离线兜底，网络异常也不中断服务
- **RAG 如何防幻觉**：回复提示词强制「只依据检索到的知识库内容回答，知识库没有的就说没有」，避免模型编造
- **为什么转人工**：退款纠纷、投诉等涉及资金/情绪的工单，AI 直接处理有风险，必须转人工——这是「AI 客服 + 人工兜底」的关键设计

## 📄 License

[MIT](LICENSE)
