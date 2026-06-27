/**
 * FilterBar 组件 —— 筛选栏
 * 提供组件选择器、日志级别按钮组、全文搜索输入框（带防抖）
 */

import { useState, useEffect, useRef } from 'react';
import type { ParseSummary, FilterState } from '../types/log';
import './FilterBar.css';

interface FilterBarProps {
  summary: ParseSummary;
  filters: FilterState;
  onFiltersChange: (filters: FilterState) => void;
}

/** 可用的日志级别 */
const LEVELS = ['ALL', 'ERROR', 'WARNING', 'NOTICE', 'INFO', 'LAUNCH', 'UNKNOWN'];

/** 搜索防抖延迟（毫秒） */
const SEARCH_DEBOUNCE_MS = 200;

export default function FilterBar({ summary, filters, onFiltersChange }: FilterBarProps) {
  // 本地搜索值（即时响应输入，不卡顿）
  const [searchValue, setSearchValue] = useState(filters.search);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 当外部 filters.search 变化时同步本地值（如会话切换时重置）
  useEffect(() => {
    setSearchValue(filters.search);
  }, [filters.search]);

  // 防抖：仅搜索字段延迟更新到父组件
  useEffect(() => {
    // 跳过初始同步（本地值已经等于 filters.search）
    if (searchValue === filters.search) return;

    debounceTimerRef.current = setTimeout(() => {
      onFiltersChange({ ...filters, search: searchValue });
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchValue]);

  /** 即时更新非搜索字段（组件、级别） */
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

      {/* 全文搜索输入框（本地状态 + 防抖提交） */}
      <input
        type="text"
        className="filter-search"
        placeholder="搜索日志内容..."
        value={searchValue}
        onChange={(e) => setSearchValue(e.target.value)}
      />
    </div>
  );
}
