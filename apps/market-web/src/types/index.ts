/**
 * 领域模型类型定义。
 *
 * 这些接口描述市场 API 返回的核心实体形状，供 Pinia store 与组件共享。
 * 字段以后端 `/v1/*` 端点实际返回为准；可选字段按需标注。
 */

/** 插件分类（市场约定值，未匹配时归一化为 `other`）。 */
export type PluginCategory =
  | "ai_tools"
  | "entertainment"
  | "integrations"
  | "productivity"
  | "utilities"
  | "other";

/** 插件排序键。 */
export type PluginSortBy = "default" | "stars" | "likes" | "comments" | "updated" | "random";

export type SortDirection = "asc" | "desc";

export type ThemeMode = "system" | "light" | "dark";

/** 用户角色。 */
export type UserRole = "core_admin" | "admin" | "user";

/** 插件状态。 */
export type PluginStatus = "pending" | "listed" | "unlisted";

/** 从 API 原始响应归一化后的插件条目。 */
export interface Plugin {
  id: number | string;
  name: string;
  display_name: string;
  version: string;
  logo: string;
  desc?: string;
  short_desc?: string;
  author?: string;
  repo?: string;
  tags: string[];
  category: PluginCategory;
  stars: number;
  likes: number;
  comments_count: number;
  list_index: number;
  owner_user_id?: number | null;
  owner_github_login?: string;
  status?: PluginStatus;
  submission_id?: number | string;
  download_url?: string;
  astrbot_version?: string;
  support_platforms?: string[];
  updated_at?: string;
  created_at?: string;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
}

/** `/v1/plugins` 返回的原始插件对象（字段可缺失，由归一化补全）。 */
export type RawPlugin = Partial<Plugin> & Record<string, unknown>;

/** 评论节点（支持嵌套）。 */
export interface Comment {
  id: number | string;
  plugin_id?: number | string;
  user_id?: number | string;
  parent_id?: number | string | null;
  body: string;
  likes?: number;
  liked?: boolean;
  muted?: boolean;
  deleted?: boolean;
  deleted_by?: number | string | null;
  deleted_at?: string | null;
  author?: Partial<User>;
  replies?: Comment[];
  created_at?: string;
  [key: string]: unknown;
}

/** 插件详情（含评论树与点赞状态）。 */
export interface PluginDetail extends Plugin {
  comments?: Comment[];
  liked?: boolean;
  readme?: string;
}

export interface PluginReadmeContext {
  owner: string;
  repo: string;
  branch: string;
  path: string;
}

export interface PluginReadmeDocument {
  content: string;
  source_url: string;
  fetched_at: string;
  cached: boolean;
  context: PluginReadmeContext;
}

/** 通知项。 */
export interface AppNotification {
  id: number | string;
  user_id?: number | string;
  type?: string;
  title?: string;
  body?: string;
  metadata?: Record<string, unknown>;
  read?: boolean;
  created_at?: string;
  [key: string]: unknown;
}

/** 公告。 */
export interface Announcement {
  id: number | string;
  title: string;
  body?: string;
  author_user_id?: number | string;
  author?: Partial<User>;
  created_at?: string;
  [key: string]: unknown;
}

/** 当前登录用户。 */
export interface User {
  id: number | string;
  username?: string;
  internal_username?: string;
  github_login?: string;
  auth_source?: string;
  role?: UserRole;
  avatar?: string;
  avatar_url?: string;
  muted_until?: string | null;
  muted_by?: number | string | null;
  muted_reason?: string | null;
  github_email?: string;
  notification_email?: string;
  notify_plugin_review?: boolean;
  notify_comments?: boolean;
  notify_replies?: boolean;
  notify_likes?: boolean;
  notify_unlist?: boolean;
  email_notify_plugin_review?: boolean;
  email_notify_pending_review?: boolean;
  email_notify_comments?: boolean;
  email_notify_replies?: boolean;
  email_notify_likes?: boolean;
  email_notify_unlist?: boolean;
  created_at?: string;
  [key: string]: unknown;
}

