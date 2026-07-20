import { defineConfig } from "@hey-api/openapi-ts";

// 生成物（src/api/generated/）は手編集禁止。
// 契約の正本は openapi/butly.openapi.json（scripts/generate_openapi.sh で再生成）。
export default defineConfig({
  input: "../openapi/butly.openapi.json",
  output: {
    path: "src/api/generated",
    format: "prettier",
    lint: false,
  },
  plugins: ["@hey-api/client-fetch"],
});
