// Subscription health. Every decision is stored per sender, so handled senders move to "已处理" and stay
// there after a refresh or a re-scan; every reversible step offers 撤销.
import { icon } from "./icons.js";
import { $, S, api, confirmDialog, el, nav, setStatus, toast, withBusy } from "./core.js";
import { exportGmailFilters } from "./gmail.js";
import { loadInbox } from "./inbox.js";
import { needWrite, renderModeHint } from "./settings.js";

let pollTimer = null;
let view = null;
const METHOD = { one_click: "支持一键退订", web: "通过网页退订", mailto: "通过邮件退订",
  body_link: "正文中有退订链接", reply: "回复关键词退订" };
const DONE = { unsubscribed: "已退订", keep: "已保留", archive_rule: "以后都归档", trash_rule: "以后都移到垃圾箱",
  spam_rule: "以后都移到垃圾邮件", junk_rule: "以后都移到垃圾邮件", unsubscribe_opened: "已打开退订页面", digest: "已加入精选" };
const RULE_STATES = ["archive_rule", "trash_rule", "spam_rule", "junk_rule"];
const canWrite = () => S.source === "demo" || !!S.state.mode.write;
const quiet = (s) => s.verdict === "keep" || s.verdict === "important";      // read often, or worth keeping unread

export const human = (n) => (n >= 1000 * 1024 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

// 7–365 whole days, written back so the box shows what the check uses (luoke-mail#27)
function clampDays() { const d = $("subs-days"); d.value = String(Math.min(365, Math.max(7, Math.round(Number(d.value) || 60)))); }

export function initSubs() {
  $("subs-run").addEventListener("click", run);
  $("subs-days").addEventListener("change", clampDays);
}

export async function showSubs() {
  renderModeHint($("subs-mode-hint"), () => view && render(view));
  $("subs").textContent = "";
  try {
    const r = await api(`/api/subscriptions?source=${S.source}`);
    if (r.senders) render(r);
    else {
      setStatus("subs-status", "");
      $("subs").append(el("div", { class: "empty-state" }, el("p", null, "还没体检过。"), el("p", null, "设好天数，点「开始体检」。")));
    }
  } catch (err) { setStatus("subs-status", err.message, "err"); }
}

async function run() {
  $("subs-run").disabled = true;
  $("subs").textContent = "";                         // the "not yet" note goes once a check starts
  setStatus("subs-status", "正在读取邮件…", "busy");
  try {
    const job = await api("/api/subscriptions", { source: S.source, days: (clampDays(), Number($("subs-days").value)) });
    poll(job.id);
  } catch (err) { $("subs-run").disabled = false; setStatus("subs-status", err.message, "err"); }
}

function poll(id) {
  clearTimeout(pollTimer);
  api(`/api/jobs/${id}`).then((j) => {
    if (j.status === "running") { setStatus("subs-status", j.stage || "处理中…", "busy"); pollTimer = setTimeout(() => poll(id), 800); return; }
    $("subs-run").disabled = false;
    if (j.status === "error") return setStatus("subs-status", j.error, "err");
    render(j.result);
  }).catch((err) => { $("subs-run").disabled = false; setStatus("subs-status", err.message, "err"); });
}

async function act(s, op, extra = {}) {
  try {
    const r = await api("/api/senders/act", { source: S.source, key: s.key, op });
    if (r.open_url) window.open(r.open_url, "_blank", "noopener,noreferrer");
    if (r.mailto) location.href = r.mailto;
    toast(r.message, r.log_id ? async () => {
      const u = await api("/api/undo", { source: S.source, log_id: r.log_id });
      toast(u.message); showSubs(); loadInbox();
    } : null);
    if (r.subscriptions) render(r.subscriptions, extra.followUp && r.status === "unsubscribed" ? s.key : null);
    loadInbox();
    nav.refresh();
  } catch (err) { toast(err.message); }
}

async function cleanHistory(s, btn) {
  let p;
  try { p = await withBusy(btn, "正在查找…", () => api("/api/senders/act", { source: S.source, key: s.key, op: "history_preview" })); }
  catch (err) { toast(err.message); return; }
  if (!p.count) { toast(`邮箱中没有「${s.name || s.from_addr}」的邮件。`); return; }
  const when = p.purge_days ? `垃圾箱中的邮件会在 ${p.purge_days} 天后被邮箱自动清空。` : "垃圾箱中的邮件会被邮箱定期清空。";
  const ok = await confirmDialog("清理历史邮件", [
    el("p", null, `将「${s.name || s.from_addr}」的 ${p.count} 封邮件（约 ${human(p.bytes)}，含已归档邮件）移到垃圾箱。`),
    el("p", { class: "hint" }, `${when}在此之前可以在「操作记录」中撤销。`),
  ], `移到垃圾箱（${p.count} 封）`);
  if (ok) await withBusy(btn, `正在移到垃圾箱（${p.count} 封）…`, () => act(s, "trash_history"));
}

async function blockGuide(s) {
  const b = (view && view.provider && view.provider.block) || {};
  const addr = s.from_addr || "";
  const domain = addr.split("@")[1] || "";
  const copy = (text, what) => el("button", { type: "button", class: "btn small", onclick: async () => {
    try { await navigator.clipboard.writeText(text); toast(`已复制${what}：${text}`); } catch (e) { toast(`请手动复制：${text}`); }
  } }, `复制${what}`);
  const ok = await confirmDialog(b.title || "在邮箱中拉黑", [
    el("p", null, "设好后，邮箱会直接拦下这个发件人之后的邮件。"),
    el("ol", null, (b.steps || []).map((x) => el("li", null, x))),
    el("div", { class: "card-actions" }, el("span", { class: "mono" }, addr), copy(addr, "地址"), domain ? copy(domain, "域名") : null),
  ], b.link ? b.link.label : "知道了");
  if (ok && b.link) window.open(b.link.url, "_blank", "noopener,noreferrer");
}

function render(r, justUnsubscribed = null) {
  view = r;
  const rows = r.senders;
  // anything the user already acted on is "handled"; whether a web unsubscribe worked is checked by later scans
  const pending = rows.filter((s) => !s.decision && !quiet(s));
  const regular = rows.filter((s) => !s.decision && quiet(s));
  const handled = rows.filter((s) => s.decision);
  const pendingBytes = pending.reduce((a, s) => a + (s.bytes || 0), 0);
  setStatus("subs-status", `${r.at ? r.at + " · " : ""}最近 ${r.days} 天共 ${r.scanned} 封邮件，来自 ${rows.length} 个发件人。`
    + (pending.length ? `建议处理 ${pending.length} 个，约占 ${human(pendingBytes)}。` : "")
    + (r.model_note ? ` ${r.model_note}` : ""), r.model_note ? "err" : "ok");
  const box = $("subs");
  box.textContent = "";
  // each section's senders in one list group (luoke-brand DESIGN.md 6)
  const head = (title, n) => el("h3", { class: "subhead" }, title, n ? el("span", { class: "count" }, String(n)) : null);
  const group = (list) => el("div", { class: "list" }, ...list.map((s) => card(s, s.key === justUnsubscribed)));
  box.append(head("建议处理", pending.length));
  box.append(pending.length ? group(pending) : el("p", { class: "empty-state" }, "没有建议处理的发件人。"));
  if (handled.length) box.append(head("已处理", handled.length), group(handled));
  if (regular.length) box.append(el("details", { class: "others" }, el("summary", null, `建议保留（${regular.length}）`), group(regular)));
}

function unsubLabel(s) {
  if (s.method === "one_click") return "一键退订";
  if (s.method === "web") return "打开退订网页";
  if (s.method === "mailto") return "发送退订邮件";
  if (s.method === "body_link") { try { return `打开退订链接（${new URL(s.body_link).hostname}）`; } catch (e) { return "打开退订链接"; } }
  if (s.method === "reply") return `回复「${s.reply_keyword || "TD"}」退订`;
  return null;
}

function card(s, justUnsubscribed = false) {
  const st = s.decision && s.decision.status;
  const readPct = s.count ? Math.round((s.read / s.count) * 100) : 0;
  const chip = st ? el("span", { class: "chip " + (s.still_sending ? "high" : "soft") }, DONE[st] + (s.decision.at ? " · " + s.decision.at.slice(5, 10).replace("-", "/") : ""))
    : el("span", { class: "chip " + (s.verdict === "never" ? "warn" : quiet(s) ? "soft" : "action") },
      { unsubscribe: "建议退订", filter: "无法退订", never: "可疑发件人", keep: "常读", important: "建议保留" }[s.verdict]);
  // a rule only acts when luoke-mail runs; on Gmail it can become a filter that runs on Google's side
  const gmailRule = RULE_STATES.includes(st) && s.rule && view.provider.gmail_filters && S.state.gmail_filter_actions[s.rule.action];
  const c = el("article", { class: "sub-card " + (st ? "handled" : "v-" + s.verdict) },
    el("div", { class: "sub-head" }, el("h4", null, s.name || s.from_addr), el("span", { class: "sub-tags" }, chip,
      gmailRule && s.rule.exported_at ? el("span", { class: "chip soft" }, "已导出到 Gmail") : null)),
    el("div", { class: "meta" }, [s.from_addr, `${s.count} 封，读过 ${readPct}%`, `约 ${human(s.bytes || 0)}`,
      s.last_date ? `最近 ${s.last_date.slice(0, 10)}` : null, s.method ? METHOD[s.method] : "无退订方式"].filter(Boolean).join(" · ")));
  // the meta line already has the counts and the way out: only a reason that says more is shown (subscriptions.py)
  if (!st && (s.verdict === "never" || s.verdict === "important") && s.why) c.append(el("p", { class: "reason" }, s.why));
  if (!st && s.method === "body_link") c.append(el("p", { class: "hint" }, "链接来自邮件正文，打开后请确认网址属于该发件人。"));
  if (st === "unsubscribe_opened" && !s.still_sending) c.append(el("p", { class: "hint" },
    "如果之后仍收到此发件人的邮件，下次体检时会提示你。"));
  if (s.still_sending) c.append(el("p", { class: "err" }, "退订后仍在发信。可以将之后的邮件移到垃圾箱，或在邮箱中拉黑。"));
  if (justUnsubscribed) c.append(el("p", { class: "hint" }, "已退订。是否同时清理此发件人的历史邮件？"));
  if (gmailRule && !s.rule.exported_at) c.append(el("p", { class: "hint" },
    "导出到 Gmail 后，由 Gmail 自动处理此发件人之后的邮件。"));

  const row = el("div", { class: "card-actions" });
  // every button answers at once; the slow ones (they wait on the mailbox or the sender's server) say what they do
  // the sender's main step is an outlined button, the rest quiet (one main action per screen: luoke-brand DESIGN.md 1)
  const btn = (label, onclick, opts = {}) => el("button", { type: "button", class: opts.primary ? "btn small" : "btn small ghost",
    title: opts.write && !canWrite() ? "移到垃圾箱前需要先开启处理模式，点击时会说明" : null,
    onclick: async (e) => {
      const b = e.currentTarget;
      if (opts.write && !canWrite() && !(await needWrite())) return;          // luoke-mail#18: say why, then go on
      return opts.self ? onclick(b) : withBusy(b, opts.pending || "处理中…", onclick);
    } }, label);
  const history = (primary) => btn("清理历史邮件", (b) => cleanHistory(s, b), { write: true, primary, self: true });
  const block = () => btn("去邮箱里拉黑", () => blockGuide(s), { self: true });
  // the rarer steps wait in a small 更多 menu, so a card keeps one short row of buttons
  const more = (...items) => {
    const d = el("details", { class: "more-menu" }, el("summary", { class: "btn small ghost" }, "更多", icon("chevron-down", 14)),
      el("div", { class: "menu" }, ...items));
    d.addEventListener("click", (e) => { if (e.target.closest(".menu button")) d.open = false; });
    return d;
  };

  if (!st) {
    if (s.verdict === "never") {
      row.append(btn("以后都移到垃圾邮件", () => act(s, "spam_rule"), { primary: true }), more(history(), block()));
    } else {
      const u = unsubLabel(s);
      if (u) row.append(btn(u, () => act(s, "unsubscribe", { followUp: true }), { primary: s.verdict === "unsubscribe", pending: "正在退订…" }));
      row.append(btn("以后都移到垃圾箱", () => act(s, "trash_rule"), { primary: s.verdict === "filter" }), btn("加入精选", () => act(s, "digest")));
      if (!quiet(s)) row.append(btn("保留", () => act(s, "keep")));
      row.append(more(history(), block()));
    }
  } else {
    if (s.still_sending) row.append(btn("以后都移到垃圾箱", () => act(s, "trash_rule"), { primary: true }), block());
    if (st === "unsubscribe_opened") row.append(btn(s.method === "reply" || s.method === "mailto" ? "再次发送" : "再次打开", () => act(s, "unsubscribe"), { pending: "正在退订…" }));
    if (gmailRule) row.append(btn(s.rule.exported_at ? "重新导出" : "导出到 Gmail",
      () => exportGmailFilters([s.rule], { ids: [s.rule.id], onDone: showSubs }), { primary: !s.rule.exported_at, self: true }));
    if (st !== "keep") row.append(history(justUnsubscribed));
    row.append(btn(RULE_STATES.includes(st) ? "删除规则" : st === "digest" ? "移出精选" : "移回建议处理", () => act(s, "reset")));
  }
  c.append(row, el("details", null, el("summary", null, "最近的邮件主题"), el("ul", null, s.subjects.map((x) => el("li", null, x)))));
  return c;
}
