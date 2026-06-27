/**
 * ComponentsPanel 组件 —— 组件注册表管理面板
 *
 * 功能：
 * - 列出所有已注册组件（名称 / Git 地址 / 分支 / 是否可用 / 操作）
 * - 是否可用：只读状态徽章，反映该组件「是否已进行 AST 分析」
 *   （可用 = 已分析；不可用 = 暂未被 AST 分析）
 * - 更新：对该组件单独发起 AST 语法分析（成功后置为可用）
 * - 删除：删除该组件的 AST 分析结果（二次确认 ✓/✗），
 *   组件本身保留在列表中，仅标记为不可用
 * - 添加组件：顶部按钮打开内联表单，调用 createComponent
 * - 批量 AST 分析：多选组件后一键发起增量分析（SSE 流）
 *
 * 样式遵循 WattVision 深色主题（详见 App.css 的 CSS 变量）。
 */

import { useEffect, useRef, useState } from 'react';
import type { ComponentInfo } from '../types/component';
import type { AstAnalyzeEvent, AstResult } from '../types/ast';
import {
  listComponents,
  createComponent,
  deleteAstAnalysis,
  getAstResult,
  analyzeAstStream,
} from '../api/client';
import './ComponentsPanel.css';

/** AST component_result 行的展示形态（合并事件流后用于渲染）。 */
interface ComponentFeedItem {
  component: string;
  action: 'added' | 'updated' | 'unchanged' | 'error';
  commit: string | null;
  files: number;
  symbols: number;
  error: string | null;
}

/** AST 分析 summary 统计（沿用后端字段名）。 */
type AstSummaryStats = Extract<AstAnalyzeEvent, { type: 'summary' }>['stats'];

/** 添加表单字段（enabled = 是否已分析，由 AST 流程维护，不在此录入） */
interface AddForm {
  name: string;
  git_url: string;
  branch: string;
}

const EMPTY_ADD: AddForm = { name: '', git_url: '', branch: 'main' };

interface ComponentsPanelProps {
  /** 是否已登录（控制 AST 分析按钮可用性）。 */
  authed?: boolean;
}

const STAGE_LABEL: Record<string, string> = {
  cloning: '正在克隆',
  parsing: '正在解析',
  removing: '正在移除',
};

const ACTION_LABEL: Record<ComponentFeedItem['action'], string> = {
  added: '新增',
  updated: '更新',
  unchanged: '未变',
  error: '失败',
};

