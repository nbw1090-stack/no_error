/**
 * TypeScript 类型定义 —— AST 语法分析（SSE 事件 + 结果）
 *
 * 与现有 StreamEvent / ParseStreamEvent 流式联合类型保持独立（不要合并）。
 * 镜像后端 /api/ast/* 路由的响应结构。
 */

/**
 * AST 分析 SSE 事件联合类型。
 *
 * 事件序列（正常）：plan → progress* → component_result* → summary → done
 * 异常：当所选组件中无任何有效项时，后端发出 error 而非 plan（已有结果不会被清除）。
 */
export type AstAnalyzeEvent =
  | {
      type: 'plan';
      added: string[];
      check: string[];
      invalid: string[];
    }
  | {
      type: 'progress';
      stage: 'cloning' | 'parsing';
      component: string;
    }
  | {
      type: 'component_result';
      component: string;
      action: 'added' | 'updated' | 'unchanged' | 'error';
      commit: string | null;
      files: number;
      symbols: number;
      error: string | null;
    }
  | {
      type: 'summary';
      stats: {
        added: number;
        updated: number;
        unchanged: number;
        error: number;
        components_total: number;
        files_total: number;
        symbols_total: number;
      };
      last_analyzed_at: string;
    }
  | { type: 'done' }
  | { type: 'error'; message: string; invalid: string[] };

/** 单个已分析组件的摘要 */
export interface AstComponentSummary {
  component: string;
  git_url: string;
  branch: string;
  commit_sha: string;
  analyzed_at: string;
  file_count: number;
  symbol_count: number;
}

/** GET /api/ast/result 返回的完整分析结果 */
export interface AstResult {
  user_id: number;
  last_analyzed_at: string | null;
  components: AstComponentSummary[];
  stats: {
    components: number;
    files: number;
    symbols: number;
    by_language: Record<string, number>;
  };
}
