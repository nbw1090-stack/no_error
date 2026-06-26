/**
 * FilterBar 组件 —— 筛选栏
 * 提供组件选择器、日志级别按钮组、全文搜索输入框
 */

import type { ParseSummary, FilterState } from '../types/log';
import './FilterBar.css';

interface FilterBarProps {
  summary: ParseSummary;
  filters: FilterState;
  onFiltersChange: (filters: FilterState) => void;
}

/** 可用的日志级别 */
const LEVELS = ['ALL', 'ERROR', 'WARNING', 'NOTICE', 'INFO', 'LAUNCH', 'UNKNOWN'];

export default function FilterBar({ summary, filters, onFiltersChange }: FilterBarProps) {
  function updateFilter(key: keyof FilterState, value: string) {
    onFiltersChange({ ...filters, [key]: value });
  }

  return (
    <div className="filter-bar">
      {/* 组件下拉选择器 */}
      <select
        className="filter-select"
        value={filters.component}
        onChange={(e) => updateFilter('component', e.target.value)}
      >
        <option value="">所有组件</option>
        {summary.components.map((comp) => (
          <option key={comp} value={comp}>{comp}</option>
        ))}
      </select>

      {/* 日志级别按钮组 */}
      <div className="level-buttons">
        {LEVELS.map((level) => (
          <button
            key={level}
            className={`level-btn ${filters.level === level ? 'active' : ''}`}
            onClick={() => updateFilter('level', level)}
          >
            {level}
          </button>
        ))}
      </div>

      {/* 全文搜索输入框 */}
      <input
        type="text"
        className="filter-search"
        placeholder="搜索日志内容..."
        value={filters.search}
        onChange={(e) => updateFilter('search', e.target.value)}
      />
    </div>
  );
}
