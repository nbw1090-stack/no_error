/**
 * App 根组件
 *
 * 管理全局状态：解析结果、筛选条件、会话生命周期、加载/错误状态
 *
 * 会话生命周期：
 * 1. 上传解析成功后自动创建会话（sessionId 存入 state + localStorage）
 * 2. 页面加载时尝试从 localStorage 恢复会话
 * 3. 恢复失败（404）则清除旧 sessionId，等待用户重新上传
 */

import { useState, useEffect, useMemo } from 'react';
import { checkHealth, createSession, getSession } from './api/client';
import type { ParseResult, FilterState } from './types/log';
import UploadZone from './components/UploadZone';
import KpiCards from './components/KpiCards';
import FilterBar from './components/FilterBar';
import LogTable from './components/LogTable';
import StatsPanel from './components/StatsPanel';
import ChatPanel from './components/ChatPanel';

const SESSION_STORAGE_KEY = 'bmc_session_id';

export default function App() {
  const [parseResult, setParseResult] = useState<ParseResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);
  const [llmAvailable, setLlmAvailable] = useState<boolean>(false);
  const [filters, setFilters] = useState<FilterState>({
    component: '',
    level: 'ALL',
    search: '',
  });

  // 会话状态
  const [sessionId, setSessionId] = useState<string>('');
  const [sessionRestored, setSessionRestored] = useState(false);

  /** 页面加载时：检查后端 + 恢复会话 */
  useEffect(() => {
    async function init() {
      // 检查后端健康状态
      const health = await checkHealth();
      setBackendOnline(health.status === 'ok');
      setLlmAvailable(health.llm_available ?? false);

      // 尝试从 localStorage 恢复会话
      const savedSessionId = localStorage.getItem(SESSION_STORAGE_KEY);
      if (savedSessionId) {
        try {
          await getSession(savedSessionId);
          setSessionId(savedSessionId);
        } catch {
          // 会话已过期或删除，清除
          localStorage.removeItem(SESSION_STORAGE_KEY);
        }
      }
      setSessionRestored(true);
    }

    init();
  }, []);

  /** 上传成功回调：解析完成后自动创建会话 */
  async function handleParseResult(result: ParseResult) {
    setParseResult(result);
    setError(null);
    setFilters({ component: '', level: 'ALL', search: '' });

    // 创建新会话
    if (result.dataset_id) {
      try {
        const session = await createSession(result.dataset_id);
        setSessionId(session.session_id);
        localStorage.setItem(SESSION_STORAGE_KEY, session.session_id);
      } catch (e) {
        console.error('Failed to create session:', e);
      }
    }
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
      if (filters.component && entry.component !== filters.component) {
        return false;
      }
      if (filters.level !== 'ALL' && entry.level !== filters.level) {
        return false;
      }
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
          <span className={`status-dot ${backendOnline ? 'online' : backendOnline === false ? 'offline' : ''}`} />
          <span>
            {backendOnline === null
              ? '检查中...'
              : backendOnline
                ? `后端已连接${llmAvailable ? ' · LLM 已就绪' : ' · 规则匹配模式'}`
                : '后端未连接'}
          </span>
          {sessionId && (
            <span className="session-badge" title={sessionId}>
              会话: {sessionId.slice(0, 8)}...
            </span>
          )}
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

          {/* Agent 对话窗口 */}
          {sessionId && sessionRestored && (
            <div className="chat-section">
              <ChatPanel sessionId={sessionId} />
            </div>
          )}
        </>
      )}
    </div>
  );
}
