import type { AttachmentLimits } from "../../api/generated";
import type { PreparedAttachment } from "./types";

export type AttachmentPreparationError =
  | { code: "attachment_limit"; count: number }
  | { code: "attachment_size"; name: string; maxSizeBytes: number }
  | { code: "attachment_type"; name: string }
  | { code: "attachment_read"; name: string };

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () =>
      typeof reader.result === "string"
        ? resolve(reader.result)
        : reject(new Error("FileReader returned a non-string value."));
    reader.onerror = () => reject(reader.error ?? new Error("FileReader failed."));
    reader.readAsDataURL(file);
  });
}

export async function prepareAttachments(
  files: File[],
  currentCount: number,
  limits: AttachmentLimits,
): Promise<{ attachments: PreparedAttachment[]; errors: AttachmentPreparationError[] }> {
  const errors: AttachmentPreparationError[] = [];
  const remaining = Math.max(0, limits.max_count - currentCount);
  if (files.length > remaining) {
    errors.push({ code: "attachment_limit", count: limits.max_count });
  }

  const accepted = files.slice(0, remaining);
  const attachments: PreparedAttachment[] = [];
  for (const file of accepted) {
    if (!limits.allowed_mime_types.includes(file.type)) {
      errors.push({ code: "attachment_type", name: file.name });
      continue;
    }
    if (file.size > limits.max_size_bytes) {
      errors.push({
        code: "attachment_size",
        name: file.name,
        maxSizeBytes: limits.max_size_bytes,
      });
      continue;
    }
    try {
      const previewUrl = await readDataUrl(file);
      const marker = ";base64,";
      const markerIndex = previewUrl.indexOf(marker);
      if (markerIndex < 0) throw new Error("FileReader did not return base64 data.");
      attachments.push({
        input: {
          kind: "image",
          mime_type: file.type as PreparedAttachment["input"]["mime_type"],
          data_base64: previewUrl.slice(markerIndex + marker.length),
          name: file.name,
        },
        sizeBytes: file.size,
        previewUrl,
      });
    } catch {
      errors.push({ code: "attachment_read", name: file.name });
    }
  }
  return { attachments, errors };
}