export default function ComponentsPanel({ authed = false }: ComponentsPanelProps) {
  const [components, setComponents] = useState<ComponentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // ---- 添加表单 ----
  const [showAdd, setShowAdd] = useState(false);
  const [addForm, setAddForm] = useState<AddForm>(EMPTY_ADD);
  const [addError, setAddError] = useState<string | null>(null);

  // ---- 删除确认 + 行内错误 ----
  const [confirmName, setConfirmName] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  // ---- 多选（AST 分析目标） ----
  const [selectedNames, setSelectedNames] = useState<Set<string>>(new Set());
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  // ---- AST 分析状态 ----
  const [analyzing, setAnalyzing] = useState(false);
  const [feedItems, setFeedItems] = useState<ComponentFeedItem[]>([]);
  const [runningStage, setRunningStage] = useState<string | null>(null);
  const [summaryStats, setSummaryStats] = useState<AstSummaryStats | null>(null);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [result, setResult] = useState<AstResult | null>(null);
  const analyzeAbortRef = useRef<AbortController | null>(null);

  /** 全选复选框的 indeterminate 状态：部分选中时为 true。 */
  const allNames = components.map((c) => c.name);
  const selectedCount = selectedNames.size;
  const totalCount = allNames.length;
  const allSelected = totalCount > 0 && selectedCount === totalCount;
  const someSelected = selectedCount > 0 && selectedCount < totalCount;

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someSelected;
    }
  }, [someSelected]);

  /** 拉取组件列表（挂载时 + 每次 AST 变动后刷新，使「是否可用」徽标即时更新）。 */
  async function refreshComponents() {
    try {
      const list = await listComponents();
      setComponents(list);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : '加载组件失败');
    } finally {
      setLoading(false);
    }
  }

  /** 初始加载组件列表 */
  useEffect(() => {
    let cancelled = false;
    void refreshComponents().then(() => {
      if (cancelled) return;
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 拉取已存储的 AST 分析结果（挂载时 + 每次分析完成后刷新）。 */
  async function refreshAstResult() {
    try {
      const res = await getAstResult();
      setResult(res);
    } catch {
      // 拉取失败不阻塞主面板
    }
  }

  useEffect(() => {
    void refreshAstResult();
  }, []);

  /** 全选 / 取消全选（点击表头复选框）。 */
  function toggleSelectAll(checked: boolean) {
    setSelectedNames(checked ? new Set(allNames) : new Set());
  }

  /** 切换某行选中。 */
  function toggleSelect(name: string, checked: boolean) {
    setSelectedNames((prev) => {
      const next = new Set(prev);
      if (checked) next.add(name);
      else next.delete(name);
      return next;
    });
  }

  /** 发起 AST 分析（批量或单个）：消费 SSE 事件，更新进度面板与最终统计。返回分析成功的组件名集合。 */
  async function runAnalysis(names: string[]): Promise<Set<string>> {
    if (names.length === 0) return new Set();

    setAnalyzing(true);
    setFeedItems([]);
    setRunningStage(null);
    setSummaryStats(null);
    setAnalyzeError(null);

    const controller = new AbortController();
    analyzeAbortRef.current = controller;
    const succeeded = new Set<string>();

    try {
      for await (const ev of analyzeAstStream(names, controller.signal)) {
        switch (ev.type) {
          case 'plan':
            // plan 仅作为信号：invalid 项可提示，但不阻塞
            break;
          case 'progress':
            setRunningStage(`${STAGE_LABEL[ev.stage] || ev.stage} ${ev.component}…`);
            break;
          case 'component_result':
            setFeedItems((prev) => [
              ...prev,
              {
                component: ev.component,
                action: ev.action,
                commit: ev.commit,
                files: ev.files,
                symbols: ev.symbols,
                error: ev.error,
              },
            ]);
            // 记录分析成功（非 error）的组件，供「更新」流程标记为可用
            if (ev.action !== 'error') succeeded.add(ev.component);
            break;
          case 'summary':
            setSummaryStats(ev.stats);
            void refreshAstResult();
            // 刷新组件列表，使「是否可用」徽标反映后端维护的 enabled
            void refreshComponents();
            break;
          case 'done':
            break;
          case 'error':
            setAnalyzeError(ev.message || '分析失败');
            break;
        }
      }
    } catch (e) {
      // 用户主动停止：静默
      if ((e as Error).name === 'AbortError') {
        // no-op
      } else {
        setAnalyzeError(e instanceof Error ? e.message : '分析失败，请重试');
      }
    } finally {
      setAnalyzing(false);
      setRunningStage(null);
      analyzeAbortRef.current = null;
    }

    return succeeded;
  }

  /** 停止当前分析。 */
  function handleStopAnalyze() {
    analyzeAbortRef.current?.abort();
  }

  /**
   * 「更新」操作：对该组件单独发起 AST 语法分析。
   * 分析成功后由后端把该组件标记为可用（enabled=true），runAnalysis 的 summary
   * 分支会触发 refreshComponents 使「是否可用」徽标即时刷新。
   */
  async function handleUpdateAnalyze(comp: ComponentInfo) {
    if (analyzing) return;
    setRowErrors((prev) => {
      const next = { ...prev };
      delete next[comp.name];
      return next;
    });

    await runAnalysis([comp.name]);
  }

  /**
   * 「删除」操作：删除该组件的 AST 分析结果。
   * 组件本身保留在列表中，仅乐观地置为不可用（enabled=false），表示暂未被分析。
   */
  async function handleDeleteAst(comp: ComponentInfo) {
    const prev = components;
    setComponents((prevComps) =>
      prevComps.map((c) => (c.name === comp.name ? { ...c, enabled: false } : c)),
    );
    setConfirmName(null);
    try {
      const res = await deleteAstAnalysis(comp.name);
      // 用后端返回的最新 registry 状态对齐
      if (res?.component) {
        setComponents((prevComps) =>
          prevComps.map((c) =>
            c.name === comp.name ? { ...c, enabled: res.component.enabled } : c,
          ),
        );
      }
      void refreshAstResult();
    } catch (e) {
      // 回滚
      setComponents(prev);
      setRowErrors((prevErr) => ({
        ...prevErr,
        [comp.name]: e instanceof Error ? e.message : '删除 AST 分析失败',
      }));
    }
  }

  /** 提交添加 */
  async function handleAdd() {
    setAddError(null);
    if (!addForm.name.trim()) {
      setAddError('请输入组件名称');
      return;
    }
    try {
      const created = await createComponent(addForm);
      setComponents((prev) => [created, ...prev]);
      setAddForm(EMPTY_ADD);
      setShowAdd(false);
    } catch (e) {
      setAddError(e instanceof Error ? e.message : '添加失败');
    }
  }

  function cancelAdd() {
    setShowAdd(false);
    setAddForm(EMPTY_ADD);
    setAddError(null);
  }

  return (
    <section className="components-panel">
      {/* ---- 头部 ---- */}
      <div className="components-panel-header">
        <h2 className="components-panel-title">组件管理</h2>
        {!showAdd && (
          <button className="add-component-btn" onClick={() => setShowAdd(true)}>
            <span className="add-component-icon">+</span>
            添加组件
          </button>
        )}
      </div>

      {/* ---- 添加表单 ---- */}
      {showAdd && (
        <div className="component-add-form">
          <div className="add-form-row">
            <label className="add-field">
              <span className="add-field-label">组件名称</span>
              <input
                className="component-input"
                type="text"
                value={addForm.name}
                placeholder="如 pcie_device"
                onChange={(e) => setAddForm({ ...addForm, name: e.target.value })}
              />
            </label>
            <label className="add-field add-field-grow">
              <span className="add-field-label">Git 地址</span>
              <input
                className="component-input component-input-mono"
                type="text"
                value={addForm.git_url}
                placeholder="https://github.com/org/repo.git"
                onChange={(e) => setAddForm({ ...addForm, git_url: e.target.value })}
              />
            </label>
            <label className="add-field">
              <span className="add-field-label">分支</span>
              <input
                className="component-input component-input-mono"
                type="text"
                value={addForm.branch}
                placeholder="main"
                onChange={(e) => setAddForm({ ...addForm, branch: e.target.value })}
              />
            </label>
          </div>
          {addError && <div className="add-form-error">{addError}</div>}
          <div className="add-form-actions">
            <button className="component-btn primary" onClick={handleAdd}>
              创建
            </button>
            <button className="component-btn" onClick={cancelAdd}>
              取消
            </button>
          </div>
        </div>
      )}

      {/* ---- AST 分析操作栏 ---- */}
      {!loading && !loadError && totalCount > 0 && (
        <div className="ast-actionbar">
          <span className="ast-counter">
            已选 {selectedCount} / 共 {totalCount}
          </span>
          <div className="ast-actionbar-spacer" />
          {!authed ? (
            <span className="ast-hint">请先登录后再分析</span>
          ) : (
            <button
              className="component-btn primary"
              onClick={() => void runAnalysis([...selectedNames])}
              disabled={selectedCount === 0 || analyzing}
            >
              {analyzing ? '分析中…' : '开始 AST 语法分析'}
            </button>
          )}
          {analyzing && (
            <button className="component-btn danger" onClick={handleStopAnalyze}>
              停止
            </button>
          )}
        </div>
      )}

      {/* ---- 分析中错误提示 ---- */}
      {analyzeError && (
        <div className="ast-analyze-error" role="alert">
          {analyzeError}
        </div>
      )}

      {/* ---- 分析进度面板（分析中 / 分析完成） ---- */}
      {(analyzing || runningStage || feedItems.length > 0 || summaryStats) && (
        <div className="ast-progress">
          {runningStage && <div className="ast-stage">{runningStage}</div>}
          {feedItems.length > 0 && (
            <table className="components-table ast-results-table">
              <thead>
                <tr>
                  <th>组件</th>
                  <th>结果</th>
                  <th>commit</th>
                  <th>文件数</th>
                  <th>符号数</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {feedItems.map((item) => (
                  <tr key={item.component} className="components-row">
                    <td className="cell-name">{item.component}</td>
                    <td>
                      <span className={`ast-badge ${item.action}`}>
                        {ACTION_LABEL[item.action]}
                      </span>
                    </td>
                    <td>
                      <span className="cell-mono">
                        {item.commit ?? '—'}
                      </span>
                    </td>
                    <td>{item.files}</td>
                    <td>{item.symbols}</td>
                    <td>{item.error ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {summaryStats && (
            <div className="ast-summary">
              <span className="ast-summary-item">
                新增 <b>{summaryStats.added}</b>
              </span>
              <span className="ast-summary-item">
                更新 <b>{summaryStats.updated}</b>
              </span>
              <span className="ast-summary-item">
                未变 <b>{summaryStats.unchanged}</b>
              </span>
              <span className="ast-summary-item">
                移除 <b>{summaryStats.removed}</b>
              </span>
              <span className="ast-summary-item">
                失败 <b>{summaryStats.error}</b>
              </span>
              <span className="ast-summary-divider" />
              <span className="ast-summary-item">
                总组件 <b>{summaryStats.components_total}</b>
              </span>
              <span className="ast-summary-item">
                文件 <b>{summaryStats.files_total}</b>
              </span>
              <span className="ast-summary-item">
                符号 <b>{summaryStats.symbols_total}</b>
              </span>
            </div>
          )}
        </div>
      )}

      {/* ---- 已存储的 AST 分析结果 ---- */}
      <div className="ast-stored">
        <h3 className="ast-stored-title">
          AST 分析结果
          {result?.last_analyzed_at && (
            <span className="ast-stored-time">
              最近：{new Date(result.last_analyzed_at).toLocaleString()}
            </span>
          )}
        </h3>
        {result === null ? (
          <p className="ast-empty">尚未进行 AST 分析</p>
        ) : result.components.length === 0 ? (
          <p className="ast-empty">尚无分析结果</p>
        ) : (
          <>
            <table className="components-table ast-results-table">
              <thead>
                <tr>
                  <th>组件</th>
                  <th>分支</th>
                  <th>commit</th>
                  <th>文件数</th>
                  <th>符号数</th>
                </tr>
              </thead>
              <tbody>
                {result.components.map((c) => (
                  <tr key={c.component} className="components-row">
                    <td className="cell-name">{c.component}</td>
                    <td>
                      <span className="cell-mono">{c.branch}</span>
                    </td>
                    <td>
                      <span className="cell-mono">{c.commit_sha}</span>
                    </td>
                    <td>{c.file_count}</td>
                    <td>{c.symbol_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {Object.keys(result.stats.by_language).length > 0 && (
              <div className="ast-langs">
                {Object.entries(result.stats.by_language).map(([lang, n]) => (
                  <span key={lang} className="ast-lang-badge">
                    {lang} {n}
                  </span>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {/* ---- 加载状态 ---- */}
      {loading ? (
        <div className="components-panel-state">
          <div className="spinner" />
          <p>加载中...</p>
        </div>
      ) : loadError ? (
        <div className="components-panel-state components-panel-error">
          <p>{loadError}</p>
        </div>
      ) : components.length === 0 ? (
        <div className="components-panel-state">
          <p>暂无注册组件</p>
          <p className="components-panel-hint">点击右上角“添加组件”开始注册</p>
        </div>
      ) : (
        <div className="components-table-wrap">
          <table className="components-table">
            <thead>
              <tr>
                <th className="cell-select-all">
                  <input
                    ref={selectAllRef}
                    type="checkbox"
                    className="row-check select-all-check"
                    checked={allSelected}
                    onChange={(e) => toggleSelectAll(e.target.checked)}
                    aria-label="全选"
                  />
                </th>
                <th>组件名称</th>
                <th>Git 地址</th>
                <th>分支</th>
                <th>是否可用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {components.map((comp) => {
                const isConfirming = confirmName === comp.name;
                const rowError = rowErrors[comp.name];

                return (
                  <tr key={comp.name} className="components-row">
                    <td>
                      <input
                        type="checkbox"
                        className="row-check"
                        checked={selectedNames.has(comp.name)}
                        onChange={(e) => toggleSelect(comp.name, e.target.checked)}
                        aria-label={`选择 ${comp.name}`}
                      />
                    </td>
                    <td className="cell-name">{comp.name}</td>
                    <td>
                      <span className="cell-mono">{comp.git_url}</span>
                    </td>
                    <td>
                      <span className="cell-mono">{comp.branch}</span>
                    </td>
                    <td>
                      {/* 只读状态徽章：可用 = 已进行 AST 分析；不可用 = 暂未被分析 */}
                      <span
                        className={`toggle-badge static ${comp.enabled ? 'on' : 'off'}`}
                        title={
                          comp.enabled ? '已进行 AST 分析' : '暂未被 AST 分析'
                        }
                      >
                        <span className="toggle-knob" />
                        <span className="toggle-text">
                          {comp.enabled ? '可用' : '不可用'}
                        </span>
                      </span>
                      {rowError && <span className="row-inline-error">{rowError}</span>}
                    </td>
                    <td className="cell-actions">
                      {isConfirming ? (
                        <span className="delete-confirm">
                          <span className="confirm-text">删除分析?</span>
                          <span
                            className="confirm-yes"
                            onClick={() => handleDeleteAst(comp)}
                            title="确认删除 AST 分析"
                          >
                            ✓
                          </span>
                          <span
                            className="confirm-no"
                            onClick={() => setConfirmName(null)}
                            title="取消"
                          >
                            ✕
                          </span>
                        </span>
                      ) : (
                        <>
                          <button
                            className="component-btn small primary"
                            onClick={() => handleUpdateAnalyze(comp)}
                            disabled={analyzing || !authed}
                            title={
                              !authed
                                ? '请先登录'
                                : '更新此组件的 AST 语法分析'
                            }
                          >
                            {analyzing ? '分析中…' : '更新'}
                          </button>
                          <button
                            className="component-btn small danger"
                            onClick={() => setConfirmName(comp.name)}
                            disabled={analyzing}
                            title="删除此组件的 AST 分析（组件保留，标记为不可用）"
                          >
                            删除
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
