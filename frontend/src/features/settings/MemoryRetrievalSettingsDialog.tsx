import { useEffect, useMemo, useState } from "react";
import { Save, Settings2, X } from "lucide-react";

import type {
  InstanceMemoryRetrievalSettingsResponse,
  MemoryRetrievalInstancePatch,
  MemoryRetrievalValues,
} from "../../api/generated";
import type { ApiTransport } from "../../api/transport";
import { isAbortError, normalizeApiError } from "../../api/errors";
import { useI18n } from "../../i18n/strings";

type SettingKey = keyof MemoryRetrievalValues;
type InheritedState = Record<SettingKey, boolean>;

const SETTING_KEYS: SettingKey[] = [
  "search_mode",
  "vector_search_limit",
  "rag_source_mode",
  "rag_raw_max_chars",
  "rag_raw_top_k",
  "rag_raw_neighbor_radius",
  "evidence_fusion_base_weight",
  "evidence_raw_chunk_chars",
  "vector_candidates",
  "bm25_candidates",
];

interface Draft {
  values: MemoryRetrievalValues;
  inherited: InheritedState;
}

interface MemoryRetrievalSettingsDialogProps {
  transport: ApiTransport;
  instanceName: string;
  onClose: () => void;
}

function createDraft(response: InstanceMemoryRetrievalSettingsResponse): Draft {
  const inherited = {} as InheritedState;
  for (const key of SETTING_KEYS) {
    inherited[key] = !(key in response.instance_override);
  }
  return { values: { ...response.effective }, inherited };
}

function buildPatch(draft: Draft): MemoryRetrievalInstancePatch {
  const patch: MemoryRetrievalInstancePatch = {};
  for (const key of SETTING_KEYS) {
    Object.assign(patch, {
      [key]: draft.inherited[key] ? null : draft.values[key],
    });
  }
  return patch;
}

