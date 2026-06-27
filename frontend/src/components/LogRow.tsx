/**
 * LogRow 组件 —— 单行日志条目
 *
 * 可点击展开查看完整消息内容，级别用彩色标签展示。
 * 展开状态由父组件 LogTable 管理（用于虚拟滚动持久化）。
 */

import { forwardRef } from 'react';
import type { LogEntry } from '../types/log';
import './LogRow.css';

interface LogRowProps {
  entry: LogEntry;
  isExpanded: boolean;
  onToggleExpand: (entryId: number) => void;
}

/** 获取行的级别样式类名 */
function getRowClass(level: string): string {
  switch (level) {
    case 'ERROR': return 'row-error';
    case 'WARNING': return 'row-warning';
    case 'LAUNCH': return 'row-launch';
    case 'UNKNOWN': return 'row-unknown';
    default: return '';
  }
}

/** 格式化文件:行号 */
function formatFileRef(file: string | null, line: number | null): string {
  if (!file) return '-';
  if (line !== null) return `${file}:${line}`;
  return file;
}

const LogRow = forwardRef<HTMLTableRowElement, LogRowProps>(
  function LogRow({ entry, isExpanded, onToggleExpand }, ref) {
    const rowClass = [
      'log-row',
      getRowClass(entry.level),
      isExpanded ? 'expanded' : '',
    ].filter(Boolean).join(' ');

    const handleClick = () => onToggleExpand(entry.id);

    return (
      <>
        <tr ref={ref} className={rowClass} onClick={handleClick}>
          <td className="col-id">{entry.id}</td>
          <td className="col-timestamp">{entry.timestamp || '-'}</td>
          <td className="col-level">
            <span className={`level-badge ${entry.level.toLowerCase()}`}>
              {entry.level}
            </span>
          </td>
          <td className="col-component">{entry.component || '-'}</td>
          <td className="col-file">{formatFileRef(entry.file, entry.line)}</td>
          <td className="col-source">{entry.source}</td>
          <td className="col-message" title={entry.message}>
            {entry.message}
          </td>
        </tr>
        {isExpanded && (
          <tr className="expanded-row">
            <td colSpan={7} className="expanded-message-cell">
              <div className="expanded-message">
                <div className="expanded-meta">
                  {entry.component && <span>组件: {entry.component}</span>}
                  {entry.file && <span>文件: {formatFileRef(entry.file, entry.line)}</span>}
                  <span>来源: {entry.source}</span>
                </div>
                <pre className="expanded-body">{entry.message}</pre>
              </div>
            </td>
          </tr>
        )}
      </>
    );
  }
);

export default LogRow;
