import { describe, expect, it } from "vitest";

import { prepareAttachments } from "./attachments";

const LIMITS = {
  max_count: 3,
  max_size_bytes: 16,
  allowed_mime_types: ["image/png"],
};

describe("prepareAttachments", () => {
  it("converts an allowed image to header-free base64", async () => {
    const file = new File([new Uint8Array([1, 2, 3, 4])], "memory.png", {
      type: "image/png",
    });
    const result = await prepareAttachments([file], 0, LIMITS);

    expect(result.errors).toEqual([]);
    expect(result.attachments[0]?.input).toMatchObject({
      kind: "image",
      mime_type: "image/png",
      name: "memory.png",
      data_base64: "AQIDBA==",
    });
    expect(result.attachments[0]?.previewUrl).toMatch(/^data:image\/png;base64,/);
  });

  it("rejects unsupported and oversized files before encoding", async () => {
    const unsupported = new File(["x"], "note.gif", { type: "image/gif" });
    const oversized = new File([new Uint8Array(17)], "large.png", {
      type: "image/png",
    });
    const result = await prepareAttachments([unsupported, oversized], 0, LIMITS);

    expect(result.attachments).toEqual([]);
    expect(result.errors).toEqual([
      { code: "attachment_type", name: "note.gif" },
      { code: "attachment_size", name: "large.png", maxSizeBytes: 16 },
    ]);
  });

  it("enforces the aggregate attachment count", async () => {
    const extra = new File(["x"], "extra.png", { type: "image/png" });
    const result = await prepareAttachments([extra], LIMITS.max_count, LIMITS);

    expect(result.attachments).toEqual([]);
    expect(result.errors).toEqual([{ code: "attachment_limit", count: 3 }]);
  });
});
