# 日志分析系统 - 实现方案

## 背景

构建一个基于 Web 的 BMC 日志分析工具（方案 B：React 前端 + Python 后端）。用户上传 `dump_info.tar.gz` 压缩包，系统自动提取并解析 `app.log` 和 `framework.log`，以深色主题仪表盘（遵循 WattVision DESIGN.md 设计规范）展示结构化的日志条目。

---

## 日志格式分析

```
YYYY-MM-DD HH:MM:SS.microseconds 组件名 日志级别: 源文件(行号): 消息内容
```

**两种格式变体：**

| 变体 | 示例 | 覆盖 |
|------|------|------|
| 标准格式 | `2025-07-24 11:31:13.714532 pcie_device ERROR: pcie_card.lua(49): PCIe card oob management init failed.` | ~98% |
| LAUNCH 格式 (仅 framework.log) | `1970-01-01 00:00:21.666722 [:00000002] framework: LAUNCH snlua bootstrap` | ~183 行 |

**日志级别分布：** ERROR (~2,726)、NOTICE (~12,652)、WARNING (~520)、LAUNCH (~183)

**标准格式解析正则：**
```
^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) (?P<component>\S+) (?P<level>ERROR|WARNING|WARN|NOTICE|INFO|DEBUG|CRITICAL): (?P<file>\S+)\((?P<line>\d+)\): (?P<message>.*)$
```

**LAUNCH 格式降级正则：**
```
^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) \[(?P<thread>[^\]]+)\] (?P<component>\S+): LAUNCH (?P<message>.*)$
```

---

## 架构设计

```
┌─────────────────────┐   HTTP/JSON      ┌──────────────────────┐
│   React 前端        │ ◄──────────────► │   Python 后端        │
│   (Vite + React)    │ POST /api/parse   │   (FastAPI)          │
│                     │ GET  /api/health  │                      │
│   端口: 5173 (dev)  │                   │   端口: 8000         │
└─────────────────────┘                   └──────────────────────┘
```

---

## 项目目录结构

```
no_error/
├── docs/
│   └── implementation-plan.md    # 本文档
│
├── backend/
│   ├── requirements.txt          # fastapi, uvicorn, python-multipart
│   ├── main.py                   # FastAPI 应用入口、CORS、路由
│   ├── parser.py                 # 日志解析器（正则提取）
│   ├── extractor.py              # Tar.gz 解压器（tarfile + gzip）
│   └── uploads/                  # 上传文件临时目录
│
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx               # 根组件：深色主题容器
│       ├── App.css               # 全局样式变量
│       ├── api/
│       │   └── client.ts         # 后端 fetch 封装
│       ├── types/
│       │   └── log.ts            # LogEntry、ParseResult 类型定义
│       └── components/
│           ├── UploadZone.tsx    # 拖拽上传区域
│           ├── KpiCards.tsx      # KPI 统计卡片（总行数/错误/警告）
│           ├── FilterBar.tsx     # 筛选栏（组件选择+级别+搜索）
│           ├── LogTable.tsx      # 日志表格主组件
│           ├── LogRow.tsx        # 单行日志（可展开查看详情）
│           └── StatsPanel.tsx    # 组件错误分布图表
```

---

## 后端 API 设计

### `POST /api/parse`
- **输入**：multipart/form-data，字段名 `file`（tar.gz 压缩包）
- **处理流程**：
  1. 保存到临时目录
  2. 使用 `tarfile` 模块解压，仅提取 `*/LogDump/app.log` 和 `*/LogDump/framework.log`
  3. 逐行读取日志，应用正则解析
  4. 匹配成功 → 结构化条目 / 匹配失败 → 原始条目，标记 `level: "UNKNOWN"`
  5. 返回 JSON
