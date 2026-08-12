// assistant 応答の Markdown 描画。
//
// 安全側の既定を明示しておく:
// - **raw HTML は描画しない**（rehype-raw を入れない）。記憶や web 検索結果を
//   経由して任意タグを差し込まれる経路を作らないため。
// - **リンクは外部で開く**。webview 内遷移を許さない（external.ts）。
//   URL は http/https のみ通す（safeExternalUrl）。
// - **画像は読み込まず、リンクとして描画する**。Tauri の CSP は
//   `img-src 'self' data:` なので remote 画像はどのみち表示できず、
//   読みに行けば tracking pixel として機能してしまう。
//
// 送信者の発言は Markdown として解釈しない（打った通りに見せる）。

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { openExternal } from "./external";
import { safeExternalUrl } from "./SourceList";

function ExternalLink({
  href,
  children,
}: {
  href?: string;
  children?: React.ReactNode;
}) {
  const url = safeExternalUrl(href);
  if (!url) return <>{children}</>;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => {
        event.preventDefault();
        void openExternal(url);
      }}
    >
      {children}
    </a>
  );
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <ExternalLink href={href}>{children}</ExternalLink>
          ),
          // 画像は取得せずリンクに落とす（CSP と tracking 対策）。
          img: ({ src, alt }) => (
            <ExternalLink href={typeof src === "string" ? src : undefined}>
              {alt || src || ""}
            </ExternalLink>
          ),
          table: ({ children }) => (
            <div className="markdown-table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
