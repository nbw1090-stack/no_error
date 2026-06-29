/**
 * WikiPanel 组件 —— openUBMC LLM Wiki（知识库）状态与编译面板
 *
 * 采用 Karpathy「LLM Wiki」模式：点「编译」后，后端克隆 openUBMC 文档 → 让 LLM 把精选
 * 架构文档**逐篇提炼**成结构化互链 markdown 页 → 生成索引页。本面板展示编译进度
 * （逐页 compiled/reused/failed）、最终状态（模型/commit/页数/编译时间）与已编译页清单。
 *
 * wiki 是全局共享的只读知识库：任一登录用户编译后，所有用户的 agent 在日志分析 / 问答时
 * 都能据此结合 BMC 架构文档与源码作答。
 *
 * 样式复用 ComponentsPanel.css（WattVision 深色主题），个别细节走 WikiPanel.css。
 */

import { useEffect, useRef, useState } from 'react';
import type { WikiStatus, WikiSyncEvent } from '../types/wiki';
import { getWikiStatus, syncWikiStream } from '../api/client';
import './ComponentsPanel.css';
import './WikiPanel.css';

interface WikiPanelProps {
  /** 是否已登录（控制编译按钮可用性）。 */
  authed?: boolean;
}

const STAGE_LABEL: Record<string, string> = {
  cloning: '正在克隆 openUBMC 文档…',
  planning: '正在规划精选源文档…',
  indexing: '正在互链并生成索引页…',
};

const SECTION_LABEL: Record<string, string> = {
  design_reference: '架构与设计',
  api: '接口（API）',
  quick_start: '快速上手',
  overview: '总览',
};

interface CompileProgress {
  total: number;
  done: number;
  compiled: number;
  reused: number;
  failed: number;
  current: string | null;
}

const EMPTY_PROGRESS: CompileProgress = {
  total: 0,
  done: 0,
  compiled: 0,
  reused: 0,
  failed: 0,
  current: null,
};

