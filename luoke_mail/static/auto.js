// Background sorting is a property of the whole app, not of one page: its controls live in 设置, and its
// state (with the processing mode) sits at the bottom of the rail on every page. Turning it on hands this
// page over to the login item; the background sorts, notifies, marks sorted mail read and carries out confirmed
// habits (ADR 0016) -- each listed in 今日处理 -- and never trashes, unsubscribes or sends.
import { $, S, api, confirmDialog, el, endPage, mailboxes, nav, setupDone, toast, withBusy } from "./core.js";
import { renderActivity } from "./activity.js";

const every = (m) => (m >= 60 ? `每 ${m / 60} 小时` : `每 ${m} 分钟`);
const listeners = [];

// ---- what the page checks every so often: background progress and status ------------------------------
export function onActivity(fn) { listeners.push(fn); }

let lastAuto = "";
let lastModel;

export async function checkActivity() {
  if (!S.state) return null;
  let r;
  // "seen": someone is looking -- a sync they started is then work they wait for (see App.watching)
  try { r = await api("/api/activity" + (document.visibilityState === "visible" ? "?seen=1" : "")); } catch (e) { return null; }
  renderActivity(r);
  const unseen = (r.review || {})[S.source] || 0;                        // 今日处理: handled today, not looked at yet
  document.querySelectorAll("[data-review-badge]").forEach((b) => { b.hidden = !unseen; b.textContent = unseen || ""; });
  document.querySelectorAll("[data-memory-badge]").forEach((b) => {       // what the memory would like to ask
    b.hidden = !r.guesses;
    b.textContent = r.guesses || "";
  });
  const model = JSON.stringify(r.model || null);      // 螺壳主控 not ready to sort yet: the rail and 设置 say why
  if (model !== lastModel) {
    lastModel = model;
    S.model = r.model || null;
    renderRailStatus();
    if ($("set-model-note")) $("set-model-note").textContent = S.model ? S.model.text : "";
  }
  const auto = JSON.stringify(r.auto);
  if (auto !== lastAuto) {           // redraw only on change: polling must not close a menu the user has open
    lastAuto = auto;
    S.state.auto = r.auto;
    renderRailStatus();
    if ($("set-auto")) renderAutoRow($("set-auto"));
  }
  listeners.forEach((fn) => fn(r));
  return r;
}

function lastText(a) {
  if (!a.last) return "尚未运行";
  const next = a.enabled && a.next_in_min !== null && a.next_in_min !== undefined ? `。下次约在 ${a.next_in_min} 分钟后` : "";
  return `上次 ${a.last.at.slice(11)}：${a.last.text}${next}`;
}

// whose name macOS shows the notifications under: 螺壳 posts them when this runs inside it
const notifier = () => (S.state.auto.desktop.in_luoke ? "螺壳" : "螺壳邮件助手");

const stalled = (a) => a.enabled && a.desktop.supported && !a.service && (!a.desktop.installed || a.desktop.stale);

// ---- the rail: always visible once set up -------------------------------------------------------------
export function renderRailStatus() {
  const box = $("rail-status");
  box.textContent = "";
  box.hidden = !setupDone() || !mailboxes().length;
  if (box.hidden) return;
  const a = S.state.auto;
  const bad = stalled(a) || (a.enabled && a.last && !a.last.ok);
  box.classList.toggle("warn", !!bad);
  const hub = S.state.engine.via === "hub";
  box.append(...[
    // the model first (luoke-brand DESIGN.md 7: the sidebar's foot says how 主控 is)
    S.model ? el("span", { class: "dot key" }, S.model.short)
      : el("span", { class: "dot on" }, hub ? "螺壳 · 本机模型就绪" : "本机模型已连接"),
    el("span", { class: "dot" + (a.enabled ? (bad ? " key" : " on") : "") },
      a.enabled ? (stalled(a) ? "后台自动整理未运行" : `后台自动整理 · ${every(a.every)}`) : "后台自动整理：已关闭"),
    el("span", { class: "dot" + (S.state.mode.write ? " on" : "") }, S.state.mode.write ? "处理模式已开启" : "处理模式未开启"),
  ].filter(Boolean));          // Element.append would print a null as "null"
  box.title = "在「设置」中修改";
}

// ---- the 设置 row ----------------------------------------------------------------------------------------
export function renderAutoRow(row) {
  const a = S.state.auto;
  const d = a.desktop;
  const body = row.querySelector(".set-body");
  const act = row.querySelector(".set-act");
  body.textContent = "";
  act.textContent = "";
  row.classList.remove("warn");
  if (!mailboxes().length) {
    body.append(el("p", null, "连接邮箱后可用。"));
    return;
  }
  if (!a.enabled) {
    body.append(el("p", null, "已关闭：开启后定时整理，有要你处理的邮件时发通知。"));
    act.append(el("button", { type: "button", class: "btn small primary", onclick: enable }, "开启"));
    return;
  }
  if (stalled(a)) {
    row.classList.add("warn");
    body.append(el("p", null, el("strong", null, "未运行"), d.stale ? "：程序的位置已变更。" : "：登录项已被移除。"));
    act.append(el("button", { type: "button", class: "btn small primary", onclick: () => turnOn(a.every, true) }, "修复"));
    return;
  }
  const sel = el("select", { "aria-label": "整理间隔", onchange: async () => {
    try { const r = await api("/api/auto", { enabled: true, every: Number(sel.value) }); toast(`已改为${every(r.auto.every)}整理一次`); }
    catch (err) { toast(err.message); }
    await nav.refresh();
  } }, a.intervals.map((m) => el("option", { value: m, selected: m === a.every }, every(m))));
  const n = S.state.notify;              // what macOS says of the app's notifications (luoke-core's launcher)
  if (n && (n.authorization === "denied" || n.alert === "disabled")) {
    row.classList.add("warn");
    body.append(el("p", { class: "warn-text" }, `macOS 没有允许${notifier()}显示通知，提醒不会出现在你面前。请在「系统设置 → 通知 → ${notifier()}」里打开「允许通知」。`),
      el("p", null, el("button", { type: "button", class: "btn small primary", onclick: openNotifySettings }, "打开通知设置")));
  } else if (n && n.authorization === "not_determined") {
    body.append(el("p", { class: "warn-text" }, `${notifier()}还没有获得通知权限。点「发送测试通知」，在 macOS 的询问里选「允许」。`));
  }
  act.append(sel);
  body.append(el("p", { class: a.last && !a.last.ok ? "warn-text" : null }, lastText(a)));
  act.append(el("button", { type: "button", class: "btn small", onclick: (e) => withBusy(e.currentTarget, "正在发送…", testNotify) }, "发送测试通知"),
    el("button", { type: "button", class: "btn small", onclick: disable }, "关闭"));
}

