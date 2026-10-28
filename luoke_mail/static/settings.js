// 设置: everything that applies to the whole app -- the mailbox, the model, processing mode, background
// sorting, what a sync reads, and the local data. Pages only show a one-line hint where it matters.
import { $, S, api, confirmDialog, el, endPage, mailboxes, mark, nav, setStatus, toast } from "./core.js";
import { renderAutoRow } from "./auto.js";
import { editMailbox } from "./setup.js";
import { icon } from "./icons.js";

// a setting row (luoke-brand DESIGN.md 6): icon, the title over a note, the control on the right
function row(id, label, ico, ...body) {
  return el("div", { class: "set-row", id }, el("span", { class: "set-icon" }, icon(ico)), el("div", { class: "set-label" }, label),
    el("div", { class: "set-body" }, ...body), el("div", { class: "set-act" }));
}
function group(title, ...rows) {
  return el("section", null, el("h3", { class: "set-group-title" }, title), el("div", { class: "set-group" }, ...rows.filter(Boolean)));
}

// 外观: follow the system, or light / dark; kept in this browser
const THEME_KEY = "luoke-mail.theme";
export function applyTheme() {
  let t = null;
  try { t = localStorage.getItem(THEME_KEY); } catch (e) { /* the system's */ }
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  return t || "system";
}
function themeRow() {
  const r = row("set-theme", "外观", "monitor");
  const now = applyTheme();
  const seg = el("div", { class: "segmented", role: "radiogroup", "aria-label": "外观" },
    [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]].map(([v, label]) => el("button", { type: "button", role: "radio",
      "aria-checked": String(v === now), onclick: () => {
        try { if (v === "system") localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, v); } catch (e) { /* this time only */ }
        if (v === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = v;
        seg.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-checked", String(["system", "light", "dark"][i] === v)));
      } }, label)));
  r.querySelector(".set-act").append(seg);
  return r;
}

async function setOwn(a) {
  if (!a.own) {
    const ok = await confirmDialog(`把 ${a.address} 设为螺壳自己的邮箱`, [
      el("p", null, "这个邮箱的邮件分析后会直接处理：该归档的归档，该删的移到垃圾箱；可疑的和要你处理的留在收件箱。"),
      el("p", { class: "hint" }, "每封都列在「今日处理」里，可以撤销；移到垃圾箱的，在邮箱清空垃圾箱之前都能撤销。只用于专给螺壳的邮箱，不要设在日常用的邮箱上。"),
    ], "设为螺壳自己的邮箱");
    if (!ok) return;
  }
  try { await api("/api/mail/own", { source: a.key, own: !a.own }); }
  catch (err) { toast(err.message); return; }
  await nav.refresh();
}

