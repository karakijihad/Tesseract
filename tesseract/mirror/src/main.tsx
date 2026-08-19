import React from "react";
import ReactDOM from "react-dom/client";
import "./styles/globals.css";
import "./styles/hud-chat.css";
import App from "./App";
import { installAppearance } from "./stores/appearance";
import { revealWhenWarm } from "./lib/warmup";

// Before the first paint — a persisted type scale or accent must not flash
// the shipped default first.
installAppearance();

// This window is hidden behind the launch splash until the backend finishes
// preparing itself. Started before the render, not after: the wait is the
// backend's, and React mounting is what should be happening during it.
revealWhenWarm();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
