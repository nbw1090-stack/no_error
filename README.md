# BMC 日志分析系统

基于 Web 的 BMC（Baseboard Management Controller）日志分析工具。用户上传 `dump_info.tar.gz`，后端在内存中提取并解析 `app.log` / `framework.log`，前端以 WattVision 深色主题仪表盘展示结构化日志条目。在此之上叠加了一个 **ReAct Agent 对话系统**：通过工具调用对已上传日志进行检索、过滤、统计分析，并结合用户已索引的组件源码（AST）做根因诊断。

系统还包含**用户认证与全链路用户隔离**（数据集 / 会话 / 组件 / 源码索引互不可见）、**组件注册表**（SQLite）以及 **Langfuse 可观测性**（自动嵌套的 trace）。

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React 18 + TypeScript + Vite 6（react-markdown + highlight.js 渲染 Agent 回复） |
| 后端 | Python 3.10 + FastAPI + uvicorn |
| Agent | ReAct 循环 + OpenAI 兼容 LLM 适配器 + 装饰器式工具注册表 |
| 源码分析 | tree-sitter（C / C++ / Lua），AST 结果存 SQLite |
| 存储 | JSON 文件（数据集 / 会话） + SQLite（用户 / 组件 / AST 索引） |
| 可观测性 | Langfuse v4（OpenTelemetry 上下文，自动嵌套 trace） |
| 设计 | WattVision 深色主题规范 |

## 架构概览

```
┌──────────────┐   上传 tar.gz / SSE    ┌──────────────────────────────┐
│  React 前端  │ ─────────────────────▶ │         FastAPI 后端          │
│ (仪表盘+对话) │ ◀─── SSE 流式回复 ──── │  extractor → parser → 持久化   │
└──────────────┘                        │            ▲                  │
                                        │     /api/chat (ReAct Agent)   │
                                        │  ┌────────┴────────┐          │
                                        │  │ LLM 适配器(OpenAI 兼容)│     │
                                        │  │ 工具(日志 / 源码 AST) │     │
                                        │  └────────┬────────┘          │
                                        │  auth / ast / components      │
                                        └───────────┬──────────────────┘
                                          JSON 文件 │ SQLite │ AST 源码快照
                                              Langfuse trace（可选）
```

## 快速开始

### 环境要求

- **Node.js** >= 18、**npm** >= 9
- **Python** >= 3.10
- **Docker** + **Docker Compose**（仅当需要本地 Langfuse 可观测性时）

### 1. 启动后端

```bash
cd backend

# 创建虚拟环境并安装依赖（首次运行）
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 配置环境变量（首次运行，复制下面「配置说明」中的变量到 backend/.env）
#   vim .env

# 启动开发服务（端口 8000）
uvicorn main:app --reload --port 8000
```

> ⚠️ **必须使用 `backend/venv` 启动。** AST 源码分析依赖 tree-sitter，只有在该 venv 中才装得上；用其他环境启动会出现 tree-sitter 缺失报错。

后端启动后可访问 `http://localhost:8000/api/health`（返回 `{"status":"ok"}`）和 `http://localhost:8000/docs`（Swagger）。

### 2. 启动前端

```bash
cd frontend

# 安装依赖（首次运行）
npm install

# 启动开发服务器（端口 5173，自动把 /api 代理到 http://localhost:8000）
npm run dev
```

打开浏览器访问 **http://localhost:5173** 即可：注册 / 登录后，上传 `dump_info.tar.gz` 查看仪表盘，并可在对话面板与 Agent 交互。

### 3. 启动 Langfuse 可观测性（可选）

系统内置 Langfuse v4 追踪：每次 `/api/chat` 会生成一条自动嵌套的 trace（chat → react-iteration → llm-generation / tool），用于观测 LLM 调用、工具调用与 token 用量。未启用时所有追踪降级为空操作，**不影响功能**。

**A. 用 Docker Compose 拉起本地 Langfuse 全套（postgres / clickhouse / redis / minio / langfuse-web）：**

