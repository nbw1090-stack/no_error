/**
 * LogTable 组件 —— 日志表格（虚拟滚动 + 分页按需加载）
 *
 * - 虚拟滚动：仅渲染可视区域 + overscan 内的行，DOM 节点恒定。
 * - 分页按需加载：日志条目经 /api/datasets/{id}/entries 分页获取，
 *   首屏 PAGE_SIZE 条，滚动接近已加载底部时预取下一批 —— 不再一次性
 *   传输/驻留全量 entries。
 *
 * 滚动条高度反映筛选后总数（filteredTotal，非已加载数），
 * 加载更多只是把底部占位换成真实行，scrollTop 不跳。
 *
 * entries 拉取下沉到本组件内部，App 只传 datasetId + filters + total(占位)。
 */

import { useState, useRef, useCallback, useEffect } from 'react';
import type { LogEntry, FilterState } from '../types/log';
import { fetchEntries } from '../api/client';
import LogRow from './LogRow';
import './LogTable.css';

interface LogTableProps {
  datasetId: string;
  filters: FilterState;
  /** 筛选后总数的初始占位（通常 = summary.totalLines）；首屏请求后用真实 total 覆盖 */
  total: number;
  /** 上传 progress 阶段（数据集尚未就绪）*/
  loadingHint?: boolean;
}

/** 预估行高（px），用于初始计算 */
const ROW_HEIGHT_ESTIMATE = 57;
/** 可视区域外额外渲染的行数 */
const OVERSCAN = 10;
/** 低于此阈值跳过虚拟化 */
const VIRTUALIZATION_THRESHOLD = 50;
/** 每页加载条目数 */
const PAGE_SIZE = 200;
/** 距已加载底部多少行时预取下一批 */
const PREFETCH_THRESHOLD = 30;

