import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
import { createLifecycleBridge } from "./lifecycle/bridge";
import "./styles.css";

const bridge = createLifecycleBridge();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App bridge={bridge} />
  </React.StrictMode>,
);
