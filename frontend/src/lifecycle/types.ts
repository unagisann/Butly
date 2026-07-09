// Tauri (Rust) 側 backend.rs が emit する backend-state event の payload。
// 状態の正本は Rust 側。React は表示に徹する。

export type BackendPhase =
  | "starting"
  | "ready"
  | "unavailable"
  | "crashed"
  | "version_mismatch";

export interface BackendState {
  phase: BackendPhase;
  /** ready のときだけ入る。loopback port。 */
  port?: number;
  backendVersion?: string;
  apiVersion?: string;
  /** unavailable / crashed / version_mismatch の補足（token 等の秘密は含めない） */
  detail?: string;
}

export interface ConnectionInfo {
  baseUrl: string;
  /** per-launch desktop token。React の memory にのみ保持し、永続化しない。 */
  token: string | null;
}
