/**
 * TypeScript 类型定义 —— LLM Wiki（编译 SSE 事件 + 状态）
 *
 * 与其它流式联合类型保持独立。镜像后端 /api/wiki/* 路由（Karpathy「LLM Wiki」模式：
 * LLM 把 openUBMC 文档提炼成结构化互链 markdown 页，全局共享）。
 */

/**
 * Wiki 编译 SSE 事件联合类型。
 *
 * 事件序列（正常）：plan → progress(cloning) → progress(planning) → plan_done
 *   → page* → progress(indexing) → summary → done
 * 异常：clone/planning/compiling/init 阶段失败发出 error（旧 wiki 不受影响）。
 */
export type WikiSyncEvent =
  | { type: 'plan'; git_url: string; branch: string }
  | { type: 'plan_done'; total: number }
  | { type: 'progress'; stage: 'cloning' | 'planning' | 'indexing' }
  | {
      type: 'page';
      status: 'compiled' | 'reused' | 'failed';
      index: number;
      total: number;
      slug: string;
      title: string;
      error?: string;
    }
  | {
      type: 'summary';
      git_url: string;
      branch: string;
      commit: string;
      model: string;
      pages: number;
      compiled: number;
      reused: number;
      failed: number;
      compiled_at: string;
    }
  | { type: 'done' }
  | { type: 'error'; stage?: string; message: string };

/** wiki 编译元信息（GET /api/wiki/status 的 meta 字段） */
export interface WikiMeta {
  git_url: string;
  branch: string;
  commit_sha: string;
  model: string;
  compiled_at: string;
  page_count: number;
  source_count: number;
}

/** 单页摘要（status / index 返回的 pages 项） */
export interface WikiPageInfo {
  slug: string;
  title: string;
  description: string;
  section: string;
}

/** GET /api/wiki/status 返回 */
export interface WikiStatus {
  indexed: boolean;
  meta: WikiMeta | null;
  pages?: WikiPageInfo[];
}