/** 个人 / 管理员 API Key。 */
export interface ApiKey {
  id: number | string;
  name: string;
  user_id?: number | string;
  scopes?: string[];
  key?: string;
  preview?: string;
  created_at?: string;
  [key: string]: unknown;
}

/** 后台用户列表项。 */
export interface AdminUser extends User {
  muted_until?: string | null;
  muted_reason?: string | null;
}

/** 插件提交记录。 */
export interface Submission {
  id: number | string;
  plugin_id: number | string;
  user_id?: number | string;
  payload?: Partial<Plugin> & Record<string, unknown>;
  status?: PluginStatus;
  created_at?: string;
  [key: string]: unknown;
}

/** GitHub 仓库预取出的提交表单候选数据。 */
export interface PluginSubmissionMetadataPreview {
  repo: string;
  name?: string;
  display_name?: string;
  desc?: string;
  author?: string;
  social_link?: string;
  category?: PluginCategory | "";
  tags?: string[];
}

/** 站点认证配置。 */
export interface AuthConfig {
  github_login_enabled: boolean;
  public_login_enabled: boolean;
  login_agreement_enabled: boolean;
  login_agreement_text: string;
  service_terms_enabled: boolean;
  service_terms_text: string;
  terms_revision: string;
}

/** 市场行为配置。 */
export interface MarketConfig {
  submissions_enabled: boolean;
  comments_enabled: boolean;
  likes_enabled: boolean;
  max_plugin_tags: number;
  plugin_auto_approve_enabled?: boolean;
}

/** 公开站点配置（`/v1/site`）。 */
export interface SiteConfig {
  name: string;
  icon_url: string;
  web_url: string;
  subtitle: string;
  description: string;
  contact_email: string;
  docs_url: string;
  auth: AuthConfig;
  market: MarketConfig;
}

/** 初始化向导配置。 */
export interface SetupConfig {
  site: Partial<SiteConfig>;
  admin: { username: string; password: string };
  auth: AuthConfig;
  github: {
    client_id: string;
    client_secret: string;
    callback_url: string;
    scope: string;
    admin_org: string;
  };
  market: MarketConfig & {
    api_token: string;
    api_token_configured: boolean;
    api_token_previews: string[];
    api_token_statuses: {
      token: string;
      disabled: boolean;
      status: string;
      error_code?: number | null;
      error_message?: string;
      retry_after_seconds?: number;
      reset_at?: string;
      checked_at?: string;
    }[];
    api_token_remove_indexes: number[];
    metadata_sync_enabled: boolean;
    metadata_sync_interval_seconds: number;
  };
  email: {
    provider: string;
    smtp: SmtpConfig;
    cloudflare: CloudflareEmailConfig;
    daily_limit: number;
    verification_daily_limit_per_user: number;
  };
  postgres: DbHostConfig;
  redis: DbHostConfig;
}

export interface SmtpConfig {
  host: string;
  port: number;
  username: string;
  password: string;
  from_address: string;
  from_name: string;
  ssl: boolean;
  encryption: string;
  auth_method: string;
  validate_certs: boolean;
}

export interface CloudflareEmailConfig {
  account_id: string;
  api_token: string;
  from_address: string;
  from_name: string;
}

export interface DbHostConfig {
  host: string;
  port: number;
  database: number | string;
  username: string;
  password: string;
  ssl: boolean;
}

/** Setup 状态（`/v1/setup/status`）。 */
export interface SetupStatus {
  required: boolean;
  missing: string[];
  database_configured: boolean;
  redis_configured: boolean;
  saved_setup: SetupConfig;
  restart_required: boolean;
}

/** 分页查询选项。 */
export interface PageOptions {
  page?: number;
  pageSize?: number;
}

/** 分页结果。 */
export interface PaginatedResult<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  limit: number;
  offset: number;
  unread_count?: number;
}

/** 通用 API 错误响应。 */
export interface ApiError {
  error?: string;
  detail?: string;
  [key: string]: unknown;
}

/** 帮助文档章节。 */
export interface HelpSection {
  title: string;
  content: string;
}

export interface HelpContent {
  title: string;
  sections: HelpSection[];
}
