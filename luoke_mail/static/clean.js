// 清理: old mail, two tabs -- inbox mail older than the sorting window, and archived mail. Both are judged
// message by message by the local model (sweep.js); the tabs differ only in what a verdict leads to.
import { $, nav } from "./core.js";
import { renderModeHint } from "./settings.js";
import { showSweep, stopSweep } from "./sweep.js";

let tab = "inbox";

export function openClean(which) {
  tab = which;
  nav.go("clean");
}

export function showClean() {
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  document.querySelectorAll("[data-tabpanel]").forEach((p) => { p.hidden = p.dataset.tabpanel !== tab; });
  renderModeHint($("clean-mode-hint"), showClean);
  showSweep(tab);
}

export function initClean() {
  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.tab; showClean(); }));
}

export const leaveClean = () => stopSweep();
