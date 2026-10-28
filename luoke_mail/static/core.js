// Shared plumbing. Email content is untrusted: everything is rendered through el() with text nodes;
// never put mail text through innerHTML.
import { icon } from "./icons.js";

// The token comes once in ?t= (the app, the terminal link) and is kept for this address in localStorage: every
// tab on it works, also after the service restarts or is upgraded. It never stays in the address bar or history.
const TOKEN_KEY = "luoke-mail.token";
const TOKEN = (() => {
  const u = new URL(location.href);
  const t = u.searchParams.get("t");
  if (t) {
    try { localStorage.setItem(TOKEN_KEY, t); } catch (e) { /* private mode: this tab only */ }
    u.searchParams.delete("t");
    history.replaceState(null, "", u.pathname + u.search + u.hash);   // keep #page: a link may open a page
    return t;
  }
  try { return localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem("luoke-token") || ""; } catch (e) { return ""; }
})();
export const hasToken = () => !!TOKEN;

export async function api(path, body) {
  const opts = { headers: { "X-Luoke-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) {                     // nothing answered: the service is restarting or not running
    const err = new Error("无法连接螺壳邮件助手，请稍后重试。");
    err.offline = true;
    throw err;
  }
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) {
    if (r.status === 401) { try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* nothing kept */ } }   // another data folder's
    if (!data.error) console.error(`${path}: HTTP ${r.status}`);
    const err = new Error(data.error || "操作失败，请重试。");
    err.status = r.status;
    throw err;
  }
  return data;
}

export function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

export const $ = (id) => document.getElementById(id);

// follow a background job until it ends; onStage(job) while it runs
export function waitJob(id, onStage = () => {}) {
  return new Promise((resolve, reject) => {
    const tick = () => api(`/api/jobs/${id}`).then((j) => {
      if (j.status === "running") { onStage(j); setTimeout(tick, 700); return; }
      if (j.status === "error") reject(new Error(j.error)); else resolve(j.result);
    }).catch(reject);
    tick();
  });
}

export function setStatus(id, text, kind) {
  const s = $(id);
  s.textContent = text || "";
  s.className = "status" + (kind ? " " + kind : "");
}

export const shortDate = (iso) => { const [, m, d] = iso.split("-"); return `${Number(m)}/${Number(d)}`; };