export function MemoryRetrievalSettingsDialog({
  transport,
  instanceName,
  onClose,
}: MemoryRetrievalSettingsDialogProps) {
  const { t } = useI18n();
  const [response, setResponse] = useState<InstanceMemoryRetrievalSettingsResponse | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    transport
      .getInstanceMemoryRetrievalSettings(instanceName, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setResponse(next);
        setDraft(createDraft(next));
      })
      .catch((cause: unknown) => {
        if (!isAbortError(cause)) setError(normalizeApiError(cause).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [instanceName, transport]);

  const fusionActive = draft?.values.search_mode === "hybrid_evidence_fusion";
  const rawActive = draft?.values.rag_source_mode !== "cards";
  const evidencePercent = useMemo(
    () => Math.round((1 - (draft?.values.evidence_fusion_base_weight ?? 0.7)) * 100),
    [draft?.values.evidence_fusion_base_weight],
  );

  function setValue<K extends SettingKey>(key: K, value: MemoryRetrievalValues[K]) {
    setSaved(false);
    setDraft((current) =>
      current
        ? {
            ...current,
            values: { ...current.values, [key]: value },
          }
        : current,
    );
  }

  function setInherited(key: SettingKey, inherited: boolean) {
    setSaved(false);
    setDraft((current) =>
      current
        ? {
            ...current,
            inherited: { ...current.inherited, [key]: inherited },
          }
        : current,
    );
  }

  function sourceText(key: SettingKey): string {
    if (!response || !draft) return "";
    if (!draft.inherited[key]) return t("settings.origin_instance");
    return response.origins[key] === "global"
      ? t("settings.origin_global")
      : t("settings.origin_default");
  }

  async function save() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const next = await transport.patchInstanceMemoryRetrievalSettings(
        instanceName,
        buildPatch(draft),
      );
      setResponse(next);
      setDraft(createDraft(next));
      setSaved(true);
    } catch (cause) {
      setError(normalizeApiError(cause).message);
    } finally {
      setSaving(false);
    }
  }

  function field(
    key: SettingKey,
    label: string,
    control: React.ReactNode,
    help?: string,
    inactive = false,
  ) {
    if (!draft) return null;
    return (
      <div className="memory-setting-field" data-inactive={inactive || undefined}>
        <div className="memory-setting-label">
          <label htmlFor={`memory-setting-${key}`}>{label}</label>
          <label className="inherit-toggle">
            <input
              type="checkbox"
              checked={!draft.inherited[key]}
              onChange={(event) => setInherited(key, !event.target.checked)}
            />
            {t("settings.override")}
          </label>
        </div>
        {control}
        <small>
          {sourceText(key)}
          {help ? ` · ${help}` : ""}
        </small>
      </div>
    );
  }

  const disabled = (key: SettingKey, inactive = false) =>
    Boolean(!draft || draft.inherited[key] || inactive || saving);

  return (
    <div className="settings-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="memory-settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-settings-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span className="settings-title-icon" aria-hidden="true"><Settings2 size={19} /></span>
          <div>
            <h2 id="memory-settings-title">{t("settings.title")}</h2>
            <p>{instanceName}</p>
          </div>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X size={17} aria-hidden="true" />
          </button>
        </header>

        <div className="memory-settings-body">
          {loading && <p className="muted">{t("settings.loading")}</p>}
          {error && <p className="inline-error" role="alert">{error}</p>}
          {draft && (
            <>
              <section className="settings-group" aria-labelledby="retrieval-basic-title">
                <h3 id="retrieval-basic-title">{t("settings.basic")}</h3>
                {field(
                  "search_mode",
                  t("settings.search_mode"),
                  <select
                    id="memory-setting-search_mode"
                    value={draft.values.search_mode}
                    disabled={disabled("search_mode")}
                    onChange={(event) =>
                      setValue(
                        "search_mode",
                        event.target.value as MemoryRetrievalValues["search_mode"],
                      )
                    }
                  >
                    <option value="vector">Vector</option>
                    <option value="hybrid">Hybrid</option>
                    <option value="hybrid_evidence_fusion">Hybrid + Evidence Fusion</option>
                  </select>,
                )}
                {field(
                  "vector_search_limit",
                  t("settings.injection_limit"),
                  <input
                    id="memory-setting-vector_search_limit"
                    type="number"
                    min={1}
                    max={10}
                    value={draft.values.vector_search_limit}
                    disabled={disabled("vector_search_limit")}
                    onChange={(event) => setValue("vector_search_limit", Number(event.target.value))}
                  />,
                )}
                {field(
                  "rag_source_mode",
                  t("settings.source_mode"),
                  <select
                    id="memory-setting-rag_source_mode"
                    value={draft.values.rag_source_mode}
                    disabled={disabled("rag_source_mode")}
                    onChange={(event) =>
                      setValue(
                        "rag_source_mode",
                        event.target.value as MemoryRetrievalValues["rag_source_mode"],
                      )
                    }
                  >
                    <option value="cards">{t("settings.cards")}</option>
                    <option value="raw">RAW</option>
                    <option value="both">{t("settings.both")}</option>
                  </select>,
                )}
              </section>

              <section className="settings-group" aria-labelledby="retrieval-raw-title">
                <h3 id="retrieval-raw-title">{t("settings.raw")}</h3>
                {field(
                  "rag_raw_max_chars",
                  t("settings.raw_max_chars"),
                  <div className="unlimited-control">
                    <input
                      id="memory-setting-rag_raw_max_chars"
                      type="number"
                      min={1}
                      max={50000}
                      value={draft.values.rag_raw_max_chars || 2500}
                      disabled={disabled("rag_raw_max_chars", !rawActive) || draft.values.rag_raw_max_chars === 0}
                      onChange={(event) => setValue("rag_raw_max_chars", Number(event.target.value))}
                    />
                    <label>
                      <input
                        type="checkbox"
                        checked={draft.values.rag_raw_max_chars === 0}
                        disabled={disabled("rag_raw_max_chars", !rawActive)}
                        onChange={(event) =>
                          setValue("rag_raw_max_chars", event.target.checked ? 0 : 2500)
                        }
                      />
                      {t("settings.unlimited")}
                    </label>
                  </div>,
                  draft.values.rag_raw_max_chars === 0
                    ? t("settings.unlimited_warning")
                    : t("settings.characters"),
                  !rawActive,
                )}
                {field(
                  "rag_raw_top_k",
                  t("settings.raw_top_k"),
                  <input
                    id="memory-setting-rag_raw_top_k"
                    type="number"
                    min={0}
                    max={20}
                    value={draft.values.rag_raw_top_k}
                    disabled={disabled("rag_raw_top_k", !rawActive)}
                    onChange={(event) => setValue("rag_raw_top_k", Number(event.target.value))}
                  />,
                  t("settings.zero_all"),
                  !rawActive,
                )}
                {field(
                  "rag_raw_neighbor_radius",
                  t("settings.raw_neighbor"),
                  <select
                    id="memory-setting-rag_raw_neighbor_radius"
                    value={draft.values.rag_raw_neighbor_radius}
                    disabled={disabled("rag_raw_neighbor_radius", !rawActive)}
                    onChange={(event) => setValue("rag_raw_neighbor_radius", Number(event.target.value))}
                  >
                    {Array.from({ length: 11 }, (_, radius) => (
                      <option key={radius} value={radius}>
                        {radius === 0 ? t("settings.none") : `±${radius}`}
                      </option>
                    ))}
                  </select>,
                  undefined,
                  !rawActive,
                )}
              </section>

              <details className="settings-advanced">
                <summary>{t("settings.advanced")}</summary>
                <section className="settings-group">
                  {field(
                    "evidence_fusion_base_weight",
                    t("settings.fusion_weight"),
                    <input
                      id="memory-setting-evidence_fusion_base_weight"
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={draft.values.evidence_fusion_base_weight}
                      disabled={disabled("evidence_fusion_base_weight", !fusionActive)}
                      onChange={(event) =>
                        setValue("evidence_fusion_base_weight", Number(event.target.value))
                      }
                    />,
                    t("settings.fusion_split", {
                      base: Math.round(draft.values.evidence_fusion_base_weight * 100),
                      evidence: evidencePercent,
                    }),
                    !fusionActive,
                  )}
                  {field(
                    "evidence_raw_chunk_chars",
                    t("settings.evidence_chunk"),
                    <input
                      id="memory-setting-evidence_raw_chunk_chars"
                      type="number"
                      min={200}
                      max={10000}
                      step={100}
                      value={draft.values.evidence_raw_chunk_chars}
                      disabled={disabled("evidence_raw_chunk_chars", !fusionActive)}
                      onChange={(event) => setValue("evidence_raw_chunk_chars", Number(event.target.value))}
                    />,
                    t("settings.ranking_only"),
                    !fusionActive,
                  )}
                  {field(
                    "vector_candidates",
                    t("settings.vector_candidates"),
                    <input
                      id="memory-setting-vector_candidates"
                      type="number"
                      min={3}
                      max={100}
                      value={draft.values.vector_candidates}
                      disabled={disabled("vector_candidates")}
                      onChange={(event) => setValue("vector_candidates", Number(event.target.value))}
                    />,
                  )}
                  {field(
                    "bm25_candidates",
                    t("settings.bm25_candidates"),
                    <input
                      id="memory-setting-bm25_candidates"
                      type="number"
                      min={3}
                      max={100}
                      value={draft.values.bm25_candidates}
                      disabled={disabled("bm25_candidates", draft.values.search_mode === "vector")}
                      onChange={(event) => setValue("bm25_candidates", Number(event.target.value))}
                    />,
                    undefined,
                    draft.values.search_mode === "vector",
                  )}
                </section>
              </details>
            </>
          )}
        </div>

        <footer>
          {saved && <span className="save-success" role="status">{t("settings.saved")}</span>}
          <button className="text-button" type="button" onClick={onClose}>
            {t("common.close")}
          </button>
          <button
            className="primary-button"
            type="button"
            disabled={!draft || loading || saving}
            onClick={() => void save()}
          >
            <Save size={16} aria-hidden="true" />
            {saving ? t("settings.saving") : t("settings.save")}
          </button>
        </footer>
      </section>
    </div>
  );
}
