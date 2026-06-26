/**
 * App 根组件
 *
 * 管理全局状态：解析结果、筛选条件、会话生命周期、加载/错误状态
 *
 * 会话管理生命周期：
 * 1. 页面加载时从服务端获取所有历史会话
 * 2. 尝试从 localStorage 恢复上次活跃会话（自动加载日志数据 + 聊天历史）
 * 3. 用户可在左侧边栏切换会话、删除会话、开始新会话
 * 4. 上传新日志后自动创建会话并加入列表
 *
 * 性能优化：
 * - 并行请求（health + sessions 同时发起）
 * - 数据集内存缓存（避免切换会话时重复加载）
 * - 乐观删除（立即从 UI 移除，失败时回滚）
 * - 预加载会话消息传递给 ChatPanel，消除冗余 fetch
 */

import { useState, useEffect, useMemo, useRef } from 'react';
import {
  checkHealth,
  createSession,
  getSession,
  listSessions,
  deleteSession,
  getDataset,
} from './api/client';
import type { ParseResult, FilterState, SessionInfo, SessionMessage } from './types/log';
import UploadZone from './components/UploadZone';
import KpiCards from './components/KpiCards';
import FilterBar from './components/FilterBar';
import LogTable from './components/LogTable';
import StatsPanel from './components/StatsPanel';
import ChatPanel from './components/ChatPanel';
import SessionSidebar from './components/SessionSidebar';

const SESSION_STORAGE_KEY = 'bmc_session_id';

