import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ButlyApiError } from "../../api/errors";
import { I18nProvider } from "../../i18n/strings";
import { TraceGraph } from "./TraceGraph";
import type { ApiTransport } from "../../api/transport";

const renderMock = vi.fn(async (id: string, source: string) => ({
  source,
  svg: `<svg data-testid="trace-svg" data-id="${id}"><g /></svg>`,
}));
const initializeMock = vi.fn();

// mermaid は bundle が大きいので動的 import している。ここではその境界をモックする。
vi.mock("mermaid", () => ({
  default: {
    initialize: (...args: unknown[]) => initializeMock(...args),
    render: (id: string, source: string) => renderMock(id, source),
  },
}));

const TRACE = {
  trace_id: "turn_7",
  turn_id: 7,
  source: "web",
  created_at: "2026-08-12T23:28:28",
  mermaid: 'flowchart TD\n    gatekeeper["Gatekeeper"]',
  node_counts: { active: 2, skipped: 1 },
};

function renderGraph(transport: Partial<ApiTransport>, instanceName = "Alpha") {
  render(
    <I18nProvider>
      <TraceGraph
        transport={transport as ApiTransport}
        instanceName={instanceName}
      />
    </I18nProvider>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("TraceGraph", () => {
  it("does not fetch or load mermaid until asked", () => {
    const getTrace = vi.fn();
    renderGraph({ getTrace });

    expect(getTrace).not.toHaveBeenCalled();
    expect(renderMock).not.toHaveBeenCalled();
  });

  it("renders the backend-generated Mermaid as SVG", async () => {
    const getTrace = vi.fn(async () => TRACE);
    renderGraph({ getTrace });

    await userEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(screen.getByRole("img")).toBeInTheDocument());
    expect(getTrace).toHaveBeenCalledWith("Alpha", "TD");
    // Mermaid 文字列は backend が正本。frontend では組み立て直さない。
    expect(renderMock).toHaveBeenCalledWith(expect.any(String), TRACE.mermaid);
    expect(screen.getByRole("img").innerHTML).toContain("<svg");
  });

  it("keeps mermaid from interpreting labels as HTML", async () => {
    renderGraph({ getTrace: vi.fn(async () => TRACE) });

    await userEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(initializeMock).toHaveBeenCalled());
    expect(initializeMock).toHaveBeenCalledWith(
      expect.objectContaining({ securityLevel: "strict", htmlLabels: false }),
    );
  });

  it("shows the trace identity and status counts", async () => {
    renderGraph({ getTrace: vi.fn(async () => TRACE) });

    await userEvent.click(screen.getByRole("button"));

    await waitFor(() =>
      expect(screen.getByText(/turn_7/)).toHaveTextContent("active 2 / skipped 1"),
    );
  });

  it("explains a missing trace instead of showing a raw error", async () => {
    const getTrace = vi.fn(async () => {
      throw new ButlyApiError("trace_not_found", "no trace", { status: 404 });
    });
    renderGraph({ getTrace });

    await userEvent.click(screen.getByRole("button"));

    await waitFor(() =>
      expect(screen.getByText(/まだ trace がありません/)).toBeInTheDocument(),
    );
  });

  it("drops a previous graph when the instance changes", async () => {
    const getTrace = vi.fn(async () => TRACE);
    const { rerender } = render(
      <I18nProvider>
        <TraceGraph transport={{ getTrace } as unknown as ApiTransport} instanceName="Alpha" />
      </I18nProvider>,
    );

    await userEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByRole("img")).toBeInTheDocument());

    rerender(
      <I18nProvider>
        <TraceGraph transport={{ getTrace } as unknown as ApiTransport} instanceName="Beta" />
      </I18nProvider>,
    );

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
  it("expands the graph over the whole window and closes with Escape", async () => {
    renderGraph({ getTrace: vi.fn(async () => TRACE) });

    await userEvent.click(screen.getByRole("button", { name: /処理フロー/ }));
    await waitFor(() => expect(screen.getByRole("img")).toBeInTheDocument());

    // パネル内に収まらないグラフは、常設カラムではなく必要なときだけ広げる。
    await userEvent.click(screen.getByRole("button", { name: /拡大/ }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });
  it("re-fetches the graph laid out horizontally when the screen is wide", async () => {
    const getTrace = vi.fn(async () => TRACE);
    renderGraph({ getTrace });

    await userEvent.click(screen.getByRole("button", { name: /処理フロー/ }));
    await waitFor(() => expect(screen.getByRole("img")).toBeInTheDocument());

    // 向きは backend の render_mermaid に委ねる。frontend で SVG を組み替えない。
    await userEvent.click(screen.getByRole("button", { name: /横向き/ }));
    await waitFor(() => expect(getTrace).toHaveBeenCalledWith("Alpha", "LR"));
    expect(screen.getByRole("button", { name: /縦向き/ })).toBeInTheDocument();
  });
});