```bash
# 在仓库根目录执行（docker-compose.yml 即 Langfuse 基础设施，非应用本体）
docker compose up -d
```

启动后访问 Langfuse Web UI：**http://localhost:3001**
（`docker-compose.yml` 中 `langfuse-web` 端口映射为 `3001:3000`；其余组件仅绑定 `127.0.0.1`）。

在 Web UI 中新建组织 / 项目，拿到项目的 **Public Key**（`pk-lf-...`）与 **Secret Key**（`sk-lf-...`）。

**B. 在 `backend/.env` 中开启并指向本地 Langfuse：**

```ini
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY="pk-lf-xxxxxxxx"
LANGFUSE_SECRET_KEY="sk-lf-xxxxxxxx"
LANGFUSE_BASE_URL="http://localhost:3001"
```

> 也可以不自己部署，直接用 Langfuse 云端：把 `LANGFUSE_BASE_URL` 设为 `https://cloud.langfuse.com` 并填入云端项目的 key。

重启后端后，每次对话的 trace 会出现在 Langfuse 控制台。后端在每个请求结束时调用 `tracer.flush()` 立即上报。

### 4. Langfuse 进阶：Prompt / Scores / Datasets 管理

在「3」开启基础 trace 之后，还可启用 Langfuse 的三大管理能力。它们**全部默认降级**：未配置 / 拉取失败时自动回退本地逻辑，绝不阻断对话。下面命令都需先 `cd backend && source venv/bin/activate`。

#### Scores —— 用户点赞 / 踩

每轮 AI 回复下方有 👍/👎 按钮。点击后，后端把反馈作为 `user_feedback`（CATEGORICAL: `like` / `dislike`）score 打到**对应那条 trace** —— 每轮对话生成的 `trace_id` 会随响应 / SSE `done` 事件回传前端并挂到该条消息上；幂等键 `trace_id-user_feedback` 让"先赞后踩"覆盖而非堆叠。

- **验证**：聊天 → 点赞 → 在 Langfuse 该 trace 的 Scores 看到 `user_feedback=like`；再点踩 → 同一 trace 的 score 变为 `dislike`。
- **接口**：`POST /api/feedback`，body `{ trace_id, session_id, feedback: "like"|"dislike", comment? }`（需登录；`trace_id` 必须属于当前用户的会话，否则 404）。

#### Prompt Management —— 在线改 system prompt，不发版

「数据分析模式」（已上传日志）的 system prompt 托管在 Langfuse（prompt 名 `bmc-system`，默认取 `production` 标签）。改 prompt 无需重新部署；拉取失败自动回退本地 `build_system_prompt`。

**方式 A：一键上传初始版本（推荐）**

```bash
python -m eval.seed_prompt
```

脚本把内置的数据分析模式模板（`{{var}}` 双花括号）上传为 `bmc-system` 的初始版本并打 `production` 标签。之后在 Langfuse UI 编辑该 prompt 即可热更新。

**方式 B：在 Langfuse UI 手动创建**

Prompts → New Prompt，text 类型：
- **Name**：`bmc-system`（或你在 `LANGFUSE_PROMPT_NAME` 设的值）
- **模板**用 `{{var}}` 双花括号变量，可用变量见 `backend/agent/core.py` 的 `_prompt_vars`：`{{total_lines}}` `{{error_count}}` `{{warning_count}}` `{{notice_count}}` `{{launch_count}}` `{{component_count}}` `{{components}}` `{{time_start}}` `{{time_end}}` `{{indexed_components}}`
- 给当前版本打 `production` 标签。

**验证 / 回退**：在 Langfuse 改一句 prompt → 开新一轮对话确认用上新版（trace 关联的 prompt version 更新）；删掉该 prompt 或取消 `production` 标签 → 确认对话自动回退本地版本、不中断。

> 仅「数据分析模式」走 Langfuse 托管；无日志的纯问答 / 源码问答模式仍用本地 prompt。

#### Datasets —— 回归评测（黄金集 + 一键回放）

攒一批 `(问题 → 期望根因)` 黄金集，改 prompt / 换模型后一键回放对比分数。

