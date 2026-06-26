/**
 * TypeScript 类型定义 —— 日志条目、解析结果与汇总统计
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
