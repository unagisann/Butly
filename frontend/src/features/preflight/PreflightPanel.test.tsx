import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../../i18n/strings";
import { PreflightPanel } from "./PreflightPanel";

describe("PreflightPanel", () => {
  it("shows Ollama and embedding checks with localized reason codes", () => {
    render(
      <I18nProvider>
        <PreflightPanel
          report={{
            status: "degraded",
            checked_at: "2026-08-12T00:00:00Z",
            connections: [
              {
                connection_id: "ollama",
                label: "Ollama",
                protocol: "openai_compat",
                configured: true,
                reachable: false,
                status: "unreachable",
                reason: "connection_timeout",
              },
            ],
            embedding: {
              connection_id: "ollama",
              model_name: "nomic-embed-text",
              configured: true,
              reachable: false,
              status: "unreachable",
              reason: "embedding_not_supported",
            },
          }}
          loading={false}
          error={null}
          onRefresh={vi.fn()}
        />
      </I18nProvider>,
    );

    expect(screen.getByText("Ollama")).toBeInTheDocument();
    expect(screen.getByText(/接続がタイムアウトしました/)).toBeInTheDocument();
    expect(screen.getByText(/この接続はEmbeddingに対応していません/)).toBeInTheDocument();
    expect(screen.queryByText(/connection_timeout/)).not.toBeInTheDocument();
  });
});
