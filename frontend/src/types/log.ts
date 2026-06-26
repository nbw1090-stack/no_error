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
  timeRange: {
    start: string | null;
    end: string | null;
  };
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
}

/** 聊天请求（简化：只需 message + session_id，历史由服务端管理） */
export interface ChatRequest {
  message: string;
  session_id: string;
}

/** 聊天响应 */
export interface ChatResponse {
  reply: string;
}

/** 会话元数据（列表用） */
export interface SessionInfo {
  session_id: string;
  dataset_id: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

/** 会话详情（含完整消息历史，用于恢复） */
export interface SessionDetail {
  session_id: string;
  dataset_id: string;
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