let sortRow = null, signRow = null, dataRow = null;        // written in index.html; kept here, as clearing the list would drop them
export function showSettings() {
  sortRow = sortRow || $("set-sort");
  signRow = signRow || $("set-sign");
  dataRow = dataRow || $("set-data");
  const box = $("settings");
  box.textContent = "";
  const e = S.state.engine;
  const accounts = S.state.accounts;
  // one row per mailbox: the address, what it is, its buttons; then one to add another
  const mails = accounts.map((a, i) => {
    const p = S.state.providers.find((x) => x.key === a.provider);
    const r = row(i ? null : "set-mail", el("span", { class: "mono" }, a.address), "inbox",
      el("p", { class: "hint" }, [p ? `${p.name} · IMAP` : "IMAP", a.own ? "螺壳自己的邮箱" : null].filter(Boolean).join(" · "),
        a.has_secret ? "" : el("span", { class: "warn-text" }, " · 授权码未保存")));
    r.querySelector(".set-icon").replaceChildren(mark(a.key, a.address));      // the same mark as in the sidebar
    r.querySelector(".set-act").append(
      el("button", { type: "button", class: "btn small ghost", onclick: () => setOwn(a) }, a.own ? "改为我的邮箱" : "设为螺壳自己的邮箱"),
      el("button", { type: "button", class: "btn small", onclick: () => editMailbox(a.key) }, "修改"),
      el("button", { type: "button", class: "btn small ghost", onclick: () => removeMailbox(a) }, "移除"));
    return r;
  });
  const add = row(accounts.length ? "set-mail-add" : "set-mail", accounts.length ? "添加另一个邮箱" : "连接邮箱", "plus",
    el("p", { class: "hint" }, accounts.length && !accounts.some((a) => a.own)
      ? "推荐给螺壳一个专用邮箱，把订阅、通知转发过去，由它自动整理。"
      : "每个邮箱的规则、清理和精选分开管理。"));
  add.querySelector(".set-act").append(el("button", { type: "button", class: "btn small" + (accounts.length ? "" : " primary"),
    onclick: () => editMailbox(null) }, accounts.length ? "添加" : "连接邮箱"));

  const hm = S.state.hub_model;          // the app: 「螺壳主控 · 模型名」 and a ready mark (luoke-brand spec, luoke-mail#6)
  const model = row("set-model", "本机模型", "cpu", e.via === "hub"
    ? el("p", null, "螺壳", hm && hm.name ? el("span", { class: "mono" }, ` · ${hm.name}`) : "",
      hm && hm.ready ? el("span", { class: "chip success" }, "就绪") : "")
    : e.model
      ? el("p", null, el("span", { class: "mono" }, e.model), el("span", { class: "hint" }, ` · ${e.engine_name || e.base_url}`))
      : el("p", null, "未选择"));
  if (S.state.hub_only) {
    model.querySelector(".set-body").append(el("p", { class: "hint" }, "由螺壳按这台 Mac 挑选；要换档，在螺壳的设置里换。"),
      el("p", { class: "hint", id: "set-model-note" }, S.model ? S.model.text : ""));
  } else model.querySelector(".set-act").append(el("button", { type: "button", class: "btn small", onclick: () => nav.go("model") }, "更换模型"));

  const mode = row("set-mode", "处理模式", "mail-check");
  renderModeRow(mode);
  const auto = row("set-auto", "后台自动整理", "refresh-cw");
  renderAutoRow(auto);
  const power = row("set-power", "功耗", "zap");
  renderPowerRow(power);
  const talk = row("set-threads", "往来邮件", "users");
  renderThreadsRow(talk);
  const flow = row("set-flow", "已读", "mail-open");
  renderFlowRow(flow);
  const m2 = S.state.memory || {};
  const memory = row("set-memory", "记忆", "brain",
    el("p", null, [`关于你 ${m2.fact || 0} 条`, `关注的话题 ${m2.interest || 0} 个`, m2.dislike ? `不感兴趣 ${m2.dislike} 个` : null,
      m2.proposed ? `${m2.proposed} 个问题想问你` : null].filter(Boolean).join(" · ")));
  memory.querySelector(".set-act").append(el("button", { type: "button", class: "btn small", onclick: () => nav.go("memory") }, "查看"));
  // the sync range and the data rows are written in index.html: they join their groups here
  // grouped by what belongs together: which mail and how it is sorted (整理), then the app itself (通用)
  box.append(group("邮箱", ...mails, add), group("整理", sortRow, auto, flow, talk, signRow, mode),
    group("通用", themeRow(), model, power, memory, dataRow));
  renderScope();
  $("settings-store-note").textContent = S.state.store.plaintext
    ? "注意：在此系统上，授权码以明文保存在本机文件中（仅你的账户可读）。"
    : `授权码保存在${S.state.store.label}中，其他数据保存在本机螺壳的数据目录。`;
}

// ---- whose turn it is in a conversation: on by default, reads the sent folder ----------------------------
function renderThreadsRow(r) {
  const body = r.querySelector(".set-body"), act = r.querySelector(".set-act");
  body.textContent = "";
  act.textContent = "";
  const on = !!(S.state.threads && S.state.threads.on);
  const box = el("input", { type: "checkbox", checked: on, onchange: async () => {
    try { S.state.threads = await api("/api/threads", { on: box.checked }); toast(box.checked ? "已开启" : "已关闭"); }
    catch (err) { toast(err.message); }
    renderThreadsRow(r);
  } });
  box.setAttribute("aria-label", "提醒在等对方回复的邮件");
  act.append(el("label", { class: "check" }, box));
  body.append(el("p", null, "提醒在等对方回复的邮件"), el("p", { class: "hint" }, "会读「已发送」里最近一个月的邮件，只在本机分析。"));
}