- **输出格式**：
```json
{
  "success": true,
  "summary": {
    "totalLines": 16106,
    "errorCount": 2726,
    "warningCount": 520,
    "noticeCount": 12652,
    "components": ["sensor", "pcie_device", "hwproxy", "..."],
    "timeRange": {
      "start": "1970-01-01 00:00:21",
      "end": "2025-07-28 13:12:51"
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
- 返回 `{ "status": "ok" }`

---

## 前端组件树与数据流

```
App
├── 页面头部（标题 + 系统状态指示器）
├── UploadZone          ← 文件拖拽上传，解析中 loading 状态
├── KpiCards            ← 读取 summary：总行数、错误数、警告数
├── FilterBar           ← 纯前端过滤 entries[]
│   ├── 组件下拉选择器（数据来自 summary.components）
│   ├── 级别选择器（ERROR | WARNING | NOTICE | ALL）
│   ├── 时间范围选择器
│   └── 搜索输入框（全文搜索消息内容）
└── LogTable            ← 展示过滤后的条目
    └── LogRow（可展开行 → 查看完整消息 + 源文件路径）
```

**状态管理**：React `useState` + `useMemo`（当前规模无需 Redux）。筛选通过 `useMemo` 计算派生数据。

---

## 设计规范映射（来自 DESIGN.md）

| 用途 | CSS 变量 | 色值 |
|------|---------|------|
| 页面背景 | `--bg-primary` | `#121212` |
| 卡片背景 | `--bg-card` | `#1E1E1E` |
| 数据强调色 | `--accent-data` | `#00E5FF`（KPI 数字、链接） |
| 警告/错误色 | `--accent-alert` | `#FF453A`（ERROR 行高亮） |
| 正常状态色 | `--accent-ok` | `#32D74B`（成功状态） |
| 主文字色 | `--text-primary` | `#FFFFFF` |
| 次要文字色 | `--text-secondary` | `#98989D` |
| 边框色 | `--border-subtle` | `#2C2C2E` |
| 告警背景 | `--alert-bg` | `#3A1C1C` |
| 等宽字体 | `--font-mono` | JetBrains Mono（KPI、时间戳） |
| 正文字体 | `--font-body` | Inter（其余文字） |

**组件规范**：
- 卡片：`border-radius: 16px`，`padding: 20px`，`border: 1px solid #2C2C2E`
- 表格行：`border-bottom: 1px solid #2C2C2E`，hover 时背景变为 `#252525`
- 错误告警框：背景 `#3A1C1C`，左边框 `4px solid #FF453A`

---

## 实现步骤

### 第一步：后端搭建
1. 创建 `backend/` 目录及 `requirements.txt`
2. 实现 `extractor.py` — tar.gz 解压，提取 app.log 和 framework.log
3. 实现 `parser.py` — 双正则策略解析日志行
4. 实现 `main.py` — FastAPI 应用 + CORS + 接口
5. 用实际 dump_info.tar.gz 测试

### 第二步：前端脚手架
1. Vite + React + TypeScript 项目初始化
2. 编写 CSS 变量和全局样式（DESIGN.md 配色）
3. 定义 TypeScript 类型
4. 实现 API 调用封装

### 第三步：前端组件开发
1. UploadZone — 拖拽上传区
2. KpiCards — 三张统计卡片
3. FilterBar — 筛选栏
4. LogTable + LogRow — 日志表格与可展开行
5. App.tsx — 组合所有组件

### 第四步：联调与打磨
1. 前后端联调，完整上传→解析→展示流程
2. 添加 loading / empty / error 状态处理
3. 响应式布局适配

---

## 验证方式

1. 启动后端：`cd backend && uvicorn main:app --reload --port 8000`
2. 启动前端：`cd frontend && npm run dev`
3. 通过 UI 上传 `dump_info.tar.gz`
4. 验证 KPI 卡片显示：~16,106 总行数、~2,726 错误、~520 警告
5. 筛选组件 `pcie_device` → 只显示 PCIe 相关错误
6. 筛选级别 `ERROR` → 只显示 ERROR 条目
7. 确认深色主题配色与 DESIGN.md 规范一致