**1. 准备本地数据集**：先在前端上传一份 `dump_info.tar.gz`，记下 `dataset_id`（如 `d5ff544231bf`，对应 `backend/data/datasets/<id>.json`）。

**2. 在 Langfuse 建 dataset**：UI → Datasets → New Dataset（如 `bmc-diagnosis-golden`），为每个 case 加一个 item：
- `input` = `{ "question": "<要问 agent 的问题>", "dataset_id": "<上一步的本地 dataset_id>" }`
- `expected_output` = `<期望命中的根因关键词>`（用于确定性打分）

（也可从已有高质量 trace 一键「Add to dataset」收集。）

**3. 跑评测**：

```bash
python -m eval.run_eval --dataset bmc-diagnosis-golden --run-name prompt-v2
```

脚本会：拉取 dataset items → 逐条跑 ReAct agent → 用 `diagnosis_acc`（期望关键词是否命中）打分 → 把分数上报回 Langfuse。

**4. 对比**：在 Langfuse Datasets → Runs 并排看不同 run 的分数。改 prompt 前后各跑一次即可回归。

> 脚本是骨架：dataset item 的 `input` / `expected_output` schema 与 evaluator 逻辑（`backend/eval/run_eval.py`）都可按实际回归需求改。

## 配置说明

所有配置通过环境变量注入，`backend/.env`（gitignored）为本地默认来源。优先级：**环境变量 > `.env` > 默认值**。加载由 `backend/config.py` 内的手写 loader 完成（无 `python-dotenv` 依赖）。

在 `backend/.env` 中写入：

```ini
# ---- LLM（任意 OpenAI 兼容接口）----
LLM_PROVIDER=openai          # openai / ollama / custom（均走 OpenAI 兼容适配器）
LLM_MODEL=gpt-4o
LLM_API_KEY=sk-xxxxxxxx
LLM_API_BASE=                # 留空=官方；本地 ollama: http://localhost:11434/v1
LLM_TEMPERATURE=0.3
LLM_MAX_TOKENS=4096

# ---- Agent 循环 ----
AGENT_MAX_ITERATIONS=10        # ReAct 最大迭代轮数
AGENT_MAX_HISTORY=40           # 保留的对话历史消息条数（防御性上限）
AGENT_CONTEXT_TOKEN_BUDGET=24000  # 历史 token 软预算：超过后省略最旧工具结果
                                  # （压制 ReAct 重发历史的二次方膨胀；0=禁用）
AGENT_RECENT_TOOLS_KEEP=3      # 省略时保留多少条最近工具结果不动

# ---- Langfuse 可观测性（可选）----
LANGFUSE_ENABLED=false
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_PROMPT_NAME=bmc-system      # 托管「数据分析模式」system prompt 的 prompt 名
LANGFUSE_PROMPT_LABEL=production     # 拉取的标签（拉取失败自动回退本地 prompt）
```

> **降级行为：** 若 `LLM_API_KEY` 未配置，后端启动时 `llm` / `agent` 为 `None`，`/api/chat*` 自动回退到规则匹配回复（`_generate_reply`），系统仍可正常解析日志、展示仪表盘。若 `LANGFUSE_ENABLED != true`，追踪全部降级为空操作。

## 项目结构