// ---- what was sorted is marked read (ADR 0016): reviewed in 今日处理 -------------------------------------------
function renderFlowRow(r) {
  const body = r.querySelector(".set-body"), act = r.querySelector(".set-act");
  body.textContent = "";
  act.textContent = "";
  const read = el("input", { type: "checkbox", checked: !(S.state.flow && S.state.flow.mark_read === false), onchange: async () => {
    try { S.state.flow = await api("/api/flow", { mark_read: read.checked }); toast(read.checked ? "已开启" : "已关闭"); }
    catch (err) { toast(err.message); }
  } });
  read.setAttribute("aria-label", "分析过的邮件标为已读");
  act.append(el("label", { class: "check" }, read));
  body.append(el("p", null, "分析过的邮件标为已读"));
}

// ---- 功耗: how much of the time the local model may run ------------------------------------------------
const POWER = [["full", "全速", "模型连续运行，速度最快，风扇可能全速转。"],
  ["balanced", "均衡", "每次调用后休息同样长的时间，模型约一半时间在运行，速度约为全速的一半。"],
  ["quiet", "安静", "每次调用后休息三倍的时间，模型约四分之一时间在运行，速度约为全速的四分之一。"]];

function renderPowerRow(r) {
  const body = r.querySelector(".set-body"), act = r.querySelector(".set-act");
  body.textContent = "";
  act.textContent = "";
  if (S.state.engine.via === "hub") {
    body.append(el("p", null, "由螺壳统一管理。"));
    return;
  }
  const level = (S.state.power && S.state.power.level) || "full";
  const sel = el("select", { "aria-label": "功耗", onchange: async () => {
    try { S.state.power = await api("/api/power", { level: sel.value }); toast(`功耗已改为「${POWER.find((p) => p[0] === sel.value)[1]}」`); }
    catch (err) { toast(err.message); }
    renderPowerRow(r);
  } }, POWER.map(([v, label]) => el("option", { value: v, selected: v === level }, label)));
  act.append(sel);
  body.append(el("p", null, POWER.find((p) => p[0] === level)[2]));
}

// ---- removing a mailbox ------------------------------------------------------------------------------
async function removeMailbox(a) {
  const last = S.state.accounts.length === 1;
  const ok = await confirmDialog(`移除 ${a.address}`, [
    el("p", null, "螺壳不再读这个邮箱，并删掉本机存的授权码，以及它的分析结果、规则、清理和精选记录、操作记录。"),
    el("p", { class: "hint" }, "邮箱里的邮件和关于你的记忆不受影响。可以重新连接，但删掉的记录找不回来。"),
    last && S.state.auto.enabled ? el("p", null, "这是唯一连接的邮箱，后台自动整理也将关闭。") : null,
  ], "移除");
  if (!ok) return;
  let r;
  try { r = await api("/api/mail/remove", { key: a.key }); }
  catch (err) { toast(err.message); return; }
  if (r.exiting) { endPage("已移除邮箱，后台自动整理已关闭", "螺壳邮件助手已退出，可以关闭此页面。之后可在终端运行 python3 -m luoke_mail 重新打开。"); return; }
  await nav.refresh();
  toast(r.message);
}

// ---- processing mode -------------------------------------------------------------------------------
function renderModeRow(r) {
  const body = r.querySelector(".set-body"), act = r.querySelector(".set-act");
  body.textContent = "";
  act.textContent = "";
  if (!mailboxes().length) {
    body.append(el("p", null, "未开启"), el("p", { class: "hint" }, "连接邮箱后可开启。"));
    return;
  }
  if (S.state.mode.write) {
    body.append(el("p", null, "已开启：批量移到垃圾箱、垃圾邮件时，每次都需要你确认。"));
    act.append(el("button", { type: "button", class: "btn small", onclick: disableWrite }, "关闭"));
  } else {
    body.append(el("p", null, "未开启：批量移到垃圾箱、垃圾邮件前需要开启。"));
    act.append(el("button", { type: "button", class: "btn small primary", onclick: enableWrite }, "开启处理模式"));
  }
}

