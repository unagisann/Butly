import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

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

  it("renders safe sources as copy actions without external navigation", () => {
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

    expect(screen.getByRole("button", { name: /Safe/ })).toBeInTheDocument();
    expect(screen.queryByText("Unsafe")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
