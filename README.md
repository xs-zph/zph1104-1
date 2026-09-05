# AI 客服工单自动化系统

一个基于大模型的电商客服工单自动化系统，以**聊天式 AI 客服**的形式处理客户问题：**登录 → 对话 → 工单自动分类 → 条件路由 → Agent 工具回复 / RAG 知识库回复 / 人工升级**。

系统能把重复性咨询交给 AI 自动处理，把退款纠纷、复杂投诉等需要人工介入的问题自动升级给客服工作台跟进，从而提升客服效率。

> 技术栈：Python + FastAPI + DeepSeek API + MySQL + ChromaDB（RAG 检索增强）+ MCP

## ✨ 功能特性

- **登录鉴权**：账号密码登录 + 后端 HttpOnly Cookie 会话，区分「系统管理员」「经理」「客服」「客户」四种角色
- **聊天式 AI 客服**：客户在聊天界面提问，AI 像真人客服一样自动回复
- **7 类工单自动分类**：物流查询、退货申请、商品咨询、售后维修、发票问题、退款纠纷、人工处理工单
  - 采用 **Few-shot + 思维链（Chain-of-Thought）** 提示词，同一次调用输出「分类 + 情绪 + 多诉求」
- **情绪识别与安抚**：识别负面/中性/正面情绪，极端负面（辱骂/强烈愤怒/持续讽刺/扬言投诉）强制转人工先安抚
- **条件路由（核心）**：根据「分类 + 置信度 + 情绪 + 多诉求」决定自动处理还是转人工
  - 投诉/纠纷意图、极端负面情绪、多诉求混杂 → 转人工
  - 置信度 < 0.85 → 转人工
  - 退款纠纷 / 人工处理工单 → 转人工
  - 其余可自动类别（物流/商品/发票/退货/售后）→ **场景化 Skill 召回后，Agent 调工具 + RAG 知识库**回复
- **RAG 防幻觉**：知识库检索 + 大模型生成，提示词强制「只依据知识库回答，不编造」
- **人工升级**：转人工的工单进入客服工作台，客服可接单、回复、等待客户、转派和结束，形成闭环
- **分权后台**：系统管理员只维护账号、密码、角色、启停用状态和客服操作权限；经理独立查看人工服务效果仪表盘
- **坐席协作**：人工工单支持原子接单、处理中 / 等待客户 / 已解决 / 已关闭状态和 SLA 到期提示
- **效果可量化**：自动处理率、分类准确率、平均耗时、情绪分布、满意度一目了然
- **工作台界面**：登录页 + 客户聊天页 + 客服工作台 + 管理员后台 + 经理仪表盘五套独立页面
- **日志系统**：运行日志写入 `logs/` 文件夹（不记录用户隐私信息）
- **场景化 Skills**：按场景、触发词和风险先召回 Top 3 工具，再交给 Transformer Agent 理解和决策，避免工具增加后全量暴露造成误召回
- **MCP 业务工具**：订单、物流、退款查询通过本地 stdio MCP Server 动态发现和调用；MCP 不可用时自动回退到内置工具
- **中心式多 Agent**：`main_agent` 统一调度、监管和裁决专业子 Agent；子 Agent 不能互相通信，只能通过结构化 JSON 向主 Agent 汇报
- **共享黑板**：主 Agent 写入工单级审计黑板，所有子 Agent 按任务读取，支持协作上下文留痕和结果重放
- **多 Agent 运行审计**：系统管理员可在后台查看脱敏后的任务摘要、子 Agent、状态和 JSON 事件轨迹
- **大模型并发治理**：统一使用有界任务队列和固定 worker，支持请求超时、瞬时错误重试、熔断保护；队列过载或上游不可用时自动转人工

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
   场景化 Skill 召回（Top 3）
                │
                ▼
   Transformer Agent 工具调用 + RAG 知识库
  (物流/商品/发票/退货/售后)
                │
                ▼
        中心式多 Agent 调度
        main_agent ──JSON──▶ 专业子 Agent
        (唯一写黑板)        (只读黑板，禁止互调)
                │
                ▼
                ├── MCP Client Adapter ──▶ business_mcp（订单/物流/退款只读工具）
                │                              │
                │                              └── MySQL（按当前用户归属过滤）
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
│   ├── mcp_client.py       # MCP 工具发现、schema 转换、调用和降级
│   ├── multi_agent.py       # 中心式多 Agent、JSON 协议和共享黑板
│   ├── schemas.py          # 接口数据模型
│   └── metrics.py          # 效果指标统计
├── prompts/                # 提示词（分类 / 回复 / 统计 / 复盘）
├── templates/              # 前端页面
│   ├── login.html          # 登录页
│   ├── register.html       # 注册页
│   ├── forgot_password.html # 忘记密码页
│   ├── index.html          # 聊天式客服界面
│   ├── admin.html          # 系统管理员后台
│   ├── manager.html        # 经理运营仪表盘
│   └── staff.html          # 客服工作台
├── static/                 # 前端样式与脚本（深空玻璃拟态主题）
│   ├── style.css
│   ├── login.js / register.js / forgot_password.js / app.js / admin.js / manager.js
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
├── mcp_servers/            # 本地 MCP Server（stdio）
│   └── business_server.py  # 订单 / 物流 / 退款只读工具
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