```
no_error/
├── backend/
│   ├── main.py                   # FastAPI 入口（路由、CORS、Agent 单例初始化、降级）
│   ├── config.py                 # 配置加载（.env + 环境变量 → dataclass）
│   ├── parser.py                 # 日志解析器（双正则策略）
│   ├── extractor.py              # tar.gz 解压器（内存中提取，不落临时文件）
│   ├── db.py                     # 组件注册表（SQLite）
│   ├── agent/                    # ReAct Agent 核心
│   │   ├── core.py               #   ReAct 循环（run / run_stream）
│   │   ├── session.py            #   会话持久化（每会话一个 JSON）
│   │   ├── dataset.py            #   日志数据集类型化封装
│   │   ├── context.py            #   消息构建 + 历史裁剪
│   │   ├── llm/                  #   OpenAI 兼容适配器 + 工厂
│   │   ├── prompts/              #   模块化提示词（system.py 动态注入数据/源码上下文）
│   │   └── tools/                #   工具注册表 + 日志工具 + 源码工具
│   ├── auth/                     # 用户认证（注册/登录/令牌，SQLite，前缀 /api/auth）
│   ├── ast_analysis/             # tree-sitter 源码 AST 分析（前缀 /api/ast）
│   ├── observability/            # Langfuse v4 追踪（OTEL 自动嵌套）
│   ├── eval/                     # Langfuse 评测脚本（run_eval 回归 / seed_prompt 上传初始 prompt）
│   └── data/                     # 运行时数据（datasets/sessions JSON + *.db + source 快照）
│
├── frontend/
│   ├── vite.config.ts            # 含 /api → :8000 代理
│   └── src/
│       ├── App.tsx               # 全局状态 + 布局
│       ├── api/client.ts         # 统一 API 封装（含 SSE readSSE）
│       ├── types/log.ts          # 跨端类型契约（与 parser 输出对齐）
│       └── components/           # UploadZone / KpiCards / FilterBar / LogTable /
│                                 #   StatsPanel / ChatPanel / SessionSidebar / ComponentsPanel
│
├── docker-compose.yml            # Langfuse 基础设施栈（非应用本体）
├── CLAUDE.md                     # Claude Code 工作指引
├── docs/
│   ├── implementation-plan.md    # 实现方案文档
│   └── agent-execution-flow.md   # Agent 执行流程详解
└── dump_info.tar.gz              # 测试用日志压缩包
```

## API 接口

除 `/api/health` 外，业务接口均需登录（Bearer Token，由 `/api/auth/login` 返回）。

### 解析与日志查询

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/parse` | 上传 tar.gz 并同步解析（multipart `file`） |
| `POST` | `/api/parse/stream` | 上传并**流式**解析（SSE，`summary` 事件在持久化完成后才发出） |
| `GET`  | `/api/datasets/{id}` | 取数据集摘要 |
| `GET`  | `/api/datasets/{id}/entries` | 分页获取日志条目（支持筛选，供无限滚动） |

### Agent 对话

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/chat` | ReAct Agent 对话（同步返回最终回复；LLM 不可用时降级规则匹配） |
| `POST` | `/api/chat/stream` | Agent 对话（**SSE**：实时推送工具调用进度 + 逐词回复，可用 AbortSignal 中断） |
| `POST` | `/api/feedback` | 对一轮回复点赞 / 踩，打 Langfuse `user_feedback` score（按 `trace_id` 关联） |

请求体：`{ "message": "...", "session_id": "..." }`。会话 `dataset_id` 为空时进入**纯对话模式**（通用 BMC 问答，不暴露任何工具）。

### 会话管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST`   | `/api/sessions` | 创建会话（`dataset_id` 可空） |
| `PUT`    | `/api/sessions/{id}/dataset` | 给已有会话绑定数据集（先对话后上传） |
| `GET`    | `/api/sessions` | 列出当前用户的会话（按更新时间降序，仅元数据） |
| `GET`    | `/api/sessions/{id}` | 取单个会话（含完整消息历史，用于会话恢复） |
| `DELETE` | `/api/sessions/{id}` | 删除会话 |

### 组件注册表 / 源码分析 / 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET/POST/PUT/DELETE` | `/api/components` | 组件 CRUD（组件名 / git_url / branch） |
| `POST`   | `/api/ast/analyze` | 触发某组件的源码 AST 分析（克隆 → tree-sitter 解析 → 入库） |
| `DELETE` | `/api/ast/components/{name}` | 删除某组件的源码索引 |
| `GET`    | `/api/ast/result` | 查询分析结果 |
| `POST` | `/api/auth/register` · `/login` | 注册 / 登录 |
| `GET`  | `/api/auth/me` | 当前用户信息 |
| `POST` | `/api/auth/logout` | 登出 |

