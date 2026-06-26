/**
 * LogTable 组件 —— 日志表格
 * 展示过滤后的日志条目列表，按级别着色，固定表头
 */

import type { LogEntry } from '../types/log';
import LogRow from './LogRow';
import './LogTable.css';

interface LogTableProps {
  entries: LogEntry[];
  loading: boolean;
}

export default function LogTable({ entries, loading }: LogTableProps) {
  if (loading) {
    return (
      <div className="log-table-container">
        <div className="empty-state">
          <div className="spinner" />
        </div>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="log-table-container">
        <div className="empty-state">
          <p>暂无匹配的日志条目</p>
        </div>
      </div>
    );
  }

  return (
    <div className="log-table-container">
      <table className="log-table">
        <thead>
          <tr>
            <th className="col-id">ID</th>
            <th className="col-timestamp">时间戳</th>
            <th className="col-level">级别</th>
            <th className="col-component">组件</th>
            <th className="col-file">文件:行号</th>
            <th className="col-source">来源</th>
            <th className="col-message">消息</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <LogRow key={entry.id} entry={entry} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
