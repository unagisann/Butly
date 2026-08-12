import { describe, expect, it } from "vitest";

import { localizeReason } from "./reasons";
import { translate } from "./strings";

describe("localizeReason", () => {
  it("localizes current capability and preflight reason codes", () => {
    expect(
      localizeReason("active_model_does_not_support_vision", (key, params) =>
        translate("ja", key, params),
      ),
    ).toBe("選択中のモデルは画像入力に対応していません");
    expect(
      localizeReason("embedding_model_not_confirmed", (key, params) =>
        translate("en", key, params),
      ),
    ).toBe("The embedding model availability could not be confirmed");
    expect(
      localizeReason("chat_model_not_available", (key, params) =>
        translate("ja", key, params),
      ),
    ).toBe("選択中のチャットモデルを接続先で確認できません");
  });

  it("does not expose unknown backend codes", () => {
    expect(
      localizeReason("new_internal_reason", (key, params) =>
        translate("ja", key, params),
      ),
    ).toBe("詳細を確認できません");
  });
});
