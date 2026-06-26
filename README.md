# BMC 日志分析系统

基于 Web 的 BMC 日志分析工具，支持上传 `dump_info.tar.gz` 压缩包，自动提取并解析 `app.log` 和 `framework.log`，以深色主题仪表盘展示结构化的日志条目。

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React 18 + TypeScript + Vite 6 |
| 后端 | Python 3 + FastAPI + uvicorn |
| 设计 | WattVision 深色主题规范 |

## 快速开始

### 环境要求

- **Node.js** >= 18
- **Python** >= 3.10
- **npm** >= 9

### 1. 启动后端

```bash
cd backend

# 创建虚拟环境并安装依赖（首次运行）
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 启动服务（端口 8000）
uvicorn main:app --reload --port 8000
```

### 2. 启动前端

```bash
cd frontend

# 安装依赖（首次运行）
npm install

# 启动开发服务器（端口 5173）
npm run dev
```

### 3. 使用

打开浏览器访问 **http://localhost:5173**，上传 `dump_info.tar.gz` 即可查看日志分析结果。

---

## 项目结构

```
no_error/
├── backend/
│   ├── requirements.txt          # Python 依赖
│   ├── main.py                   # FastAPI 入口（路由、CORS）
│   ├── parser.py                 # 日志解析器（双正则策略）
│   ├── extractor.py              # tar.gz 解压器
│   └── uploads/                  # 上传文件临时目录
│
├── frontend/
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts            # 含 API 代理配置
│   └── src/
│       ├── main.tsx              # 应用入口
│       ├── App.tsx               # 根组件（状态管理 + 布局）
│       ├── App.css               # 全局样式 + CSS 变量
│       ├── types/log.ts          # TypeScript 类型定义
│       ├── api/client.ts         # 后端 API 封装
│       └── components/
│           ├── UploadZone.tsx    # 拖拽上传区域
│           ├── KpiCards.tsx      # KPI 统计卡片
│           ├── FilterBar.tsx     # 筛选栏
│           ├── LogTable.tsx      # 日志表格
│           ├── LogRow.tsx        # 单行日志（可展开）
│           └── StatsPanel.tsx    # 组件错误分布图
│
├── docs/
│   └── implementation-plan.md    # 实现方案文档
│
└── dump_info.tar.gz              # 测试用日志压缩包
```

## API 接口

### `POST /api/parse`

上传并解析 tar.gz 日志压缩包。

- **请求**: `multipart/form-data`，字段名 `file`
- **响应**:

```json
{
  "success": true,
  "summary": {
    "totalLines": 16106,
    "errorCount": 2724,
    "warningCount": 520,
    "noticeCount": 12650,
    "launchCount": 183,
    "components": ["hwproxy", "pcie_device", "sensor", ...],
    "timeRange": {
      "start": "1970-01-01 00:00:21",
      "end": "2025-08-06 03:58:37"
    }
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

### `GET /api/health`

健康检查，返回 `{ "status": "ok" }`。

## 日志格式支持

| 格式 | 示例 | 占比 |
|------|------|------|
| 标准格式 | `2025-07-24 11:31:13.714532 pcie_device ERROR: pcie_card.lua(49): PCIe card ...` | ~98% |
| LAUNCH 格式 | `1970-01-01 00:00:21.666722 [:00000002] framework: LAUNCH snlua bootstrap` | ~183 行 |

## 功能特性

- 📁 **拖拽上传** — 支持拖拽和点击上传 tar.gz 压缩包
- 📊 **KPI 仪表盘** — 实时统计总行数、错误数、警告数、通知数
- 🔍 **多维筛选** — 按组件、日志级别、关键词全文搜索
- 📋 **可展开表格** — 点击行查看完整日志消息
- 📈 **错误分布图** — Top 15 组件错误数量条形图
- 🌙 **深色主题** — 遵循 WattVision 设计规范，护眼低亮度
- ⚡ **实时状态** — 后端连接状态指示器

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
