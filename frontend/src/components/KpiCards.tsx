/**
 * KpiCards 组件 —— KPI 统计卡片
 * 展示总行数、错误数、警告数、通知数、启动数及时间范围
 */

import type { ParseSummary } from '../types/log';
import './KpiCards.css';

interface KpiCardsProps {
  summary: ParseSummary;
}

export default function KpiCards({ summary }: KpiCardsProps) {
  const cards = [
    { label: '总日志行数', value: summary.totalLines.toLocaleString(), className: 'total' },
    { label: '错误 (ERROR)', value: summary.errorCount.toLocaleString(), className: 'errors' },
    { label: '警告 (WARNING)', value: summary.warningCount.toLocaleString(), className: 'warnings' },
    { label: '通知 (NOTICE)', value: summary.noticeCount.toLocaleString(), className: 'notices' },
    { label: '启动 (LAUNCH)', value: summary.launchCount.toLocaleString(), className: 'launches' },
  ];

  return (
    <div className="kpi-section">
      <div className="kpi-grid">
        {cards.map((card) => (
          <div key={card.className} className={`kpi-card ${card.className}`}>
            <div className="kpi-label">{card.label}</div>
            <div className="kpi-value">{card.value}</div>
          </div>
        ))}
      </div>
      {summary.timeRange.start && summary.timeRange.end && (
        <div className="time-range">
          <span className="time-range-label">时间范围：</span>
          <span className="time-range-value">{summary.timeRange.start} ~ {summary.timeRange.end}</span>
        </div>
      )}
    </div>
  );
}