# 可选：切换到 Ollama / vLLM 等 OpenAI 兼容服务
# LLM_MODEL=qwen2.5:7b
# LLM_BASE_URL=http://127.0.0.1:11434/v1
# LLM_API_KEY=

# MySQL 数据库（需先创建好 MySQL 服务）
MYSQL_HOST=localhost
MYSQL_PORT=3308
MYSQL_USER=root
MYSQL_PASSWORD=你的数据库密码
MYSQL_DB=ai_ticket

# 分类置信度阈值
CONFIDENCE_THRESHOLD=0.85

# MCP 只读业务工具（默认开启；失败时自动使用内置工具）
MCP_ENABLED=true
MCP_TIMEOUT_SECONDS=8
```

`LLM_*` 配置优先级高于旧版 `DEEPSEEK_*` 配置，模型调用接口保持 OpenAI
`/chat/completions` 兼容格式。开发环境可以使用本地 Ollama/vLLM，生产环境
仍可继续使用 DeepSeek；图片识别还可通过 `VISION_*` 单独指定视觉模型。

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

首次启动会自动创建演示账号（密码均为 `123456`）：

| 账号 | 密码 | 角色 | 用途 |
|------|------|------|------|
| `user` | `123456` | 客户 | 登录聊天界面，体验 AI 客服；有 4 条演示订单 |
| `demo_user` | `123456` | 客户 | 验证多用户订单隔离；有 4 条演示订单 |
| `admin` | `123456` | 系统管理员 | 管理账号、密码、角色和权限 |
| `manager` | `123456` | 经理 | 查看人工处理效果仪表盘 |
| `agent` | `123456` | 客服 | 登录客服工作台，处理转人工工单 |

首次启动会自动写入 2 个客户共 8 条幂等演示订单和 4 条实体画像事实，覆盖待发货、已发货、已完成、已退货、已取消、物流中和退款处理中等状态。已有数据库也可以手动执行：

```bash
python scripts/seed_demo_data.py
```

演示数据由 `DEMO_DATA_ENABLED=true` 控制；生产环境建议关闭，关闭后不会自动创建演示账号和订单。

### 企业部署基础配置

配置 `REDIS_URL` 后，登录会话会保存到 Redis，实时转人工事件通过 Redis Pub/Sub
跨应用进程广播；未配置时仍可使用单机内存模式。数据库默认使用有界连接池，
可通过 `DB_POOL_SIZE` 和 `DB_POOL_TIMEOUT_SECONDS` 调整。每个 HTTP 响应都会返回
`X-Request-ID`，可用来关联应用日志和前端报错。开发环境可执行
`pip install -r requirements-dev.txt`，再运行 `pytest`；现有 `unittest` 回归也可继续使用。

大模型调用默认使用 4 个 worker 和 32 个排队位。`LLM_REQUEST_TIMEOUT_SECONDS`
控制单次上游请求时限，`LLM_TOTAL_TIMEOUT_SECONDS` 控制一次任务总时限；408、429
和 5xx 等瞬时错误会按 `LLM_MAX_RETRIES` 做指数退避重试，连续失败达到
`LLM_CIRCUIT_FAILURE_THRESHOLD` 后暂时熔断。队列满、超时或熔断时，现有路由会
自动将工单转人工，不向客户返回 500。

## 🧪 端到端演示（可选）

生成模拟工单并跑通完整流程，输出核心指标：

```bash
python scripts/gen_tickets.py   # 生成模拟工单
python scripts/run_demo.py      # 跑 20 张，打印分类准确率 / 自动处理率 / 平均耗时
```

## 📡 API 接口

| 方法 | 路径 | 是否需要登录 | 说明 |
|------|------|------------|------|
| POST | `/api/login` | 否 | 登录，由服务端设置 HttpOnly 会话 Cookie |
| POST | `/api/register` | 否 | 注册普通客户账号并设置 HttpOnly 会话 Cookie |
| POST | `/api/password-reset/request` | 否 | 使用已绑定手机号申请密码重置验证码 |
| POST | `/api/password-reset` | 否 | 校验一次性验证码并修改密码 |
| POST | `/api/logout` | 是 | 登出 |
| GET  | `/api/me` | 是 | 当前用户信息 |
| GET  | `/api/profile` | 是 | 当前用户的实体画像事实卡片 |
| PUT | `/api/profile/{id}` | 是 | 手工修正并确认一条画像事实 |
| DELETE | `/api/profile/{id}` | 是 | 删除当前用户的一条画像事实 |
| POST | `/api/tickets` | 是 | 发送客服消息 / 提交工单 |
| POST | `/api/tickets/multimodal` | 是 | 上传图片并识别后提交工单 |
| GET  | `/api/tickets` | 是 | 工单列表 |
| GET  | `/api/tickets/{id}` | 是 | 工单详情 |
| GET  | `/api/escalations` | 是（仅 agent） | 升级工单列表 |
| POST | `/api/escalations/{id}/reply` | 是（仅 agent） | 发送人工回复并保持人工接管 |
| POST | `/api/escalations/{id}/resolve` | 是（仅 agent） | 标记已处理 |
| GET  | `/api/admin/agent-runs` | 是（仅 admin） | 多 Agent 运行摘要 |
| GET  | `/api/admin/agent-runs/{task_id}` | 是（仅 admin） | 单次运行的脱敏 JSON 轨迹 |
| GET  | `/api/metrics` | 是（仅 manager） | 核心指标 |
| GET  | `/health` | 否 | 健康检查 |

人工接管规则：客户工单处于 `escalated`、`in_progress` 或 `waiting_customer` 时，客户后续文字/图片会追加到原工单消息线程，不会再次调用 AI；坐席通过 SSE 的 `customer_message` 事件实时收到补充内容。人工提交回答后工单变为 `resolved`，客户下一条新问题才会重新进入 AI 流程。

注册页为 `/register`，忘记密码页为 `/forgot-password`。注册账号永远由服务端创建为 `customer`，不会接受前端传入的角色。密码重置使用已核验手机号的一次性验证码，挑战在服务端保存摘要并限制有效期、错误次数和重复消费；演示模式返回测试码，生产环境应接入短信或邮件服务。

### 登录 + 提交工单示例

```bash
# 1. 登录并保存服务端会话 Cookie
curl -c cookies.txt -s -X POST http://127.0.0.1:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"user","password":"123456"}'

