/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * dev 限定。Tauri 外の browser 実行で使う sidecar の base URL
   * （例: `http://localhost:8000`）。未設定なら browser では backend へ接続しない。
   * production build では読まない。token は含めないこと（bundle に inline される）。
   */
  readonly VITE_BUTLY_DEV_BACKEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
