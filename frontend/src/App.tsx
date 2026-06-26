/**
 * App 根组件
 * 组合 UploadZone、KpiCards、FilterBar、LogTable、StatsPanel
 * 管理全局状态：解析结果、筛选条件、加载/错误状态
 */

import { useState, useEffect, useMemo } from 'react';
import { checkHealth } from './api/client';
import type { ParseResult, FilterState } from './types/log';
import UploadZone from './components/UploadZone';
import KpiCards from './components/KpiCards';
import FilterBar from './components/FilterBar';
import LogTable from './components/LogTable';
import StatsPanel from './components/StatsPanel';
import ChatPanel from './components/ChatPanel';

export default function App() {
  const [parseResult, setParseResult] = useState<ParseResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);
  const [filters, setFilters] = useState<FilterState>({
    component: '',
    level: 'ALL',
    search: '',
  });

  /** 页面加载时检查后端健康状态 */
  useEffect(() => {
    checkHealth().then(setBackendOnline);
  }, []);

  /** 上传成功回调 */
  function handleParseResult(result: ParseResult) {
    setParseResult(result);
    setError(null);
    setFilters({ component: '', level: 'ALL', search: '' });
  }

  /** 错误回调 */
  function handleError(msg: string) {
    setError(msg);
    setParseResult(null);
  }

  /** 根据筛选条件过滤条目 */
  const filteredEntries = useMemo(() => {
    if (!parseResult?.entries) return [];

    return parseResult.entries.filter((entry) => {
      // 组件筛选
      if (filters.component && entry.component !== filters.component) {
        return false;
      }

      // 级别筛选
      if (filters.level !== 'ALL' && entry.level !== filters.level) {
        return false;
      }

      // 搜索筛选（不区分大小写）
      if (filters.search) {
        const keyword = filters.search.toLowerCase();
        if (!entry.message.toLowerCase().includes(keyword)) {
          return false;
        }
      }

      return true;
    });
  }, [parseResult, filters]);

  return (
    <div className="app">
      {/* 页面头部 */}
      <header className="app-header">
        <h1>BMC 日志分析系统</h1>
        <div className="status-indicator">
          <span className={`status-dot ${backendOnline ? 'online' : 'offline'}`} />
          <span>
            {backendOnline === null
              ? '检查中...'
              : backendOnline
                ? '后端服务已连接'
                : '后端服务未连接'}
          </span>
        </div>
      </header>

      {/* 上传区域 */}
      <UploadZone onParseResult={handleParseResult} onError={handleError} />

      {/* 全局错误提示 */}
      {error && (
        <div className="error-message" style={{ marginBottom: 24 }}>
          {error}
        </div>
      )}

      {/* 解析成功后展示仪表盘 */}
      {parseResult && parseResult.summary && (
        <>
          <KpiCards summary={parseResult.summary} />

          <div className="main-content">
            <div className="left-panel">
              <FilterBar
                summary={parseResult.summary}
                filters={filters}
                onFiltersChange={setFilters}
              />
              <LogTable entries={filteredEntries} loading={false} />
            </div>
            <div className="right-panel">
              <StatsPanel entries={parseResult.entries} />
            </div>
          </div>

          {/* Agent 对话窗口（全宽） */}
          <div className="chat-section">
            <ChatPanel parseResult={parseResult} />
          </div>
        </>
      )}
    </div>
  );
}
