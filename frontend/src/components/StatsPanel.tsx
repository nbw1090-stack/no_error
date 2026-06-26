/**
 * StatsPanel 组件 —— 组件错误分布面板
 * 纯 CSS 水平条形图展示 Top 15 组件的错误数量
 */

import { useMemo } from 'react';
import type { LogEntry } from '../types/log';
import './StatsPanel.css';

interface StatsPanelProps {
  entries: LogEntry[];
}

export default function StatsPanel({ entries }: StatsPanelProps) {
  /** 统计每个组件的 ERROR 数量，取 Top 15 */
  const componentErrors = useMemo(() => {
    const errorMap = new Map<string, number>();

    for (const entry of entries) {
      if (entry.level === 'ERROR' && entry.component) {
        errorMap.set(entry.component, (errorMap.get(entry.component) || 0) + 1);
      }
    }

    const sorted = Array.from(errorMap.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 15);

    const maxCount = sorted.length > 0 ? sorted[0][1] : 1;

    return sorted.map(([name, count]) => ({
      name,
      count,
      percentage: (count / maxCount) * 100,
    }));
  }, [entries]);

  if (componentErrors.length === 0) {
    return (
      <div className="stats-panel">
        <h3>组件错误分布</h3>
        <p className="stats-empty">暂无错误数据</p>
      </div>
    );
  }

  return (
    <div className="stats-panel">
      <h3>组件错误分布（Top {componentErrors.length}）</h3>
      <div className="stats-bars">
        {componentErrors.map((item) => (
          <div key={item.name} className="stat-bar-item">
            <div className="bar-label">
              <span className="bar-name">{item.name}</span>
              <span className="bar-count">{item.count}</span>
            </div>
            <div className="bar-track">
              <div
                className="bar-fill"
                style={{ width: `${item.percentage}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
