/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Tauri 規約の dev port 1420 を固定で使う。
//
// browser dev（Tauri を使わず素の browser で開く開発モード）では、手動起動した
// sidecar を `BUTLY_DEV_BACKEND_URL` で指定する。`/api` を dev server が proxy
// するので:
// - browser から見ると同一 origin になり、CORS も IPv4/IPv6 の食い違いも起きない
// - 遠隔の Pi で動かす場合も SSH port forward は 1420 の 1 本で済む
// 手順は docs/guides/desktop_dev_setup.ja.md。
export default defineConfig(({ mode }) => {
  // prefix "" で VITE_ 以外も読む。この値は proxy 先であって client には渡さない
  // （token を持つ URL を bundle へ inline させないため、意図的に非 VITE_）。
  // 基準は cwd ではなくこの設定ファイルの場所。repo root など別の場所から
  // 起動されても .env を取り違えず、proxy が無言で無効にならない。
  const env = loadEnv(mode, import.meta.dirname, "");
  const backendUrl = env.BUTLY_DEV_BACKEND_URL?.trim();

  return {
    plugins: [react()],
    clearScreen: false,
    server: {
      // `ssh -L` の転送先と食い違わないよう IPv4 loopback に固定する。既定の
      // localhost は環境によって ::1 に解決され、転送が繋がらないことがある。
      host: "127.0.0.1",
      port: 1420,
      strictPort: true,
      proxy: backendUrl
        ? {
            "/api": {
              target: backendUrl,
              changeOrigin: false,
            },
          }
        : undefined,
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
    },
  };
});
