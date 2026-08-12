import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Markdown } from "./Markdown";
import { I18nProvider } from "../../i18n/strings";

function renderMarkdown(text: string) {
  return render(
    <I18nProvider>
      <Markdown text={text} />
    </I18nProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Markdown rendering", () => {
  it("renders headings, emphasis and lists instead of raw syntax", () => {
    const { container } = renderMarkdown(
      "### 検証したい主要項目\n\n**強調**\n\n- 一つ目\n- 二つ目",
    );

    expect(screen.getByRole("heading", { level: 3 })).toHaveTextContent(
      "検証したい主要項目",
    );
    expect(container.querySelector("strong")).toHaveTextContent("強調");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.textContent).not.toContain("###");
    expect(container.textContent).not.toContain("**");
  });

  it("renders GFM tables and fenced code", () => {
    const { container } = renderMarkdown(
      "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```py\nprint(1)\n```",
    );

    expect(container.querySelector("table")).toBeInTheDocument();
    expect(container.querySelectorAll("td")).toHaveLength(2);
    expect(container.querySelector("pre code")).toHaveTextContent("print(1)");
  });

  it("never renders raw HTML from the model", () => {
    // 記憶や web 検索結果を経由してタグを差し込まれる経路を作らない。
    const { container } = renderMarkdown(
      '<img src="x" onerror="alert(1)"><b>bold</b>',
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<b>bold</b>");
  });

  it("does not load remote images, degrading them to links", () => {
    const { container } = renderMarkdown("![説明](https://example.com/a.png)");

    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("link", { name: "説明" })).toHaveAttribute(
      "href",
      "https://example.com/a.png",
    );
  });

  it("drops non-http links but keeps their text", () => {
    const { container } = renderMarkdown("[押すな](javascript:alert(1))");

    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("押すな");
  });

  it("opens links externally instead of navigating the webview", async () => {
    const openSpy = vi.fn();
    vi.stubGlobal("open", openSpy);
    renderMarkdown("[Butly](https://example.com/docs)");

    const link = screen.getByRole("link", { name: "Butly" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));

    await userEvent.click(link);
    expect(openSpy).toHaveBeenCalledWith(
      "https://example.com/docs",
      "_blank",
      "noopener,noreferrer",
    );
  });
});
