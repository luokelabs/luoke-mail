// The workbench: sync new mail, see it sorted, act on it (one by one, by group, or "clean all noise"), undo.
import { $, S, api, confirmDialog, download, el, hasModel, nav, setStatus, toast, withBusy } from "./core.js";
import { ACTION_ORDER, actionLabel, clearPending, handlers, markPending, primaryOp, renderCard, renderGroup } from "./cards.js";
import { checkActivity, onActivity } from "./auto.js";
import { renderOldBar } from "./sweep.js";
import { openClean } from "./clean.js";
import { NEEDS_MODE, needWrite, renderModeHint, scopeText } from "./settings.js";

let view = null;          // last /api/items response
let filter = null;        // rack filter
let pollTimer = null;
let job = null;

const canWrite = () => S.source === "demo" || !!(S.state && S.state.mode.write);   // 处理模式: for bulk trash / junk
const canClean = (n) => canWrite() || n.trash.length + n.spam.length <= 1;

// ---- loading & rendering ---------------------------------------------------------------------------
// a sync, or background sorting, belongs to one mailbox: another mailbox's inbox neither shows it nor stops it
// (luoke-mail#4)
const ours = (j) => !!j && j.account === S.source;
function letGo() {
  clearTimeout(pollTimer);
  job = null;
  $("sync-progress").hidden = true;
  $("sync").disabled = false;
  $("sync-cancel").hidden = true;
  setStatus("sync-status", "");
}

export async function loadInbox() {
  if (job && !ours(job)) letGo();                    // another mailbox was shown: its job stays with it
  renderModeHint($("mode-hint"), loadInbox);
  $("sync-scope").textContent = S.source === "demo" ? "示例收件箱：44 封" : scopeText();
  $("sync-scope").disabled = S.source === "demo";
  if (S.source !== "demo") checkActivity();
  try {
    view = await api(`/api/items?source=${S.source}`);
  } catch (err) {
    view = null;
    $("cards").textContent = "";
    $("cards").append(el("p", { class: "empty-state" }, err.message));
    $("rack").hidden = $("clean-bar").hidden = true;
    return;
  }
  render();
}

function render() {
  document.querySelectorAll("[data-badge]").forEach((badge) => {
    badge.hidden = !(view && view.pending);
    badge.textContent = view ? view.pending : "";
  });
  renderRack();
  renderCleanBar();
  renderOldBar(view.old, () => openClean("inbox"));
  renderIntro();
  renderDue();
  renderWaiting();
  renderAuto();
  renderArchived();
  const box = $("cards");
  box.textContent = "";
  const due = ((view && view.due) || []).filter(matches).length;
  if (!view.entries.length && !(view.due || []).length) {
    box.append(el("div", { class: "empty-state" },
      el("p", null, S.state.synced[S.source] ? "全部处理完毕。" : "还没有待处理的邮件。"),
      el("p", null, "点击「同步新邮件」开始分析。")));
    return;
  }
  const visible = view.entries.filter((e) => matches(e.type === "group" ? e.items[0] : e.item));
  const prio = { high: 0, medium: 1, low: 2 };
  const rank = (e) => { const it = e.type === "group" ? e.items[0] : e.item;
    return it.suspicious ? 3.5 : !it.action ? 5 : ACTION_ORDER.indexOf(it.action) + (prio[it.priority] ?? 1) / 10; };
  const date = (e) => (e.type === "group" ? e.items[0] : e.item).date || "";
  visible.sort((a, b) => rank(a) - rank(b) || date(b).localeCompare(date(a)));   // same kind and priority: newest first
  if (!visible.length && !due) box.append(el("p", { class: "empty-state" }, "没有此类邮件。"));
  visible.forEach((e) => box.append(e.type === "group" ? renderGroup(e) : renderCard(e.item)));
}

// mail the user archived in the last days (often on the phone) that still has something for them: a date for the
// calendar, a question to answer -- shown apart from the inbox list, nothing moved back
// what needs the user and has a day to it, soonest first (a bill, a form, a meeting to answer)
// the filter above counts these too (view.counts), so it filters them too (luoke-mail#21)
function renderDue() {
  const box = $("due-box");
  const items = ((view && view.due) || []).filter(matches);
  box.hidden = !items.length;
  box.textContent = "";
  if (!items.length) return;
  box.append(el("h3", { class: "subhead" }, `快到期（${items.length}）`), ...items.map((it) => renderCard(it)));
}

// conversations the user wrote last in and has waited on for a few days: worth a follow-up
function renderWaiting() {
  const box = $("waiting-box");
  const convs = (view && view.waiting) || [];
  box.hidden = !convs.length;
  box.textContent = "";
  if (!convs.length) return;
  box.append(el("h3", { class: "subhead" }, `在等对方回（${convs.length}）`),
    ...convs.map((c) => el("div", { class: "waiting-line" },
      el("span", null, `「${c.subject}」· ${c.name} · ${c.since.slice(5).replace("-", "/")} 起`),
      c.what ? el("span", { class: "muted" }, c.what) : null)));
}