// macOS asks only once; after a no, only the user can allow notifications, in System Settings
async function openNotifySettings() {
  let r;
  try { r = await api("/api/auto/notify-settings", {}); } catch (err) { toast(err.message); return; }
  if (!r.opened) { toast(`请打开「系统设置 → 通知 → ${notifier()}」，打开「允许通知」。`); return; }
  toast("已打开系统设置。打开「允许通知」后回到这里，提示会自动消失。");
  const again = () => { if (document.visibilityState === "visible") { document.removeEventListener("visibilitychange", again); nav.refresh(); } };
  document.addEventListener("visibilitychange", again);
}

// whether a notification reaches the user at all: macOS may not have allowed them, or a Focus may hold them
async function testNotify() {
  let r;
  try { r = await api("/api/auto/test-notify", {}); } catch (err) { toast(err.message); return; }
  toast(r.result === "not allowed"
    ? `macOS 没有允许${notifier()}发通知。请在「系统设置 → 通知 → ${notifier()}」里打开。`
    : `已发送测试通知。没有看到的话，请在「系统设置 → 通知」里检查${notifier()}，并确认没有开着专注模式。`);
  await nav.refresh();                   // the settings row says again whether notifications are allowed
}

async function enable() {
  const d = S.state.auto.desktop;
  const sel = el("select", { "aria-label": "整理间隔" }, S.state.auto.intervals.map((m) => el("option", { value: m, selected: m === S.state.auto.every }, every(m))));
  const ok = await confirmDialog("开启后台自动整理", [
    el("p", { class: "dialog-line" }, d.supported ? "在后台每" : "程序开着时，每", sel, "："),
    el("ul", null, ["同步并分析新邮件", "有要回复、加日程或跟进的邮件时通知你；每晚 20:00 发一条「今天处理了 N 封」",
      "标为已读，按你确认过的习惯归档或加标签；都能在「今日处理」里撤销"].map((x) => el("li", null, x))),
    el("p", { class: "hint" }, S.state.accounts.some((x) => x.own)
      ? "不会退订或发信。设为螺壳自己的邮箱里，建议删除的会删除，其他邮箱不会。"
      : "不会删除邮件、退订或发信。"),
    d.in_luoke ? el("p", { class: "hint" }, "第一次通知时，请在系统提示里选「允许」。")
      : d.supported ? el("p", { class: "hint" }, `会添加一个登录项（可能显示为 python3）和应用「${d.app_name}」，关闭时一并移除。第一次通知时，请在系统提示里选「允许」。`) : null,
  ], "开启");
  if (ok) await turnOn(Number(sel.value), false);
}

async function turnOn(minutes, repair) {
  let r;
  try { r = await api("/api/auto", { enabled: true, every: minutes, consent: true }); }
  catch (err) { toast(err.message); await nav.refresh(); return; }
  if (r.handoff) return waitForService(repair);
  toast(r.message);
  await nav.refresh();
}

// this page's server steps aside; the login item takes over the same address
async function waitForService(repair) {
  endPage(repair ? "正在重新启动后台服务…" : "正在切换到后台运行…", "完成后页面会自动刷新。");
  const t0 = Date.now();
  while (Date.now() - t0 < 45000) {
    await new Promise((res) => setTimeout(res, 700));
    try {
      const p = await api("/api/ping");
      if (p.service) { location.reload(); return; }
    } catch (e) { /* the old server is gone, the new one isn't listening yet */ }
  }
  endPage("后台服务未能启动", "请在终端运行 python3 -m luoke_mail 重新启动，终端中会显示原因。");
}

async function disable() {
  const a = S.state.auto;
  const leaving = a.service && !a.desktop.in_luoke;
  const sure = await confirmDialog("关闭后台自动整理", [
    el("p", null, a.desktop.supported && !a.desktop.in_luoke ? `将移除登录项和「${a.desktop.app_name}」，不再定时整理和发送通知。` : "将不再定时整理和发送通知。"),
    leaving ? el("p", null, "螺壳邮件助手将随之退出。之后可在终端运行 python3 -m luoke_mail 重新打开。") : null,
  ], "关闭");
  if (!sure) return;
  try {
    const r = await api("/api/auto", { enabled: false, every: a.every });
    if (r.exiting) { endPage("已关闭后台自动整理", "螺壳邮件助手已退出，可以关闭此页面。之后可在终端运行 python3 -m luoke_mail 重新打开。"); return; }
    toast(r.message);
  } catch (err) { toast(err.message); }
  await nav.refresh();
}
