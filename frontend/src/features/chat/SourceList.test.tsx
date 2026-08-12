import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../../i18n/strings";
import { safeExternalUrl, SourceList } from "./SourceList";

describe("SourceList", () => {
  it("allows only HTTP(S) source URLs", () => {
    expect(safeExternalUrl("https://example.com/path")).toBe(
      "https://example.com/path",
    );
    expect(safeExternalUrl("http://example.com")).toBe("http://example.com/");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("file:///tmp/private")).toBeNull();
    expect(safeExternalUrl("https://user:password@example.com/private")).toBeNull();
    expect(safeExternalUrl(`https://example.com/${"a".repeat(2_100)}`)).toBeNull();
    expect(safeExternalUrl("not a url")).toBeNull();
  });

  it("opens safe sources externally and keeps copy as a secondary action", async () => {
    const openSpy = vi.fn();
    vi.stubGlobal("open", openSpy);
    render(
      <I18nProvider>
        <SourceList
          sources={[
            { title: "Safe", url: "https://example.com" },
            { title: "Unsafe", url: "javascript:alert(1)" },
          ]}
        />
      </I18nProvider>,
    );

    const link = screen.getByRole("link", { name: /Safe/ });
    expect(link).toHaveAttribute("href", "https://example.com/");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(screen.getByRole("button", { name: /Safe/ })).toBeInTheDocument();
    expect(screen.queryByText("Unsafe")).not.toBeInTheDocument();

    // webview 内で遷移させず、必ず外部へ渡す。
    await userEvent.click(link);
    expect(openSpy).toHaveBeenCalledWith(
      "https://example.com/",
      "_blank",
      "noopener,noreferrer",
    );

    vi.unstubAllGlobals();
  });
});