export async function enableWrite() {
  const ok = await confirmDialog("开启处理模式", [
    el("p", null, "开启后，可以一次处理多封邮件："),
    el("ul", null, ["移到垃圾箱或垃圾邮件（邮箱一般保留 30 天，可以恢复）",
      "在订阅体检和清理里，清掉一个发件人的历史邮件"].map((x) => el("li", null, x))),
    el("p", { class: "hint" }, "归档、标为已读、加标签、存草稿和退订，不开也能用。螺壳不会发信、转发或永久删除邮件。"),
  ], "开启");
  if (!ok) return false;
  await api("/api/mode", { write: true, consent: true });
  await nav.refresh();
  toast("处理模式已开启");
  return true;
}

// a bulk step that can't be taken back, clicked while 处理模式 is off: the dialog that says what it means, then on
// with the step if the user switches it on (luoke-mail#18: a greyed button with only a tooltip said nothing)
export async function needWrite(needed = true) {
  if (!needed || S.source === "demo" || S.state.mode.write) return true;
  return enableWrite();
}
export const NEEDS_MODE = "移到垃圾箱前需要先开启处理模式，点击时会说明。";

async function disableWrite() {
  await api("/api/mode", { write: false });
  await nav.refresh();
  toast("已关闭处理模式");
}

// the one line a page shows about the mode (demo note, or why the buttons are greyed out)
export function renderModeHint(box, after) {
  box.textContent = "";
  box.className = "mode-hint";
  if (S.source === "demo") {
    box.classList.add("demo");
    box.append(el("span", null, "示例数据：所有操作仅在示例中进行，不会更改邮箱。"));
  }                                     // 处理模式 off: nothing to say here, the buttons that need it say so
  box.hidden = !box.childNodes.length;
}

// ---- what a sync reads, and the signature on drafts --------------------------------------------------
export function scopeText() {
  const f = S.state.fetch;
  return `范围：最近 ${f.days} 天，每次最多 ${f.limit} 封`;
}

function renderScope() {
  const f = S.state.fetch;
  $("f-limit").value = f.limit;
  $("f-days").value = f.days;
  $("signature").value = S.state.profile.signature || "";
}

async function saveScope(ev) {
  const where = ev && ev.target.id === "signature" ? "sign-status" : "scope-status";   // the note shows on the row that changed
  try {
    await api("/api/profile", { limit: Number($("f-limit").value), days: Number($("f-days").value), signature: $("signature").value });
    await nav.refresh();
    setStatus(where === "sign-status" ? "scope-status" : "sign-status", "");   // only the row just changed says 已保存
    setStatus(where, "已保存", "ok");
  } catch (err) { setStatus(where, err.message, "err"); }
}

export function initSettings() {
  // a row with one switch: its title and text flip it too, not only the small switch
  $("settings").addEventListener("click", (e) => {
    const r = e.target.closest(".set-row");
    if (!r || e.target.closest("button, a, input, select, label, textarea")) return;
    if (!e.target.closest(".set-label, .set-body > p:first-child")) return;
    const boxes = r.querySelectorAll('.set-act input[type="checkbox"]');
    if (boxes.length === 1) boxes[0].click();
  });
  ["f-limit", "f-days", "signature"].forEach((id) => $(id).addEventListener("change", saveScope));
  $("forget").addEventListener("click", async () => {
    // the memory is the user's, shared by every luoke tool: it is kept unless they tick it (top-level ADR 0002)
    const memory = el("input", { type: "checkbox" });
    const ok = await confirmDialog("清除本机数据", [
      el("p", null, "删除邮箱授权码、模型 API Key、规则、分析结果和操作记录，并关闭后台自动整理。邮箱里的邮件不受影响。"),
      el("label", { class: "check" }, memory, " 同时删除邮件助手写进记忆的内容"),
      el("p", { class: "hint" }, "不勾选，记忆留给其他螺壳工具用。勾选也只删邮件助手自己记下和推测的；你确认过、改过或自己写的会保留。"),
    ], "清除");
    if (!ok) return;
    const r = await api("/api/forget", { memory: memory.checked });
    if (r.exiting) { endPage("已清除", "螺壳邮件助手已退出，可以关闭此页面。"); return; }
    location.reload();
  });
}