export default function LogTable({ datasetId, filters, total, loadingHint }: LogTableProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [containerHeight, setContainerHeight] = useState(0);
  const [measuredRowHeight, setMeasuredRowHeight] = useState(ROW_HEIGHT_ESTIMATE);

  // ---- 已加载的 entries（分页累积） ----
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  // 筛选后总数（首屏请求后由后端返回的 total 覆盖）
  const [filteredTotal, setFilteredTotal] = useState(total);
  const reqSeq = useRef(0);

  // ---- 展开行状态（提升到 LogTable 管理，避免虚拟滚动丢失状态） ----
  const [expandedRows, setExpandedRows] = useState<Set<number>>(new Set());

  const toggleExpand = useCallback((entryId: number) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(entryId)) {
        next.delete(entryId);
      } else {
        next.add(entryId);
      }
      return next;
    });
  }, []);

  const rowHeight = measuredRowHeight;

  // ---- 监听容器尺寸 ----
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    setContainerHeight(el.clientHeight);

    const observer = new ResizeObserver(([entry]) => {
      setContainerHeight(entry.contentRect.height);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // ---- 数据集 / 筛选变化：重置并拉首屏 ----
  useEffect(() => {
    const seq = ++reqSeq.current;
    setLoading(true);
    setEntries([]);
    setHasMore(true);
    setExpandedRows(new Set());
    setFilteredTotal(total);
    setScrollTop(0);
    if (containerRef.current) containerRef.current.scrollTop = 0;

    let cancelled = false;
    (async () => {
      try {
        const res = await fetchEntries(datasetId, 0, PAGE_SIZE, filters);
        if (seq !== reqSeq.current || cancelled) return;
        setEntries(res.items);
        setHasMore(res.hasMore);
        setFilteredTotal(res.total);
      } catch {
        if (seq === reqSeq.current) setEntries([]);
      } finally {
        if (seq === reqSeq.current) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [datasetId, filters, total]);

  // ---- 加载更多（滚动接近已加载底部时触发） ----
  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return;
    const seq = reqSeq.current;
    setLoadingMore(true);
    try {
      const res = await fetchEntries(datasetId, entries.length, PAGE_SIZE, filters);
      if (seq !== reqSeq.current) return; // 过期请求，丢弃
      setEntries((prev) => prev.concat(res.items));
      setHasMore(res.hasMore);
      setFilteredTotal(res.total);
    } catch {
      // 静默，下次滚动重试
    } finally {
      if (seq === reqSeq.current) setLoadingMore(false);
    }
  }, [datasetId, filters, entries.length, loadingMore, hasMore]);

  // ---- 测量实际行高（从第一个可见行获取） ----
  useEffect(() => {
    if (entries.length === 0 || !containerRef.current) return;
    const timer = requestAnimationFrame(() => {
      const firstRow = containerRef.current?.querySelector('.log-row') as HTMLElement | null;
      if (firstRow) {
        const h = firstRow.getBoundingClientRect().height;
        if (h > 0) setMeasuredRowHeight(h);
      }
    });
    return () => cancelAnimationFrame(timer);
  }, [entries]);

  // ---- 滚动处理 + 接近底部预取 ----
  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    setScrollTop(el.scrollTop);
    const loadedHeight = entries.length * rowHeight;
    if (el.scrollTop + el.clientHeight > loadedHeight - PREFETCH_THRESHOLD * rowHeight) {
      loadMore();
    }
  }, [entries.length, rowHeight, loadMore]);

  // ---- 计算可见范围（基于已加载 entries） ----
  const shouldVirtualize = entries.length >= VIRTUALIZATION_THRESHOLD;

  let visibleEntries: LogEntry[];
  let paddingTop = 0;
  let paddingBottom = 0;

  if (shouldVirtualize && containerHeight > 0) {
    const startIndex = Math.max(0, Math.floor(scrollTop / rowHeight) - OVERSCAN);
    const endIndex = Math.min(
      entries.length,
      Math.ceil((scrollTop + containerHeight) / rowHeight) + OVERSCAN,
    );

    const expandedIds = new Set(expandedRows);
    let slice = entries.slice(startIndex, endIndex);

    if (expandedIds.size > 0) {
      for (let i = 0; i < entries.length; i++) {
        if (expandedIds.has(entries[i].id) && (i < startIndex || i >= endIndex)) {
          slice.push(entries[i]);
        }
      }
      slice.sort((a, b) => a.id - b.id);
    }

    visibleEntries = slice;
    paddingTop = startIndex * rowHeight;
    // 底部 padding = 已加载未渲染 + 未加载的 total 占位
    paddingBottom = (filteredTotal - endIndex) * rowHeight;
  } else {
    visibleEntries = entries;
    paddingBottom = Math.max(0, (filteredTotal - entries.length) * rowHeight);
  }

  // ---- 上传中（数据集尚未就绪） ----
  if (loadingHint) {
    return (
      <div className="log-table-container">
        <div className="empty-state">
          <div className="spinner" />
        </div>
      </div>
    );
  }

  // ---- 首屏加载中 ----
  if (loading && entries.length === 0) {
    return (
      <div className="log-table-container">
        <div className="empty-state">
          <div className="spinner" />
          <p>正在加载日志条目...</p>
        </div>
      </div>
    );
  }

  // ---- 空状态 ----
  if (entries.length === 0) {
    return (
      <div className="log-table-container">
        <div className="empty-state">
          <p>暂无匹配的日志条目</p>
        </div>
      </div>
    );
  }

  const totalHeight = filteredTotal > 0 ? filteredTotal * rowHeight : undefined;

  return (
    <div
      ref={containerRef}
      className="log-table-container"
      onScroll={handleScroll}
    >
      <table className="log-table" style={totalHeight ? { minHeight: totalHeight } : undefined}>
        <thead>
          <tr>
            <th className="col-id">#</th>
            <th className="col-timestamp">时间戳</th>
            <th className="col-level">级别</th>
            <th className="col-component">组件</th>
            <th className="col-file">文件:行号</th>
            <th className="col-source">来源</th>
            <th className="col-message">消息</th>
          </tr>
        </thead>
        <tbody>
          {/* 顶部占位 */}
          {paddingTop > 0 && (
            <tr style={{ height: paddingTop }} aria-hidden="true" />
          )}

          {/* 可见行 */}
          {visibleEntries.map((entry) => (
            <LogRow
              key={entry.id}
              entry={entry}
              isExpanded={expandedRows.has(entry.id)}
              onToggleExpand={toggleExpand}
            />
          ))}

          {/* 底部占位（含未加载部分） */}
          {paddingBottom > 0 && (
            <tr style={{ height: paddingBottom }} aria-hidden="true" />
          )}
        </tbody>
      </table>
    </div>
  );
}