/** 数据集缓存最大条目数 */
const DATASET_CACHE_MAX = 5;

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

  // ---- 会话状态 ----
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [sessionRestored, setSessionRestored] = useState(false);

  // ---- 预加载的会话消息（传递给 ChatPanel 避免冗余 fetch） ----
  // undefined = 尚未加载（ChatPanel 等待），[] = 空会话，[...] = 有历史消息
  const [preloadedMessages, setPreloadedMessages] = useState<SessionMessage[] | undefined>(undefined);

  // ---- 请求序列号（处理快速切换会话时的竞态） ----
  const selectRequestSeq = useRef(0);

  // ---- 数据集缓存（key: dataset_id, value: ParseResult） ----
  const datasetCache = useRef<Map<string, ParseResult>>(new Map());

  /** 从服务端加载所有会话列表 */
  async function loadSessions() {
    try {
      const result = await listSessions();
      setSessions(result.sessions);
    } catch (e) {
      console.error('Failed to load sessions:', e);
    }
  }

  /**
   * 选中某个会话：并行加载其关联的日志数据和聊天历史。
   *
   * 优化点：
   * 1. 从 sessions 列表中直接获取 dataset_id，无需额外 getSession 调用
   * 2. getDataset 和 getSession 并行请求
   * 3. 数据集结果缓存到内存，切换回同一数据集时即时响应
   */
  async function handleSelectSession(sessionId: string) {
    // 如果点击的是当前活跃会话，不重复加载
    if (sessionId === activeSessionId) return;

    const requestId = ++selectRequestSeq.current;

    setActiveSessionId(sessionId);
    localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    setPreloadedMessages(undefined); // 通知 ChatPanel：正在加载中

    // 从已加载的会话列表中获取 dataset_id（无需额外 API 调用）
    const sessionInfo = sessions.find((s) => s.session_id === sessionId);
    const datasetId = sessionInfo?.dataset_id;

    try {
      // 并行请求：数据集 + 会话消息历史
      const datasetPromise: Promise<ParseResult> = (() => {
        if (!datasetId) return Promise.reject(new Error('No dataset_id'));
        // 命中缓存则直接返回
        if (datasetCache.current.has(datasetId)) {
          return Promise.resolve(datasetCache.current.get(datasetId)!);
        }
        return getDataset(datasetId);
      })();

      const [dataset, sessionDetail] = await Promise.all([
        datasetPromise,
        getSession(sessionId),
      ]);

      // 竞态检查：忽略非最新请求的结果
      if (requestId !== selectRequestSeq.current) return;

      // 更新缓存（LRU 简易策略：超过上限则清空重建）
      if (datasetId && !datasetCache.current.has(datasetId)) {
        if (datasetCache.current.size >= DATASET_CACHE_MAX) {
          datasetCache.current.clear();
        }
        datasetCache.current.set(datasetId, dataset);
      }

      setParseResult(dataset);
      setPreloadedMessages(sessionDetail.messages);
      setError(null);
      setFilters({ component: '', level: 'ALL', search: '' });
    } catch (e) {
      // 竞态检查
      if (requestId !== selectRequestSeq.current) return;
      console.error('Failed to load session data:', e);
      setError('会话数据加载失败，该会话可能已被删除。');
      setPreloadedMessages([]); // 解锁 ChatPanel（空消息）
    }
  }

  /** 开始新会话：清空右侧，显示上传区域 */
  function handleNewSession() {
    setParseResult(null);
    setActiveSessionId(null);
    setError(null);
    setFilters({ component: '', level: 'ALL', search: '' });
    setPreloadedMessages(undefined);
    localStorage.removeItem(SESSION_STORAGE_KEY);
  }

  /**
   * 删除会话（乐观更新）。
   *
   * 点击删除后立即从 UI 移除会话，若后端删除失败则回滚并提示错误。
   */
  async function handleDeleteSession(sessionId: string) {
    // 保存当前状态用于回滚
    const prevSessions = sessions;
    const wasActive = activeSessionId === sessionId;
    const prevActiveId = activeSessionId;
    const prevParseResult = parseResult;
    const prevPreloadedMessages = preloadedMessages;

    // 乐观更新：立即从 UI 移除
    setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
    if (wasActive) {
      setActiveSessionId(null);
      setParseResult(null);
      setError(null);
      setPreloadedMessages(undefined);
      localStorage.removeItem(SESSION_STORAGE_KEY);
    }

    try {
      await deleteSession(sessionId);
    } catch (e) {
      // 回滚：恢复删除前的状态
      setSessions(prevSessions);
      if (wasActive) {
        setActiveSessionId(prevActiveId);
        setParseResult(prevParseResult);
        setPreloadedMessages(prevPreloadedMessages);
        if (prevActiveId) {
          localStorage.setItem(SESSION_STORAGE_KEY, prevActiveId);
        }
      }
      console.error('Failed to delete session:', e);
      setError('删除会话失败，请重试。');
    }
  }

  /**
   * 上传成功回调：解析完成后自动创建会话。
   *
   * 优化：创建成功后直接将返回的 SessionInfo 加入列表头部，无需重新拉取全部会话。
   */
  async function handleParseResult(result: ParseResult) {
    setParseResult(result);
    setError(null);
    setFilters({ component: '', level: 'ALL', search: '' });

    // 创建新会话并加入列表
    if (result.dataset_id) {
      try {
        const session = await createSession(result.dataset_id);
        setActiveSessionId(session.session_id);
        setPreloadedMessages([]); // 新会话无历史消息
        localStorage.setItem(SESSION_STORAGE_KEY, session.session_id);

        // 直接加入列表头部，无需重新 fetch
        const newSessionInfo: SessionInfo = {
          session_id: session.session_id,
          dataset_id: session.dataset_id,
          created_at: session.created_at,
          updated_at: session.created_at,
          message_count: 0,
          title: '',
        };
        setSessions((prev) => [newSessionInfo, ...prev]);

        // 缓存新创建的数据集
        if (!datasetCache.current.has(result.dataset_id!)) {
          if (datasetCache.current.size >= DATASET_CACHE_MAX) {
            datasetCache.current.clear();
          }
          datasetCache.current.set(result.dataset_id!, result);
        }
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

  /**
   * 页面加载时：并行检查后端 + 加载会话列表，然后恢复上次活跃会话。
   *
   * 优化：health 和 sessions 请求并行，减少首屏等待时间。
   */
  useEffect(() => {
    async function init() {
      // 1. 并行请求：健康检查 + 会话列表（互不依赖）
      const [health] = await Promise.all([
        checkHealth(),
        loadSessions(),
      ]);
      setBackendOnline(health.status === 'ok');
      setLlmAvailable(health.llm_available ?? false);
      setSessionsLoading(false);

      // 2. 尝试恢复上次活跃会话
      const savedSessionId = localStorage.getItem(SESSION_STORAGE_KEY);
      if (savedSessionId) {
        try {
          const session = await getSession(savedSessionId);
          const dataset = await getDataset(session.dataset_id);
          setActiveSessionId(savedSessionId);
          setParseResult(dataset);
          setPreloadedMessages(session.messages);
          // 缓存数据集
          if (!datasetCache.current.has(session.dataset_id)) {
            datasetCache.current.set(session.dataset_id, dataset);
          }
        } catch {
          // 会话或数据集已过期/删除，清除记录
          localStorage.removeItem(SESSION_STORAGE_KEY);
        }
      }

      setSessionRestored(true);
    }

    init();
  }, []);

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

  /** 是否显示上传区域（无活跃会话时显示，引导用户上传） */
  const showUpload = !activeSessionId && !parseResult;

  return (
    <div className="app">
      {/* 页面头部 */}
      <header className="app-header">
        <h1>BMC 日志分析系统</h1>
        <div className="status-indicator">
          <span
            className={`status-dot ${backendOnline ? 'online' : backendOnline === false ? 'offline' : ''}`}
          />
          <span>
            {backendOnline === null
              ? '检查中...'
              : backendOnline
                ? `后端已连接${llmAvailable ? ' · LLM 已就绪' : ' · 规则匹配模式'}`
                : '后端未连接'}
          </span>
          {activeSessionId && (
            <span className="session-badge" title={activeSessionId}>
              会话: {activeSessionId.slice(0, 8)}...
            </span>
          )}
        </div>
      </header>

      {/* 主体布局：侧边栏 + 主内容区 */}
      <div className="app-layout">
        <SessionSidebar
          sessions={sessions}
          activeSessionId={activeSessionId}
          onSelectSession={handleSelectSession}
          onNewSession={handleNewSession}
          onDeleteSession={handleDeleteSession}
          loading={sessionsLoading}
        />

        <div className="main-area">
          {/* 上传区域（无活跃会话时显示） */}
          {showUpload && (
            <UploadZone onParseResult={handleParseResult} onError={handleError} />
          )}

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
              {activeSessionId && sessionRestored && (
                <div className="chat-section">
                  <ChatPanel
                    key={activeSessionId}
                    sessionId={activeSessionId}
                    initialMessages={preloadedMessages}
                  />
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