// once: 了解你 is on from the start (ADR 0013); said here, paused on the 记忆 page
function renderIntro() {
  const box = $("intro-box");
  box.hidden = !(S.state && S.state.learn_intro) || S.source === "demo";
  box.textContent = "";
  if (box.hidden) return;
  const ok = el("button", { type: "button", class: "btn small" }, "知道了");
  ok.addEventListener("click", async () => {
    try { await api("/api/learn/intro-seen", {}); S.state.learn_intro = false; } catch (e) { /* shown again next time */ }
    box.hidden = true;
  });
  box.append(el("div", { class: "auto-line" }, el("span", null,
    "螺壳会在空闲时读你的邮件（包括你发出的），了解你的工作、常联系的人和关注的话题，只存在这台 Mac 上。可以在「记忆」里暂停。"), ok));
}

// what confirmed habits did by themselves in the last day: always shown, each undone in one click (ADR 0012 amended)
function renderAuto() {
  const box = $("auto-box");
  const done = (view && view.auto) || [];
  box.hidden = !done.length;
  box.textContent = "";
  done.forEach((a) => {
    const btn = el("button", { type: "button", class: "btn small" }, "撤销");
    btn.addEventListener("click", () => withBusy(btn, "正在撤销…", async () => {
      const r = await api("/api/undo", { source: S.source, log_id: a.log_id });
      toast(r.message || "已撤销");
      loadInbox();
    }));
    box.append(el("div", { class: "auto-line" }, el("span", null, `${a.message}（${a.at.slice(11)}）`), btn));
  });
}

function renderArchived() {
  const box = $("archived-box");
  const items = (view && view.archived) || [];
  box.hidden = !items.length;
  box.textContent = "";
  if (!items.length) return;
  box.append(el("h3", { class: "subhead" }, `你归档的邮件里还有要处理的（${items.length} 封）`),
    el("p", { class: "hint" }, "你最近归档的邮件里，这些还有日程或要回复。"),
    ...items.map((it) => renderCard(it, { archived: true })));
}

function matches(it) {
  if (!filter) return true;
  if (filter === "suspicious") return it.suspicious;
  return it.action === filter && !it.suspicious;
}

function renderRack() {
  const rack = $("rack");
  const c = view.counts;
  rack.hidden = !view.pending;
  rack.textContent = "";
  const slot = (key, label, n) => el("button", { type: "button", class: n ? "" : "empty", "aria-pressed": String(filter === key),
    onclick: () => { filter = filter === key ? null : key; render(); } }, el("span", { class: "n" }, n), el("span", { class: "l" }, label));
  ACTION_ORDER.forEach((a) => rack.append(slot(a, actionLabel(a), a === "ignore" ? c.ignore - c.suspicious : c[a])));
  rack.append(slot("suspicious", "可疑", c.suspicious));
}

function noiseIds() {
  const out = { archive: [], trash: [], spam: [] };
  for (const e of view.entries) {
    for (const it of e.type === "group" ? e.items : [e.item]) {
      const p = it.action && primaryOp(it);
      if (p) out[p.op].push(it.id);
    }
  }
  return out;
}

function renderCleanBar() {
  const bar = $("clean-bar");
  bar.textContent = "";
  const n = noiseIds();
  const total = n.archive.length + n.trash.length + n.spam.length;
  bar.hidden = !total;
  if (!total) return;
  const parts = [n.archive.length && `归档 ${n.archive.length}`, n.trash.length && `移到垃圾箱 ${n.trash.length}`, n.spam.length && `垃圾邮件 ${n.spam.length}`].filter(Boolean);
  bar.append(el("div", { id: "clean-text" }, el("strong", null, `${total} 封可直接处理`), el("span", { class: "muted" }, `（${parts.join(" · ")}）`),
      canClean(n) ? null : el("span", { class: "hint", style: "display:block" }, NEEDS_MODE)),
    el("button", { type: "button", class: "btn primary", onclick: async (e) => {
      const btn = e.currentTarget;
      if (!canClean(noiseIds()) && !(await needWrite())) return;
      cleanAll({ currentTarget: btn });
    } }, "一键处理"));
}

