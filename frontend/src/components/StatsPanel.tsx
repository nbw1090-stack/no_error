/**
 * StatsPanel 组件 —— 组件错误分布面板
 * 纯 CSS 水平条形图展示 Top 15 组件的错误数量
 *
 * 数据直接取自 summary.componentErrors（后端解析时已聚合），
 * 不再依赖前端全量 entries。
 */

import { useMemo } from 'react';
import type { ParseSummary } from '../types/log';
import './StatsPanel.css';

interface StatsPanelProps {
  summary: ParseSummary;
}

export default function StatsPanel({ summary }: StatsPanelProps) {
  /** 取 Top 15 组件错误分布（后端已排序，前端只截断 + 算百分比） */
  const componentErrors = useMemo(() => {
    const items = summary.componentErrors ?? [];
    const maxCount = items.length > 0 ? items[0].count : 1;
    return items.slice(0, 15).map(({ name, count }) => ({
      name,
      count,
      percentage: (count / maxCount) * 100,
    }));
  }, [summary]);

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
