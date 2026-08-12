// Tauri lifecycle bridge。
// - backend process / token / port は Rust 側が所有する（§5.1）。
//   React は event 購読と command 呼び出しだけを行い、shell を spawn できない。
// - テストや素の browser 実行では Tauri API が無いので、interface に切り出して
//   差し替えられるようにする。
// - dev 限定で、手動起動した sidecar へ browser から直接つなぐ DevBrowserBridge
//   を選べる（Rust 側 `BUTLY_DEV_BACKEND_PORT` の browser 版）。
//   手順は docs/guides/desktop_dev_setup.ja.md。

import type { BackendState, ConnectionInfo } from "./types";

export interface LifecycleBridge {
  getBackendState(): Promise<BackendState>;
  /** ready 前は null。token は返り値の memory 保持のみ許可（localStorage 禁止）。 */
  getConnectionInfo(): Promise<ConnectionInfo | null>;
  restartBackend(): Promise<void>;
  onBackendState(handler: (state: BackendState) => void): Promise<() => void>;
}

function hasTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

class TauriBridge implements LifecycleBridge {
  async getBackendState(): Promise<BackendState> {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<BackendState>("get_backend_state");
  }

  async getConnectionInfo(): Promise<ConnectionInfo | null> {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<ConnectionInfo | null>("get_connection_info");
  }

  async restartBackend(): Promise<void> {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("restart_backend");
  }

  async onBackendState(
    handler: (state: BackendState) => void,
  ): Promise<() => void> {
    const { listen } = await import("@tauri-apps/api/event");
    return listen<BackendState>("backend-state", (event) =>
      handler(event.payload),
    );
  }
}

/** Tauri 外（vitest / 素の browser）用。backend 不明のまま unavailable を返す。 */
class NullBridge implements LifecycleBridge {
  async getBackendState(): Promise<BackendState> {
    return {
      phase: "unavailable",
      detail: "not_running_in_tauri",
    };
  }

  async getConnectionInfo(): Promise<ConnectionInfo | null> {
    return null;
  }

  async restartBackend(): Promise<void> {
    // no-op
  }

  async onBackendState(): Promise<() => void> {
    return () => {};
  }
}

const DEV_HEALTH_TIMEOUT_MS = 3_000;

/**
 * dev 限定。Tauri を使わず Vite dev server を素の browser で開くための bridge。
 *
 * - process は管理しない。`--dev-cors` で手動起動済みの sidecar を
 *   `GET /api/v1/health` で確認するだけ。restart は再 probe であって再起動ではない。
 * - token は扱わない（常に null）。`VITE_*` は bundle に inline されるため、
 *   この経路の対象は `BUTLY_DESKTOP_TOKEN` 未設定＝認証オフの loopback sidecar に限る。
 * - version 期待値の正本は Rust 側なので、version_mismatch の判定はしない。
 *   sidecar spawn / token 受け渡し / crash 復帰の確認は Tauri 実行でのみ行う。
 */
class DevBrowserBridge implements LifecycleBridge {
  private readonly handlers = new Set<(state: BackendState) => void>();

  constructor(private readonly baseUrl: string) {}

  async getBackendState(): Promise<BackendState> {
    return this.probe();
  }

  async getConnectionInfo(): Promise<ConnectionInfo | null> {
    return { baseUrl: this.baseUrl, token: null };
  }

  async restartBackend(): Promise<void> {
    const state = await this.probe();
    for (const handler of this.handlers) handler(state);
  }

  async onBackendState(
    handler: (state: BackendState) => void,
  ): Promise<() => void> {
    this.handlers.add(handler);
    return () => {
      this.handlers.delete(handler);
    };
  }

  private async probe(): Promise<BackendState> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DEV_HEALTH_TIMEOUT_MS);
    try {
      const response = await fetch(`${this.baseUrl}/api/v1/health`, {
        signal: controller.signal,
      });
      if (!response.ok) {
        return { phase: "unavailable", detail: `dev_health_http_${response.status}` };
      }
      const body = (await response.json()) as {
        backend_version?: string;
        api_version?: string;
      };
      return {
        phase: "ready",
        backendVersion: body.backend_version,
        apiVersion: body.api_version,
        detail: "dev_browser",
      };
    } catch (error) {
      const reason = error instanceof Error ? error.name : "Error";
      return { phase: "unavailable", detail: `dev_health_failed_${reason}` };
    } finally {
      clearTimeout(timer);
    }
  }
}

/** 末尾 `/` を落とす。空文字なら null（= dev browser 経路を使わない）。 */
function normalizeDevBackendUrl(raw: string | undefined): string | null {
  if (typeof raw !== "string") return null;
  const normalized = raw.trim().replace(/\/+$/, "");
  return normalized.length > 0 ? normalized : null;
}

export function createLifecycleBridge(): LifecycleBridge {
  if (hasTauri()) return new TauriBridge();
  // `import.meta.env.DEV` は build 時に定数化されるので、production bundle では
  // この block ごと（DevBrowserBridge 本体も）dead code として削除される。
  if (import.meta.env.DEV) {
    const devUrl = normalizeDevBackendUrl(import.meta.env.VITE_BUTLY_DEV_BACKEND_URL);
    if (devUrl) return new DevBrowserBridge(devUrl);
  }
  return new NullBridge();
}
