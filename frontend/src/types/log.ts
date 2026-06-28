/**
 * TypeScript 类型定义 —— 日志条目、解析结果、会话与聊天
 */

/** 单条日志记录 */
export interface LogEntry {
  id: number;
  timestamp: string | null;
  component: string | null;
  level: 'ERROR' | 'WARNING' | 'NOTICE' | 'INFO' | 'DEBUG' | 'CRITICAL' | 'LAUNCH' | 'UNKNOWN';
  file: string | null;
  line: number | null;
  message: string;
  source: 'app.log' | 'framework.log';
}

/** 解析汇总统计 */
export interface ParseSummary {
  totalLines: number;
  errorCount: number;
  warningCount: number;
  noticeCount: number;
  launchCount: number;
  unknownCount: number;
  components: string[];
  /** 按组件的错误数分布（Top20，供 StatsPanel 直接展示，无需前端全量聚合） */
  componentErrors: ComponentErrorStat[];
  timeRange: {
    start: string | null;
    end: string | null;
  };
}

/** 组件错误统计项 */
export interface ComponentErrorStat {
  name: string;
  count: number;
}

/** 解析接口返回的完整结果 */
export interface ParseResult {
  success: boolean;
  dataset_id?: string;      // NEW: 数据集 ID，用于创建会话
  summary: ParseSummary | null;
  entries: LogEntry[];
  errors: string[];
}

/** 筛选条件 */
export interface FilterState {
  component: string;
  level: string;
  search: string;
}

/** 聊天消息 */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  /** 后端为本轮生成的 trace_id（来自 SSE done），用于点赞/踩关联到 Langfuse trace */
  traceId?: string;
}

/** 聊天请求（简化：只需 message + session_id，历史由服务端管理） */
export interface ChatRequest {
  message: string;
  session_id: string;
}

/** 聊天响应 */
export interface ChatResponse {
  reply: string;
  /** 后端为本轮生成的 trace_id，用于点赞/踩关联到 Langfuse trace */
  trace_id?: string;
  /** 整轮对话的 token 消耗（降级模式为 0） */
  usage?: {
    input: number;
    output: number;
    total: number;
  };
}

/** 会话元数据（列表用） */
export interface SessionInfo {
  session_id: string;
  /** 关联数据集 ID；null = 尚未上传日志的纯对话会话 */
  dataset_id: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
  title: string;
}

/** 会话详情（含完整消息历史，用于恢复） */
export interface SessionDetail {
  session_id: string;
  /** 关联数据集 ID；null = 尚未上传日志的纯对话会话 */
  dataset_id: string | null;
  created_at: string;
  updated_at: string;
  messages: SessionMessage[];
}

/** 会话中的单条消息 */
export interface SessionMessage {
  role: string;
  content: string | null;
  tool_calls?: Array<{
    id: string;
    type: string;
    function: { name: string; arguments: string };
  }>;
  tool_call_id?: string;
}

/** SSE 流式事件（聊天用） */
export interface StreamEvent {
  type: 'status' | 'tool_progress' | 'delta' | 'done' | 'usage';
  text?: string;
  tool?: string;
  status?: string;
  /** type === 'done' 时携带：后端为本轮生成的 trace_id，用于点赞/踩关联 */
  trace_id?: string;
  /** type === 'usage' 时携带：整轮对话的 token 消耗 */
  input?: number;
  output?: number;
  total?: number;
}

/**
 * SSE 流式事件（上传解析用）。
 *
 * 独立于 StreamEvent —— 上传解析的事件结构与聊天完全不同，
 * 合并会让两端的 switch 出现永不命中的分支。
 */
export type ParseStreamEvent =
  | { type: 'progress'; stage: 'extracting' | 'parsing' }
  | { type: 'summary'; dataset_id: string; summary: ParseSummary }
  | { type: 'done'; dataset_id: string }
  | { type: 'error'; message: string };

/** 分页日志条目响应（/api/datasets/{id}/entries） */
export interface PaginatedEntries {
  items: LogEntry[];
  total: number;
  offset: number;
  limit: number;
  hasMore: boolean;
}
