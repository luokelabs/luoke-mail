// 今日处理: what was done with each message today -- archived by a habit, marked read and left in the inbox, waiting
// for you, suspicious, handled by you -- each taken back or changed in one click (top-level ADR 0016). A change is
// what the memory learns from.
import { $, S, api, el, nav, toast, withBusy } from "./core.js";
import { icon } from "./icons.js";

const RESULT = {
  auto_archive: "已自动归档", auto_trash: "已自动删除", auto_read: "已自动标为已读", auto_label: "已自动加标签", auto_complete: "已自动归档",
  kept: "已读，留在收件箱", waiting: "等你处理", suspicious: "可疑", elsewhere: "你在邮箱中处理了", unsorted: "未分析",
  you_archive: "你已归档", you_trash: "你已删除", you_spam: "你已移到垃圾邮件", you_complete: "你已完成并归档",
  you_flag: "你已加星标", you_read: "你已标为已读", you_skip: "你已跳过", you_skipped: "你已跳过", you_done: "你已处理",
  you_draft: "你已保存回复草稿",
};
const SUGGEST = { reply: "回复", schedule: "加入日程", follow_up: "加星标", archive: "归档", ignore: "删除" };   // triage.ACTIONS
let view = null;
let day = null;
let only = null;                                  // the group the header filters to, or all

export async function showReview() {
  try { view = await api(`/api/review?source=${S.source}` + (day ? `&day=${day}` : "")); }
  catch (err) { $("review").textContent = ""; $("review").append(el("p", { class: "status err" }, err.message)); return; }
  render();
  if (!day || day === view.days[0]) api("/api/review/seen", { source: S.source }).catch(() => {});
}

// what each outcome is grouped under, in this order; the header counts the same groups, so the numbers add up
const SECTIONS = [
  ["needs", "等你处理", "等你处理", (x) => !x.older && (x.result === "waiting" || x.result === "suspicious")],
  ["older", "以前的未读", "以前的未读", (x) => x.older],
  ["kept", "已读，留在收件箱", "留在收件箱", (x) => !x.older && (x.result === "kept" || x.result === "unsorted")],
  ["auto", "已自动处理", "自动处理", (x) => !x.older && x.result.startsWith("auto_")],
  ["you", "你处理的", "你处理的", (x) => !x.older && (x.result.startsWith("you_") || x.result === "elsewhere")],
];

function render() {
  const box = $("review");
  box.textContent = "";
  const days = el("div", { class: "segmented", role: "radiogroup", "aria-label": "日期" },
    ...view.days.map((d, i) => el("button", { type: "button", role: "radio", "aria-checked": String(d === view.day),
      onclick: () => { day = d; only = null; showReview(); } }, i === 0 ? "今天" : i === 1 ? "昨天" : d.slice(5).replace("-", "/"))));
  const groups = SECTIONS.map(([key, title, short, test]) => [key, title, view.items.filter(test), short]);
  if (only && !groups.find(([k, , rows]) => k === only && rows.length)) only = null;
  const tile = (key, label, n) => el("button", { type: "button", class: "stat", "aria-pressed": String(only === key),
    disabled: n ? null : "", onclick: () => { only = key; render(); } },
    el("span", { class: "n" }, String(n)), el("span", { class: "l" }, label));
  const stats = el("div", { class: "stats filter" }, tile(null, "全部", view.items.length),
    ...groups.map(([key, , rows, short]) => tile(key, short, rows.length)));
  box.append(el("div", { class: "review-days" }, days), stats);
  if (!view.items.length) { box.append(el("p", { class: "empty-state" }, "这一天没有处理过的邮件。")); return; }
  for (const [key, title, rows] of groups) {
    if (!rows.length || (only && only !== key)) continue;
    const head = el("h3", { class: "subhead" }, title, el("span", { class: "count" }, String(rows.length)));
    const late = key === "older" ? rows.filter((x) => x.late).length : 0;
    if (key === "needs" || late) head.append(el("button", { type: "button", class: "btn small push", onclick: () => nav.go("inbox") }, icon("inbox"), "去收件箱处理"));
    box.append(head);
    if (key === "older") box.append(el("p", { class: "hint shelf-hint" }, "以前一直没读的邮件，螺壳看过并标为已读。" + (late ? `其中 ${late} 封还要你处理。` : "")));
    box.append(el("div", { class: "list" }, ...rows.map(row)));
  }
}

const LATE = { overdue: "已过期", due: "快到期", old: "很久以前收到" };
function when(x) {
  if (x.late === "overdue" && x.due_date) return `${LATE.overdue}（${x.due_date.slice(0, 10)}）`;
  if (x.late === "due" && x.due_date) return `${LATE.due}（${x.due_date.slice(0, 10)}）`;
  if (x.late === "old" && x.date) return `${x.date.slice(0, 10)} 收到`;
  return "";
}
// the result's badge: what needs the user stands out
const TONE = { suspicious: "danger" };          // red is for suspicious only; 等你处理 stays neutral

function row(x) {
  const r = el("div", { class: "mem-row review-row" + (x.result === "suspicious" ? " suspicious" : "") });
  // what the card offered: suspicious mail goes to junk, not the trash (luoke-mail#13)
  const suggested = x.suspicious ? "建议：移到垃圾邮件（可疑）" : x.action && SUGGEST[x.action] ? `建议：${SUGGEST[x.action]}` : "";
  const what = (RESULT[x.result] || "已处理") + (x.label ? `「${x.label}」` : "");     // never a code on the page
  const late = x.late ? el("span", { class: "chip warning" }, when(x) || LATE[x.late]) : "";
  r.append(el("div", { class: "mem-text" }, el("span", { class: "chip " + (TONE[x.result] || "soft") }, what), late, el("span", null, x.subject)),
    el("div", { class: "mem-meta muted" }, [x.from, suggested, x.summary].filter(Boolean).join(" · ")));
  const acts = el("div", { class: "card-actions" });
  const busy = (label, pending, fn) => el("button", { type: "button", class: "btn small", onclick: (e) => withBusy(e.currentTarget, pending, fn) }, label);
  const act = async (body) => {
    try { view = await api("/api/review/act", { source: S.source, id: x.id, day: view.day, ...body }); }
    catch (err) { toast(err.message); return; }
    toast(view.message || "已完成");
    render();
  };
  if (x.result.startsWith("auto_") && x.log_id) acts.append(busy("撤销", "正在撤销…", () => act({ op: "undo" })));
  if (x.state === "pending" && x.read_by_us) acts.append(busy("标为未读", "正在标记…", () => act({ op: "unread" })));
  if (x.state === "pending") {
    const sel = el("select", { "aria-label": "改为" }, el("option", { value: "" }, "改为"),
      ...Object.entries(SUGGEST).filter(([a]) => a !== x.action).map(([a, l]) => el("option", { value: a }, l)));
    sel.addEventListener("change", async () => {
      const a = sel.value;
      sel.value = "";
      if (!a) return;
      try { await api("/api/items/override", { source: S.source, ids: [x.id], action: a }); }
      catch (err) { toast(err.message); return; }
      toast(`已改为「${SUGGEST[a]}」`);
      showReview();
    });
    acts.append(el("span", { class: "override" }, "改为", icon("chevron-down", 14), sel));
  }
  if (acts.childElementCount) r.append(acts);
  return r;
}