`POST /api/parse` 同步响应示例：

```json
{
  "success": true,
  "summary": {
    "totalLines": 16106,
    "errorCount": 2724,
    "warningCount": 520,
    "noticeCount": 12650,
    "launchCount": 183,
    "components": ["hwproxy", "pcie_device", "sensor", "..."],
    "timeRange": { "start": "1970-01-01 00:00:21", "end": "2025-08-06 03:58:37" }
  },
  "entries": [
    {
      "id": 1,
      "timestamp": "2025-07-24 11:31:13.714530",
      "component": "pcie_device",
      "level": "ERROR",
      "file": "pcie_card.lua",
      "line": 49,
      "message": "PCIe card oob management init failed.",
      "source": "app.log"
    }
  ],
  "errors": []
}
```

## 日志格式支持

| 格式 | 示例 | 占比 |
|------|------|------|
| 标准格式 | `2025-07-24 11:31:13.714532 pcie_device ERROR: pcie_card.lua(49): PCIe card ...` | ~98% |
| LAUNCH 格式 | `1970-01-01 00:00:21.666722 [:00000002] framework: LAUNCH snlua bootstrap` | ~183 行 |

## 功能特性

**仪表盘**
- 📁 **拖拽上传** — 支持拖拽 / 点击上传 tar.gz，流式解析进度
- 📊 **KPI 仪表盘** — 总行数 / ERROR / WARNING / NOTICE / LAUNCH 实时统计
- 🔍 **多维筛选** — 按组件、级别、关键词全文搜索，分页按需加载
- 📋 **可展开表格** — 点击行查看完整日志消息
- 📈 **错误分布图** — Top 组件错误数量条形图

**ReAct Agent 对话**
- 🤖 **工具驱动的日志问答** — Agent 自主调用检索 / 过滤 / 统计 / 时间线 / 上下文工具回答问题
- 🧩 **源码级根因诊断** — 当组件已构建 AST 索引时，Agent 优先用 `gather_code_context` 拉取函数体 + 兄弟符号 + 跨文件调用点，基于 `file:line` 证据给出根因
- 💬 **SSE 流式输出** — 逐词渲染回复 + 实时工具调用进度，支持中途打断
- 🧠 **多轮上下文 / 会话恢复** — 完整消息历史（含工具调用与结果）持久化，刷新不丢上下文
- 🔓 **纯对话模式** — 未上传日志时可做通用 BMC 问答（不暴露任何工具）

**平台能力**
- 👤 **用户认证与隔离** — 数据集 / 会话 / 组件 / 源码索引全部按用户隔离，互不可见
- 🧱 **组件注册表** — 管理待分析的组件源码仓库（SQLite）
- 🌲 **AST 源码分析** — tree-sitter 解析 C / C++ / Lua，按行号反查符号
- 📈 **Langfuse 可观测性与评测** — 自动嵌套 trace；支持在线 Prompt 管理、用户点赞/踩打分（Scores）、黄金集回归评测（Datasets）
- 🌙 **WattVision 深色主题**

## 设计规范

基于 WattVision DESIGN.md 深色主题：

| 用途 | 色值 | 说明 |
|------|------|------|
| 页面背景 | `#121212` | 深色模式 |
| 卡片背景 | `#1E1E1E` | 次级容器 |
| 数据强调 | `#00E5FF` | 青色 KPI 数字 |
| 错误告警 | `#FF453A` | 红色高亮 |
| 正常状态 | `#32D74B` | 绿色指示 |
| 字体 | Inter + JetBrains Mono | 正文 + 等宽数字 |

## 验证数据

使用项目根目录下的 `dump_info.tar.gz` 测试：

| 指标 | 预期值 | 实际值 |
|------|--------|--------|
| 总行数 | ~16,106 | 16,106 ✅ |
| ERROR | ~2,726 | 2,724 |
| WARNING | ~520 | 520 ✅ |
| NOTICE | ~12,652 | 12,650 |
| LAUNCH | ~183 | 183 ✅ |
| 组件数 | - | 59 |