async function cleanAll(e) {
  const btn = e.currentTarget;
  const n = noiseIds();
  const lines = [
    n.archive.length && `归档 ${n.archive.length} 封：标为已读，移出收件箱`,
    n.trash.length && `移到垃圾箱 ${n.trash.length} 封：标为已读，邮箱一般保留 30 天，可恢复`,
    n.spam.length && `移到垃圾邮件 ${n.spam.length} 封：可疑邮件`,
  ].filter(Boolean);
  const ok = await confirmDialog("一键处理", [el("ul", null, lines.map((l) => el("li", null, l))),
    el("p", { class: "hint" }, S.source === "demo" ? "示例数据，不会更改邮箱。" : "需要回复、加入日程和待跟进的邮件不受影响。可在操作记录中撤销。")], "处理");
  if (!ok) return;
  // a real mailbox takes a few seconds per step: say which step is running
  const steps = ["archive", "trash", "spam"].filter((op) => n[op].length);
  const text = $("clean-text");
  await withBusy(btn, "正在处理…", async () => {
    const results = [];
    try {
      for (const [i, op] of steps.entries()) {
        text.textContent = "";
        text.append(el("strong", null, steps.length > 1 ? `第 ${i + 1}/${steps.length} 步：` : ""), `正在${S.state.ops[op]} ${n[op].length} 封…`);
        const r = await api("/api/items/act", { source: S.source, ids: n[op], op });
        results.push(r);
        if (r.interrupted) break;            // the mailbox connection dropped: the next steps would fail the same way
      }
    } catch (err) {
      results.push({ ok: false, message: err.message });
    }
    const skipped = steps.length - results.length;
    if (skipped > 0) results.push({ ok: false, message: `其余 ${skipped} 步未执行，可以再点一次「一键处理」` });
    const logs = results.map((r) => r.log_id).filter(Boolean);
    const undo = logs.length ? async () => {
      for (const id of logs.reverse()) await api("/api/undo", { source: S.source, log_id: id });
      toast("已撤销");
      loadInbox();
    } : null;
    // only say "done" when the mailbox did all of it; otherwise say exactly what happened
    const all = results.every((r) => r.ok) && results.length === steps.length;
    toast(all ? `已处理 ${results.reduce((a, r) => a + r.done.length, 0)} 封` : results.map((r) => r.message).join("。"), undo);
  });
  loadInbox();
}

// ---- sync ------------------------------------------------------------------------------------------------
async function startSync() {
  if (!hasModel()) { toast("请先选择本机模型。"); nav.go("model"); return; }
  try {
    job = await api("/api/sync", { source: S.source });       // joins background sorting if it is already running
  } catch (err) {
    $("sync-progress").hidden = false;
    $("sync-bar").parentElement.hidden = true;
    setStatus("sync-status", err.message, "err");
    return;
  }
  $("sync-progress").hidden = false;
  $("sync").disabled = true;
  $("sync-cancel").hidden = false;
  poll();
}

// background sorting runs on its own: follow its progress like a sync, and when a run finished while
// nobody was watching, show what it found
let seenAuto;
let notReady = false;
function followActivity(r) {
  const note = $("model-note");
  note.hidden = !r.model;
  note.textContent = r.model ? r.model.text : "";
  const ready = notReady && !r.model;               // 螺壳主控 just got ready (the download finished)
  notReady = !!r.model;
  if (ready && nav.current() === "inbox" && !(job && job.status === "running") && !r.job
      && ((r.waiting || {})[S.source] || 0) > 0) {
    startSync();
    return;
  }
  const at = r.auto.last ? r.auto.last.at : null;
  const finished = seenAuto !== undefined && at !== seenAuto;
  seenAuto = at;
  if (nav.current() !== "inbox" || S.source === "demo") return;
  if (job && job.status === "running") return;
  const queued = (r.waiting_jobs || []).find((w) => w.kind === "sync" && ours(w));
  const mine = r.job && (r.job.kind === "sync" || r.job.kind === "auto") && ours(r.job) ? r.job : queued;
  if (mine) {
    job = mine;
    $("sync-progress").hidden = false;
    $("sync").disabled = true;
    poll();
  } else if (finished) {
    loadInbox();
  }
}

