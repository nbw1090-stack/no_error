/**
 * TypeScript 类型定义 —— 组件注册表
 *
 * 镜像后端 snake_case 的 Component 对象。
 */

/** 组件信息（注册表中的一条记录） */
export interface ComponentInfo {
  name: string;
  git_url: string;
  branch: string;
  enabled: boolean;
}
