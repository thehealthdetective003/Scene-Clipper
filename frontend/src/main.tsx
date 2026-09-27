import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { ThemeProvider } from "./hooks/useTheme";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root container is missing.");
}

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <ThemeProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ThemeProvider>
  </React.StrictMode>,
);