// ---- app-wide state ----------------------------------------------------------------------------
// S.source: the inbox every page shows -- a connected mailbox (its key, the lowercased address) or "demo"
export const S = { state: null, source: "demo", synced: false };
export const nav = { go: (step) => {}, refresh: async () => {} };   // filled in by main.js
// the connected mailboxes (password saved), first first; the one the pages show now
export const mailboxes = () => (S.state ? S.state.accounts.filter((a) => a.has_secret) : []);
export const currentMailbox = () => mailboxes().find((a) => a.key === S.source) || null;
// first run shows the setup steps; once a model is chosen and there is an inbox to look at, the everyday pages
export const setupDone = () => !!(S.state && hasModel() && (mailboxes().length || S.state.synced.demo));
// a way to a model: 螺壳主控 (it picks the model), or a model service and a model
export const hasModel = () => !!(S.state && (S.state.engine.via === "hub" || S.state.engine.model));
const sourceListeners = [];
export function onSourceChange(fn) { sourceListeners.push(fn); }
// the mailbox last shown, kept in this browser so a reload (or the page opened again) stays on it (issue #8);
// storage may be unavailable (a private window): then the first mailbox, as before
const SOURCE_KEY = "luoke-mail.source";
export function lastSource() {
  try { return localStorage.getItem(SOURCE_KEY); } catch (e) { return null; }
}
export function setSource(src) {
  S.source = src;
  try { localStorage.setItem(SOURCE_KEY, src); } catch (e) { /* not kept: the first mailbox next time */ }
  document.querySelectorAll("[data-sources] [data-source]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.source === src)));
  sourceListeners.forEach((fn) => fn(src));
}

// the switch at the top of the pages: each connected mailbox, then the demo inbox. One mailbox is 「我的邮箱」;
// several are told apart by the name before the @ (the whole address when two share it).
// Returns true when the mailbox shown was removed meanwhile and another one took its place.
// a mailbox's mark in the sidebar: its first letter on a soft tint, so a folded sidebar still tells them apart
export function mark(key, address) {
  const letter = (address || "").replace(/^未连接$/, "").charAt(0).toUpperCase();
  const tone = [...(key || "")].reduce((n, ch) => (n + ch.charCodeAt(0)) % 4, 0);
  return letter ? el("span", { class: "mb-mark t" + tone, "aria-hidden": "true" }, letter) : icon("mail");
}

export function renderSources() {
  const boxes = mailboxes();
  const before = S.source;
  // the sample inbox is for before a mailbox is connected; once one is, it is gone (10-23, luoke-mail#6)
  if (!boxes.some((a) => a.key === S.source) && !(S.source === "demo" && !boxes.length)) S.source = boxes.length ? boxes[0].key : "demo";
  const label = (a) => (boxes.length === 1 ? "我的邮箱" : a.address);     // several: the whole address, "me" alone says little
  const items = boxes.map((a) => [a.key, label(a), a.address]);
  if (!boxes.length) items.push(["", "我的邮箱", "未连接"]);
  if (!boxes.length) items.push(["demo", "示例邮件", "示例数据，不会更改邮箱。"]);
  document.querySelectorAll("[data-sources]").forEach((list) => {
    list.textContent = "";
    items.forEach(([key, text, title]) => list.append(el("li", null, el("button", { type: "button", role: "radio", "data-source": key,
      title: key === "demo" ? title : `${text}\n${title}`, "aria-checked": String(key === S.source), onclick: () => {
        if (!key) { toast("请先连接邮箱。"); return; }
        if (key !== S.source) setSource(key);
      } }, key === "demo" ? icon("file-text") : mark(key, title), el("span", { class: "label" }, text)))));
  });
  return S.source !== before;
}

// ---- toast with optional undo --------------------------------------------------------------------
let toastTimer = null;
export function toast(message, undo) {
  const t = $("toast");
  t.textContent = "";
  t.classList.remove("busy");
  t.append(el("span", null, message));
  if (undo) {     // undo waits on the mailbox too: say so until its own result replaces this
    t.append(el("button", { class: "link strong", type: "button", onclick: async () => { working("正在撤销…"); await undo(); } }, "撤销"));
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, undo ? 15000 : 4500);
}

// a toast that stays until the next one: something is in progress
export function working(message) {
  const t = $("toast");
  t.textContent = "";
  t.classList.add("busy");
  t.append(el("span", null, message));
  t.hidden = false;
  clearTimeout(toastTimer);
}

// Anything that waits on the mailbox answers the click at once: the button says what is happening, its card
// or bar can't be clicked again meanwhile, and the result toast follows when it's done.
export async function withBusy(btn, label, fn) {
  const box = btn ? btn.closest(".card, .sub-card, .clean-bar, .log-row, .set-row") : null;
  const before = btn ? [...btn.childNodes] : [];
  if (btn) { btn.disabled = true; btn.classList.add("working"); btn.textContent = label; }
  if (box) box.classList.add("working");
  try {
    return await fn();
  } finally {
    if (box) box.classList.remove("working");
    if (btn) { btn.classList.remove("working"); btn.replaceChildren(...before); btn.disabled = false; }
  }
}

// ---- confirm dialog --------------------------------------------------------------------------------
export function confirmDialog(title, content, okLabel = "确定") {
  const d = $("dialog");
  $("dialog-title").textContent = title;
  const box = $("dialog-content");
  box.textContent = "";
  box.append(...(Array.isArray(content) ? content : [content]).filter(Boolean));   // optional parts may be null
  $("dialog-ok").textContent = okLabel;
  d.returnValue = "";
  d.showModal();
  return new Promise((resolve) => d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true }));
}

// when this page's server has gone away (handed over to the background service, or turned off)
export function endPage(title, text) {
  const stage = $("stage");
  stage.textContent = "";
  stage.append(el("section", { class: "ended" }, el("h2", { class: "title" }, title), el("p", { class: "lede" }, text)));
  document.querySelectorAll(".rail button").forEach((b) => { b.disabled = true; });
}

export function download(filename, text, type = "text/calendar") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
