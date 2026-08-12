// 外部 URL を開く唯一の経路。
// - Tauri では WebView 内に別ページを開かせず、OS 既定のブラウザへ渡す。
//   webview 内遷移を許すと、アプリの CSP と履歴の外にある任意ページを
//   同じ window で表示できてしまう。
// - browser dev では通常の別タブ。
// URL の検証は呼び出し側の `safeExternalUrl`（http/https のみ、認証情報つきを拒否）
// を通した値であることを前提にする。

function hasTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function openInBrowserTab(url: string): void {
  window.open(url, "_blank", "noopener,noreferrer");
}

export async function openExternal(url: string): Promise<void> {
  if (!hasTauri()) {
    openInBrowserTab(url);
    return;
  }
  try {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(url);
  } catch (error) {
    // plugin 未許可・未登録でもリンクが死なないようにする。
    console.warn("[frontend] failed to open URL externally", error);
    openInBrowserTab(url);
  }
}
