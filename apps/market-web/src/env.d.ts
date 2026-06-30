/// <reference types="vite/client" />

declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>;
  export default component;
}

interface ImportMetaEnv {
  readonly VITE_BASE_URL?: string;
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_COMMUNITY_REPO_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
