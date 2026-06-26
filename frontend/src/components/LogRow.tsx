/**
 * LogRow 组件 —— 单行日志条目
 * 可点击展开查看完整消息内容，级别用彩色标签展示
 */

import { useState } from 'react';
import type { LogEntry } from '../types/log';
import './LogRow.css';

interface LogRowProps {
  entry: LogEntry;
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

export default function LogRow({ entry }: LogRowProps) {
  const [expanded, setExpanded] = useState(false);

  const rowClass = [
    'log-row',
    getRowClass(entry.level),
    expanded ? 'expanded' : '',
  ].filter(Boolean).join(' ');

  return (
    <>
      <tr className={rowClass} onClick={() => setExpanded(!expanded)}>
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
      {expanded && (
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