# 2. 发送一条客服消息
curl -X POST http://127.0.0.1:8000/api/tickets \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"ticket_text": "我的快递三天没更新物流了，帮我查查"}'

# 3. 上传图片（需配置 VISION_MODEL）
curl -X POST http://127.0.0.1:8000/api/tickets/multimodal \
  -b cookies.txt \
  -F "text=这个商品看起来有问题，请帮我看看" \
  -F "image=@product.jpg"
```

图片识别使用兼容 OpenAI 图片消息格式的视觉模型，通过 `VISION_MODEL`、`VISION_BASE_URL` 和
`VISION_API_KEY` 配置。图片不会写入数据库；识别摘要会与用户文字一起进入工单。未配置或调用失败时，
带文字的请求继续按文字处理，纯图片请求自动转人工。

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
- **实体画像记忆**：客服回复完成后，由有界后台线程异步提取客户明确表达的设备、偏好、称呼和售后事实，写入 MySQL 事实卡片；后续 Agent 按场景召回主体事实，订单和工单则从权威业务表实时读取，避免无关偏好和过期状态污染上下文
- **持久化多轮记忆**：当前会话最近 12 轮消息保存到 MySQL，服务重启后可恢复；客户开启新对话时切换会话并清理旧上下文，数据库异常时自动回退进程内记忆
- **知识库运营**：管理员可在后台新增、编辑、停用和恢复 FAQ，保存后实时同步向量库；经理和客服没有知识库写权限
- **知识库审计**：FAQ 每次变更保存内容快照、操作者和时间，管理员可查看单条知识的历史版本；人工回复回流也会记录客服来源
- **为什么做 Skills 分层**：Transformer 负责语言理解和工具决策，`app/skills.py` 负责候选收缩；当前按规则只暴露 Top 3 Skill，后续可在这一层替换为向量召回和 reranker
- **为什么接入 MCP**：MCP 把业务工具统一成可发现、可替换的协议接口。当前只迁移只读订单域，用户身份由服务端注入并继续经过数据库归属过滤；调用失败自动回退，避免 MCP 故障影响客服主链路

### MCP 冒烟测试

安装依赖后，可以直接检查 Server 是否能启动并发现工具：

```bash
python -c "from app import mcp_client; print([t['function']['name'] for t in mcp_client.list_tools('user', {'list_my_orders', 'query_order', 'query_logistics', 'check_refund'})])"
```

预期输出包含 `list_my_orders`、`query_order`、`query_logistics`、`check_refund`。`MCP_USERNAME` 不会出现在模型工具参数中；高风险的 `cancel_order` 暂不通过 MCP 暴露。

## 📄 License

[MIT](LICENSE)
