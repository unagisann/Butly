// UI 文字列は最初から key 管理する（§11.3）。Phase 1 は ja のみ実装し、
// en は locale 対応を入れる Phase で埋める。ライブラリ導入もその時に判断する。

const ja = {
  "app.title": "Butly",
  "backend.starting": "バックエンドを起動しています…",
  "backend.ready": "接続済み",
  "backend.unavailable": "バックエンドに接続できません",
  "backend.crashed": "バックエンドが停止しました",
  "backend.version_mismatch": "バックエンドのバージョンが一致しません",
  "backend.restart": "再起動",
  "backend.backend_version": "Backend",
  "backend.api_version": "API",
} as const;

export type StringKey = keyof typeof ja;

export function t(key: StringKey): string {
  return ja[key];
}