export default function WikiPanel({ authed = false }: WikiPanelProps) {
  const [status, setStatus] = useState<WikiStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [compiling, setCompiling] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [progress, setProgress] = useState<CompileProgress>(EMPTY_PROGRESS);
  const [syncError, setSyncError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function refreshStatus() {
    try {
      setStatus(await getWikiStatus());
    } catch {
      // 状态拉取失败不阻塞面板
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refreshStatus();
  }, []);

  async function handleCompile() {
    if (compiling) return;
    setCompiling(true);
    setStage(null);
    setSyncError(null);
    setProgress(EMPTY_PROGRESS);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      for await (const ev of syncWikiStream(controller.signal) as AsyncGenerator<WikiSyncEvent>) {
        switch (ev.type) {
          case 'plan':
            setStage('准备从 ' + ev.git_url + ' 编译…');
            break;
          case 'progress':
            setStage(STAGE_LABEL[ev.stage] || ev.stage);
            break;
          case 'plan_done':
            setProgress((p) => ({ ...p, total: ev.total }));
            setStage(`共 ${ev.total} 篇精选源文档，开始逐页提炼…`);
            break;
          case 'page':
            setProgress((p) => ({
              total: ev.total,
              done: p.done + 1,
              compiled: p.compiled + (ev.status === 'compiled' ? 1 : 0),
              reused: p.reused + (ev.status === 'reused' ? 1 : 0),
              failed: p.failed + (ev.status === 'failed' ? 1 : 0),
              current: ev.title,
            }));
            break;
          case 'summary':
            void refreshStatus();
            break;
          case 'done':
            setStage(null);
            break;
          case 'error':
            setSyncError(ev.message || '编译失败');
            break;
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setSyncError(e instanceof Error ? e.message : '编译失败，请重试');
      }
    } finally {
      setCompiling(false);
      setStage(null);
      abortRef.current = null;
    }
  }

  function handleStop() {
    abortRef.current?.abort();
  }

  const meta = status?.meta ?? null;
  const indexed = !!status?.indexed;
  const pages = status?.pages ?? [];

  // 按 section 分组已编译页
  const grouped: Record<string, typeof pages> = {};
  for (const p of pages) {
    (grouped[p.section] ||= []).push(p);
  }

  return (
    <section className="components-panel">
      {/* ---- 头部 ---- */}
      <div className="components-panel-header">
        <h2 className="components-panel-title">Wiki 知识库</h2>
        {authed ? (
          <div className="wiki-header-actions">
            <button
              className="component-btn primary"
              onClick={() => void handleCompile()}
              disabled={compiling}
            >
              {compiling ? '编译中…' : indexed ? '重新编译' : '用 LLM 编译知识库'}
            </button>
            {compiling && (
              <button className="component-btn danger" onClick={handleStop}>
                停止
              </button>
            )}
          </div>
        ) : (
          <span className="ast-hint">请先登录后再编译</span>
        )}
      </div>

      <p className="wiki-intro">
        采用 LLM Wiki 模式：让 LLM 把 openUBMC 官方文档（gitcode openUBMC/docs）的精选架构内容
        <b>提炼</b>成结构化、互相链接的知识页（全局共享）。编译后，agent 在日志分析与问答时会
        按「索引页 → 整页」导航这些知识，结合 BMC 架构与源码作答。
      </p>

      {/* ---- 编译进度 / 错误 ---- */}
      {stage && <div className="ast-stage">{stage}</div>}
      {(compiling || progress.done > 0) && progress.total > 0 && (
        <div className="ast-stage">
          已提炼 {progress.done} / {progress.total}
          ｜新编译 <b>{progress.compiled}</b>
          ｜复用 <b>{progress.reused}</b>
          ｜失败 <b>{progress.failed}</b>
          {progress.current ? `｜当前：${progress.current}` : ''}
        </div>
      )}
      {syncError && (
        <div className="ast-analyze-error" role="alert">
          {syncError}
        </div>
      )}

      {/* ---- 索引状态 ---- */}
      <div className="ast-stored">
        <h3 className="ast-stored-title">
          知识库状态
          <span
            className={`wiki-badge ${indexed ? 'on' : 'off'}`}
            title={indexed ? '已编译可用' : '尚未编译'}
          >
            {indexed ? '已就绪' : '未编译'}
          </span>
        </h3>

        {loading ? (
          <p className="ast-empty">加载中…</p>
        ) : !indexed || !meta ? (
          <p className="ast-empty">
            尚未编译 wiki{authed ? '，点击右上角“用 LLM 编译知识库”开始' : ''}
          </p>
        ) : (
          <>
            <div className="wiki-meta-grid">
              <div className="wiki-meta-item">
                <span className="wiki-meta-label">知识页数</span>
                <span className="wiki-meta-value">{meta.page_count}</span>
              </div>
              <div className="wiki-meta-item">
                <span className="wiki-meta-label">源文档数</span>
                <span className="wiki-meta-value">{meta.source_count}</span>
              </div>
              <div className="wiki-meta-item">
                <span className="wiki-meta-label">编译模型</span>
                <span className="wiki-meta-value cell-mono">{meta.model || '—'}</span>
              </div>
              <div className="wiki-meta-item">
                <span className="wiki-meta-label">commit</span>
                <span className="wiki-meta-value cell-mono">
                  {meta.commit_sha ? meta.commit_sha.slice(0, 8) : '—'}
                </span>
              </div>
              <div className="wiki-meta-item">
                <span className="wiki-meta-label">最近编译</span>
                <span className="wiki-meta-value">
                  {meta.compiled_at
                    ? new Date(meta.compiled_at).toLocaleString()
                    : '—'}
                </span>
              </div>
              <div className="wiki-meta-item wiki-meta-wide">
                <span className="wiki-meta-label">来源</span>
                <span className="wiki-meta-value cell-mono">{meta.git_url}</span>
              </div>
            </div>

            {/* 已编译页清单（按 section 分组） */}
            {pages.length > 0 && (
              <div className="wiki-pages">
                {Object.keys(grouped)
                  .sort()
                  .map((section) => (
                    <div key={section} className="wiki-section">
                      <h4 className="wiki-section-title">
                        {SECTION_LABEL[section] || section}
                        <span className="wiki-section-count">
                          {grouped[section].length}
                        </span>
                      </h4>
                      <ul className="wiki-page-list">
                        {grouped[section].map((p) => (
                          <li key={p.slug} className="wiki-page-item">
                            <span className="wiki-page-title">{p.title}</span>
                            {p.description && (
                              <span className="wiki-page-desc">{p.description}</span>
                            )}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
