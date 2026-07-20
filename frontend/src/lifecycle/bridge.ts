// Tauri lifecycle bridge。
// - backend process / token / port は Rust 側が所有する（§5.1）。
//   React は event 購読と command 呼び出しだけを行い、shell を spawn できない。
// - テストや素の browser 実行では Tauri API が無いので、interface に切り出して
//   差し替えられるようにする。

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

export function createLifecycleBridge(): LifecycleBridge {
  return hasTauri() ? new TauriBridge() : new NullBridge();
}
