// base URL / desktop token を注入した API client を作る唯一の場所。
// token 注入はここに集約し、生成済み SDK の呼び出し側では扱わない（§9.3）。
// token は memory 上の ConnectionInfo からのみ渡す。localStorage 等への保存禁止。

import { createClient, createConfig } from "@hey-api/client-fetch";
import type { Client } from "@hey-api/client-fetch";

import type { ConnectionInfo } from "../lifecycle/types";

export function createApiClient(info: ConnectionInfo): Client {
  return createClient(
    createConfig({
      baseUrl: info.baseUrl,
      auth: () => info.token ?? undefined,
    }),
  );
}
