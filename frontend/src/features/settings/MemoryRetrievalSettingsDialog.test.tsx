import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  InstanceMemoryRetrievalSettingsResponse,
  MemoryRetrievalValues,
} from "../../api/generated";
import type { ApiTransport } from "../../api/transport";
import { I18nProvider } from "../../i18n/strings";
import { MemoryRetrievalSettingsDialog } from "./MemoryRetrievalSettingsDialog";

const VALUES: MemoryRetrievalValues = {
  search_mode: "hybrid_evidence_fusion",
  vector_search_limit: 3,
  evidence_fusion_base_weight: 0.7,
  evidence_raw_chunk_chars: 1800,
  vector_candidates: 20,
  bm25_candidates: 20,
  rag_source_mode: "both",
  rag_raw_top_k: 0,
  rag_raw_max_chars: 0,
  rag_raw_neighbor_radius: 1,
};

const RESPONSE: InstanceMemoryRetrievalSettingsResponse = {
  defaults: { ...VALUES, search_mode: "vector", rag_source_mode: "cards", rag_raw_top_k: 1, rag_raw_max_chars: 2500, rag_raw_neighbor_radius: 0 },
  global_override: {},
  global_effective: { ...VALUES, search_mode: "hybrid", vector_search_limit: 4 },
  instance_override: {
    search_mode: "hybrid_evidence_fusion",
    evidence_fusion_base_weight: 0.7,
    rag_source_mode: "both",
    rag_raw_max_chars: 0,
    rag_raw_neighbor_radius: 1,
  },
  effective: VALUES,
  origins: {
    search_mode: "instance",
    vector_search_limit: "global",
    evidence_fusion_base_weight: "instance",
    evidence_raw_chunk_chars: "default",
    vector_candidates: "default",
    bm25_candidates: "default",
    rag_source_mode: "instance",
    rag_raw_top_k: "default",
    rag_raw_max_chars: "instance",
    rag_raw_neighbor_radius: "instance",
  },
};

function transportWithSettings(
  response = RESPONSE,
  patch = vi.fn(async () => response),
): ApiTransport {
  const unused = async () => {
    throw new Error("not used");
  };
  return {
    ping: unused,
    listInstances: unused,
    getMessages: unused,
    getTrace: unused,
    getCapabilities: unused,
    getPreflight: unused,
    getChatRequest: unused,
    streamChat: unused,
    cancelChat: unused,
    getGlobalMemoryRetrievalSettings: unused,
    patchGlobalMemoryRetrievalSettings: unused,
    getInstanceMemoryRetrievalSettings: vi.fn(async () => response),
    patchInstanceMemoryRetrievalSettings: patch,
  };
}

function renderDialog(transport: ApiTransport) {
  return render(
    <I18nProvider>
      <MemoryRetrievalSettingsDialog
        transport={transport}
        instanceName="alpha"
        onClose={() => {}}
      />
    </I18nProvider>,
  );
}

describe("MemoryRetrievalSettingsDialog", () => {
  it("shows effective values, origins, zero semantics, and neighbor radius", async () => {
    renderDialog(transportWithSettings());

    expect(await screen.findByLabelText("検索方式")).toHaveValue("hybrid_evidence_fusion");
    expect(screen.getByLabelText("Fusion Hybrid / Base 重み")).toHaveValue(0.7);
    expect(screen.getByLabelText("RAW 近傍")).toHaveValue("1");
    expect(screen.getByLabelText("無制限")).toBeChecked();
    expect(screen.getByText("0 は全候補", { exact: false })).toBeInTheDocument();

    const injectionInput = screen.getByLabelText("最終注入カード数");
    expect(injectionInput).toBeDisabled();
    expect(injectionInput.parentElement).toHaveTextContent("グローバル設定を使用");
  });

  it("sends null for inherited fields and keeps exact numeric values", async () => {
    const patch = vi.fn(async () => RESPONSE);
    renderDialog(transportWithSettings(RESPONSE, patch));
    const injectionInput = await screen.findByLabelText("最終注入カード数");
    const injectionField = injectionInput.closest(".memory-setting-field");
    expect(injectionField).not.toBeNull();

    await userEvent.click(within(injectionField as HTMLElement).getByRole("checkbox"));
    await userEvent.clear(injectionInput);
    await userEvent.type(injectionInput, "5");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch).toHaveBeenCalledWith(
      "alpha",
      expect.objectContaining({
        vector_search_limit: 5,
        evidence_fusion_base_weight: 0.7,
        rag_raw_max_chars: 0,
        rag_raw_neighbor_radius: 1,
        rag_raw_top_k: null,
      }),
    );
    expect(await screen.findByText("保存しました")).toBeInTheDocument();
  });

  it("does not show a success state when the backend rejects the save", async () => {
    const patch = vi.fn(async () => {
      throw new Error("candidate pool is too small");
    });
    renderDialog(transportWithSettings(RESPONSE, patch));
    await screen.findByLabelText("検索方式");

    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("candidate pool is too small");
    expect(screen.queryByText("保存しました")).not.toBeInTheDocument();
  });
});
