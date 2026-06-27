/**
 * TypeScript 类型定义 —— 认证（Auth）
 *
 * 镜像后端 auth 路由的响应结构。token 仅在登录/注册响应中返回，
 * 后续持久化在 localStorage（key: bmc_auth_token）中。
 */

/** 当前登录用户（不含 token） */
export interface AuthUser {
  user_id: number;
  username: string;
}

/** 登录 / 注册成功响应（含 token） */
export interface AuthResponse extends AuthUser {
  token: string;
}