function poll() {
  clearTimeout(pollTimer);
  api(`/api/jobs/${job.id}`).then(async (j) => {
    if (!ours(j)) { letGo(); loadInbox(); return; }    // background sorting moved on to another mailbox
    job = j;
    $("sync-bar").style.width = (j.total ? Math.round((j.done / j.total) * 100) : 5) + "%";
    $("sync-bar").parentElement.hidden = j.status !== "running";       // finished: only the result stays
    const who = j.kind === "auto" ? "后台自动整理：" : "";
    if (j.status === "running") {
      setStatus("sync-status", `${who}${j.stage || "处理中"}${j.total ? `（${j.done}/${j.total}）` : ""}`, "busy");
      if (j.done && j.done % 3 === 0) loadInbox();
      pollTimer = setTimeout(poll, 1000);
      return;
    }
    $("sync").disabled = false;
    $("sync-cancel").hidden = true;
    if (j.status === "error") setStatus("sync-status", j.error, "err");
    else {
      const r = j.result || {};
      const how = [r.rule_differs && `${r.rule_differs} 封与规则不同`, r.errors && `${r.errors} 封分析失败`,
        r.waiting && `${r.waiting} 封将在模型准备好以后分析`].filter(Boolean).join("，");
      const gone = r.gone ? `${r.gone} 封已不在收件箱中，已从列表移除。` : "";
      const back = r.back ? `${r.back} 封仍在收件箱中，已重新列出。` : "";
      // cut short: what was read is kept, the next sync goes on (luoke-mail#19)
      setStatus("sync-status", who + (r.new ? `新邮件 ${r.new} 封${how ? `，其中 ${how}` : ""}。` : noNewText(r)) + gone + back + (r.cut || ""),
        r.cut ? "err" : "ok");
    }
    await nav.refresh();
    loadInbox();
  }).catch((err) => setStatus("sync-status", err.message, "err"));
}

// "nothing new" also points to the older mail outside the range
function noNewText(r) {
  return "没有新邮件。" + (r.old && S.source !== "demo" ? `更早的 ${r.old} 封可在「清理」中处理。` : "");
}

// ---- actions wired into the cards ----------------------------------------------------------------------
async function act(ids, op) {
  markPending(ids, `正在${S.state.ops[op] || "处理"}…`);
  try {
    const r = await api("/api/items/act", { source: S.source, ids, op });
    toast(r.message, r.log_id ? async () => {
      await api("/api/undo", { source: S.source, log_id: r.log_id }); toast("已撤销"); loadInbox();
    } : null);
  } catch (err) {
    toast(err.message);
  }
  clearPending(ids);
  loadInbox();
}

async function override(ids, action, sample, count) {
  const kind = sample.list_id ? "list" : "sender";
  const who = sample.list_id ? `邮件列表 ${sample.list_id}` : (sample.from_addr || "此发件人");
  // a rule fits mailing lists and bulk senders; for a person, one correction rarely means "always"
  const remember = el("input", { type: "checkbox", id: "remember-rule", checked: !!(sample.list_id || sample.group_key) });
  const label = actionLabel(action);
  // changed to archive or trash, it is usually meant now too (issue #11): done in the same click, unless the user
  // unticks it. Several to the trash still need 处理模式 (ADR 0016): then only the suggestion changes.
  const op = { archive: "archive", ignore: "trash" }[action];
  const canNow = op && (op !== "trash" || count <= 1 || canWrite());
  const now = canNow ? el("input", { type: "checkbox", id: "override-now", checked: true }) : null;
  const ok = await confirmDialog(count > 1 ? `将 ${count} 封改为「${label}」` : `改为「${label}」`, [
    now ? el("label", { class: "check" }, now, ` 现在就${label}`) : null,
    el("label", { class: "check" }, remember, ` 以后 ${who} 的邮件都这样处理（创建规则）`),
  ], "确定");
  if (!ok) return;
  try {
    const r = await api("/api/items/override", { source: S.source, ids, action, remember: remember.checked ? kind : null });
    if (now && now.checked) return act(ids, op);                // its own toast and undo; the change is in 操作记录
    toast(r.message, async () => { const u = await api("/api/undo", { source: S.source, log_id: r.log_id }); toast(u.message); loadInbox(); });
  } catch (err) { toast(err.message); }
  loadInbox();
}

async function draft(it) {
  const r = await api("/api/items/draft", { source: S.source, id: it.id });
  it.draft_text = r.text;
  return r.text;
}

async function saveDraft(it, text) {
  try {
    const r = await api("/api/items/draft/save", { source: S.source, id: it.id, text });
    toast(r.message, async () => { await api("/api/undo", { source: S.source, log_id: r.log_id }); toast("草稿已删除"); loadInbox(); });
  } catch (err) { toast(err.message); }
  loadInbox();
}

async function calendar(it) {
  try {
    const r = await api("/api/items/calendar", { source: S.source, id: it.id });
    if (r.google_url) window.open(r.google_url, "_blank", "noopener,noreferrer");
    else download(r.filename, r.ics);
    toast(r.google_url ? "已在 Google 日历中打开，保存即可。" : "已下载日历文件，打开即可添加到日历。");
    loadInbox();                       // other mail about the same appointment now says 已在日程中
  } catch (err) { toast(err.message); }
}

export function initInbox() {
  Object.assign(handlers, { act, override, draft, saveDraft, calendar, canWrite, needWrite });
  onActivity(followActivity);
  $("sync").addEventListener("click", startSync);
  $("sync-cancel").addEventListener("click", () => job && api("/api/jobs/cancel", { id: job.id }));
}
