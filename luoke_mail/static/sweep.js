// 清理: old mail, judged message by message by the local model. Two scopes, one page:
// - inbox:   inbox mail older than the sorting window -> spent mail to the trash, the rest archived, what may
//            still be open (an unpaid bill, an unanswered question) left for the user to look at;
// - archive: archived mail -> spent mail to the trash, the rest kept.
// Headers only; the model runs in the background at the lowest priority; ticked groups are carried out in
// checked batches; one undo puts everything back (with its labels).
import { $, S, api, confirmDialog, currentMailbox, el, nav, setStatus, toast, waitJob, withBusy } from "./core.js";
import { human } from "./subs.js";
import { loadInbox } from "./inbox.js";
import { NEEDS_MODE, needWrite } from "./settings.js";

let scope = "inbox";
let view = null;
let timer = null;
let shownJudged = 0;                       // verdicts the list was last drawn with
// each showing of a tab (a scope, a mailbox) is one generation; an answer that comes back after the page moved on
// -- to another tab, another mailbox, or the same tab shown again -- is dropped, so it never draws over what is
// newer (issue #4: a slow first read put "正在读取" back over a later error)
let gen = 0;
const current = (g, src, sc) => g === gen && src === S.source && sc === scope;
const on = {}, off = {};                   // groups the user ticked / unticked, per scope and inbox
const opened = {};                         // blocks the user opened or closed: redrawing the list keeps them so
const mine = (sets) => (sets[`${scope}:${S.source}`] ||= new Set());
const canWrite = () => S.source === "demo" || !!S.state.mode.write;
const canApply = (c) => canWrite() || !(scope === "archive" ? c.n : c.trash);    // archiving needs only the click
const gmailLike = () => S.source === "demo" || (currentMailbox() || {}).provider === "gmail";
const allMail = () => (gmailLike() ? "「所有邮件」" : "归档文件夹");

const SECTIONS = {
  inbox: [["del", "建议移到垃圾箱"], ["todo", "待确认"], ["arch", "建议归档"], ["pending", "未分析"]],
  archive: [["del", "建议移到垃圾箱"], ["keep", "建议保留"], ["pending", "未分析"]],
};
const sectionOf = (g) => (g.kind === "pending" ? "pending" : g.action === "trash" && g.suggested ? "del"
  : g.kind === "todo" ? "todo" : scope === "inbox" ? "arch" : "keep");

// the bar on the inbox page
export function renderOldBar(old, open) {
  const bar = $("old-bar");
  bar.textContent = "";
  bar.hidden = !(old && old.count);
  if (bar.hidden) return;
  // judged by itself in idle time (luoke-mail#33): the line says what is suggested, or how far it has got
  const left = old.total !== undefined ? old.total - old.judged : null;
  let text = `收件箱中有 ${old.count} 封超过 ${old.days} 天的邮件`;
  if (left === 0 && old.judged) {
    const parts = [old.archive && `建议归档 ${old.archive} 封`, old.trash && `移到垃圾箱 ${old.trash} 封`,
      old.todo && `待你确认 ${old.todo} 封`].filter(Boolean);
    text = `超过 ${old.days} 天的 ${old.judged} 封已分析` + (parts.length ? `：${parts.join("、")}` : "");
  } else if (left && old.judging) {
    text += `，正在分析（${old.judged} / ${old.total}）`;
  }
  bar.append(el("div", null, text), el("button", { type: "button", class: "btn", onclick: open }, left === 0 && old.judged ? "查看" : "清理"));
}

export function stopSweep() {
  clearTimeout(timer);
  timer = null;
  gen += 1;                                // whatever is still on its way belongs to what was shown before
}

export async function showSweep(which) {
  stopSweep();
  scope = which;
  const box = $(`sw-${scope}`);
  box.querySelector(".lede").textContent = scope === "inbox"
    ? `收件箱里超过 ${S.state.fetch.days} 天的邮件。加了星标或「收藏」「重要」这类标签的不会动。`
    : (gmailLike() ? "已归档的邮件" : "归档文件夹里的邮件") + "。加了星标或「收藏」「重要」这类标签的不会动。";   // triage.held
  const g = gen, src = S.source, sc = scope;
  try {
    const v = await api(`/api/old?source=${src}&scope=${sc}`);
    if (!current(g, src, sc)) return;
    view = v;
    if (!view.scanned || view.stale) await scan();      // stale: the inbox has counted other old mail since
    else { render(); poll(); }
  } catch (err) { if (current(g, src, sc)) setStatus(`sw-${sc}-status`, err.message, "err"); }
}

const part = (id) => $(`sw-${scope}`).querySelector(id);

async function scan() {
  stopSweep();
  const g = gen, src = S.source, sc = scope;
  const status = `sw-${scope}-status`;
  const before = view && view.scanned ? view : null;
  setStatus(status, "正在读取…", "busy");
  part(".old").textContent = "";
  part(".judge-bar").hidden = part(".old-actions").hidden = true;
  try {
    const job = await api("/api/old/scan", { source: src, scope: sc });
    const v = await waitJob(job.id, (j) => {
      if (current(g, src, sc)) setStatus(status, `${j.stage.replace(/…$/, "")}${j.total ? `（${j.done}/${j.total}）` : ""}`, "busy");
    });
    if (!current(g, src, sc)) return;
    view = v;
  } catch (err) {
    if (!current(g, src, sc)) return;
    // not read this time: what was there stays, and reading again is one click away (issue #4)
    if (before) { view = before; render(); poll(); }
    setStatus(status, err.message, "err");
    part(".old").prepend(el("p", null, el("button", { class: "btn", onclick: () => scan() }, "重新读取")));
    return;
  }
  render();
  poll();
}

// while the model works, keep the progress current; redraw the list only when asked or when it is done
const judging = () => (view.judging.on && view.judged < view.total) || view.judging.running;
const reading = () => view.groups.some((g) => g.digest && g.digest.queued);
function poll() {
  clearTimeout(timer);
  timer = null;
  if (!view || !view.scanned || !(judging() || reading())) return;
  const g = gen, src = S.source, sc = scope;
  timer = setTimeout(async () => {
    try {
      const v = await api(`/api/old?source=${src}&scope=${sc}`);
      if (!current(g, src, sc)) return;
      const was = judging(), wasReading = reading();
      view = v;
      if ((was && !judging()) || (wasReading && !reading())) render(); else { renderJudge(); renderReading(); }
    } catch (e) { /* try again */ }
    if (current(g, src, sc)) poll();
  }, 4000);
}

function picked(g) {
  if (mine(on).has(g.key)) return true;
  if (mine(off).has(g.key)) return false;
  return g.suggested && !view.keep.includes(g.key);          // suggested, unless kept at an earlier clean-up
}
function pick(g, yes) {
  mine(yes ? on : off).add(g.key);
  mine(yes ? off : on).delete(g.key);
}
const count = (rows) => rows.reduce((a, g) => a + g.count, 0);
const size = (rows) => rows.reduce((a, g) => a + g.size, 0);
const kindLabel = (k) => (k === "pending" ? "未分析" : k === "personal" ? "和人的往来" : view.kinds[k] || "其他");
const shownKind = (g) => g.shown || g.kind;        // mail with a person judged a notice is listed under 和人的往来

function render() {
  shownJudged = view.judged;
  const box = part(".old");
  const status = `sw-${scope}-status`;
  box.textContent = "";
  const held = view.flagged ? `加了星标或「收藏」「重要」这类标签的 ${view.flagged} 封不在此列。` : "";
  const fresh = el("p", { class: "hint" }, `更新于 ${view.at} · `, el("button", { type: "button", class: "link", onclick: scan }, "刷新"));
  if (!view.total) {
    setStatus(status, "没有需要清理的邮件。" + held, "ok");
    box.append(fresh);                                 // a scan from days ago: one click reads it again
    part(".judge-bar").hidden = part(".old-actions").hidden = true;
    return;
  }
  setStatus(status, `共 ${view.total} 封，约 ${human(view.size)}` + (view.judged ? `，已分析 ${view.judged} 封` : "") + "。" + held, "ok");
  box.append(fresh);
  renderJudge();
  for (const [id, title] of SECTIONS[scope]) {
    const rows = view.groups.filter((g) => sectionOf(g) === id);
    if (!rows.length) continue;
    box.append(el("h3", { class: "subhead section-title" }, `${title}（${count(rows)} 封，约 ${human(size(rows))}）`));
    if (id === "todo") box.append(el("p", { class: "hint" }, "可能尚未处理完毕，例如未付的账单、未回复的问题。默认不勾选，勾选后将归档。"));
    [...new Set(rows.map(shownKind))].forEach((k) => box.append(kindBlock(k, rows.filter((g) => shownKind(g) === k), id === "del" || id === "todo", id)));
  }
  renderReading();
  renderActions();
}

function kindBlock(kind, rows, open, section) {
  const pending = kind === "pending";          // its section title already names it and gives the totals
  const label = pending ? "全选" : kindLabel(kind);
  const id = `${scope}:${S.source}:${section}:${kind}`;
  // ticking 全选 changes the boxes in place: no redraw, so no block opens or closes by itself
  const all = el("input", { type: "checkbox", "aria-label": pending ? "全选：未分析" : `全选：${label}`, onchange: () => {
    rows.forEach((g) => pick(g, all.checked));
    d.querySelectorAll(".old-row input[type=checkbox]").forEach((cb) => { cb.checked = all.checked; });
    all.indeterminate = false;
    renderActions();
  } });
  all.checked = rows.every(picked);
  all.indeterminate = !all.checked && rows.some(picked);
  const d = el("details", { class: "kind-block", open: id in opened ? opened[id] : open || rows.some(picked),
    ontoggle: () => { opened[id] = d.open; } },
    el("summary", null, el("label", { class: "check", onclick: (e) => e.stopPropagation() }, all),
      el("strong", null, ` ${label}`), el("span", { class: "muted" }, pending ? ` ${rows.length} 个来源`
        : ` ${count(rows)} 封 · ${rows.length} 个来源 · 约 ${human(size(rows))}`)));
  if (kind === "newsletter") d.append(el("div", { class: "kind-note", "data-kind": kind }));
  d.append(el("div", { class: "old-list" }, rows.map((g) => row(g, all, rows))));
  return d;
}

// 订阅精选 in 清理: before newsletters go to the trash, the model can read them and keep what is worth reading
function renderReading() {
  document.querySelectorAll(`#sw-${scope} .kind-note[data-kind]`).forEach((note) => {
    const rows = view.groups.filter((g) => g.kind === note.dataset.kind);
    const total = count(rows);
    const d = rows.reduce((a, g) => ({ queued: a.queued + (g.digest ? g.digest.queued : 0), read: a.read + (g.digest ? g.digest.read : 0),
      kept: a.kept + (g.digest ? g.digest.kept : 0) }), { queued: 0, read: 0, kept: 0 });
    note.textContent = "";
    const toDigest = el("button", { type: "button", class: "link", onclick: () => nav.go("digest") }, "查看精选");
    if (!d.queued && !d.read) {
      note.append(el("span", null, "移到垃圾箱之前，可以让本机模型先读一遍，把值得看的留下。"),
        el("button", { type: "button", class: "btn small", onclick: () => readFirst(rows) }, "先读一遍"));
    } else if (d.queued) {
      note.append(el("span", null, `${view.reading_on ? "正在阅读：" : "已暂停："}已读 ${d.read} / ${d.read + d.queued} 封${d.kept ? `，${d.kept} 封含精选内容` : ""}。`),
        toDigest);
    } else {
      note.append(el("span", null, d.kept ? `已阅读 ${d.read} 封，其中 ${d.kept} 封含精选内容，处理时将保留并加星标。` : `已阅读 ${d.read} 封，没有值得保留的内容。`),
        toDigest);
    }
  });
}

async function readFirst(rows) {
  const n = count(rows);
  const minutes = Math.max(1, Math.round((n * 15 * (1 + ((S.state.power && S.state.power.factor) || 0))) / 60));   // 功耗 slows it
  const ok = await confirmDialog("先读一遍", [
    el("p", null, `本机模型会读这 ${n} 封邮件，把值得看的放进「精选」。`),
    el("ul", null, [`阅读在后台进行，预计约 ${minutes >= 90 ? `${Math.round(minutes / 60)} 小时` : `${minutes} 分钟`}，可以离开此页面。`,
      "含精选内容的邮件在清理时会保留并加星标，其余照常处理。",
      "这些发件人之后的邮件会汇总到每周精选。"].map((x) => el("li", null, x))),
    S.state.memory && !S.state.memory.interest ? el("p", { class: "hint" }, "在「记忆」里加上你的网站或博客，挑得会更准。") : null,
  ], "开始阅读");
  if (!ok) return;
  try {
    view = await api("/api/digest/read", { source: S.source, scope, keys: rows.map((g) => g.key) });
    renderReading();
    poll();
  } catch (err) { toast(err.message); }
}

function row(g, all, rows) {
  const verb = g.action === "trash" ? "移到垃圾箱" : "归档";
  const cb = el("input", { type: "checkbox", "aria-label": `${verb}：${g.name || g.addr} 的 ${g.count} 封`, onchange: () => {
    pick(g, cb.checked);
    all.checked = rows.every(picked);
    all.indeterminate = !all.checked && rows.some(picked);
    renderActions();
  } });
  cb.checked = picked(g);
  // the user's own labels matter more than the address: they come first, the address is what gets cut off
  const tags = g.labels.map((l) => el("span", { class: "chip soft", title: "你的标签" }, l));
  const who = g.name && g.addr && g.name !== g.addr ? [g.name, ...tags, el("span", { class: "muted mono" }, ` ${g.addr}`)] : [g.name || g.addr, ...tags];
  return el("label", { class: "old-row" }, cb,
    el("span", { class: "old-who" }, ...who),
    el("span", { class: "old-subject", title: g.subjects.join("\n") }, g.subjects.join(" / ")),
    el("span", { class: "old-count" }, `${g.count} 封`, el("span", { class: "muted" }, g.unread ? ` · ${g.unread} 未读` : ` · ${human(g.size)}`),
      g.digest && g.digest.kept ? el("span", { class: "chip action", title: "处理时保留并加星标" }, `含精选 ${g.digest.kept}`) : null),
    el("span", { class: "old-date mono" }, (g.latest || "").slice(0, 10)));
}

// rebuilt only when its shape changes; between polls only the numbers move, so a click on 暂停 is never lost
function renderJudge() {
  const bar = part(".judge-bar");
  const j = { ...view.judging, problem: view.judging.running ? null : view.judging.problem };   // not while it runs again
  const left = view.total - view.judged;
  const fresh = view.judged - shownJudged;
  const shape = JSON.stringify([S.source, !left && !j.problem, j.on, j.running, j.problem || "", fresh > 0, !view.judged]);
  if (bar.dataset.shape !== shape || bar.hidden !== (!left && !j.problem)) {     // (scan() hides it directly)
    bar.dataset.shape = shape;
    bar.textContent = "";
    bar.hidden = !left && !j.problem;
    if (bar.hidden) return;
    if (!j.on) {
      // paused (or not started); a model call already under way still finishes
      const note = !view.judged ? "。分析在后台进行，可以离开此页面。" : j.running ? "。当前一批分析完后暂停。" : "。已暂停。";
      bar.append(el("span", null, el("strong", { "data-role": "count" }), el("span", { class: "muted" }, note)),
        el("button", { type: "button", class: "btn primary", onclick: () => setJudging(true) }, view.judged ? "继续分析" : "开始分析"));
    } else {
      bar.append(el("div", { class: "judge-progress" },
        el("span", null, el("strong", { "data-role": "count" }),
          el("span", { class: "muted" }, j.problem ? "" : j.running ? "，可以离开此页面" : "，等待其他任务完成")),
        el("div", { class: "bar" }, el("span", { "data-role": "pct" })),
        j.problem ? el("p", { class: "warn-text" }, `${j.problem}恢复后将自动继续。`) : null),
      el("span", { class: "judge-acts" },
        fresh > 0 ? el("button", { type: "button", class: "btn small", "data-role": "fresh", onclick: render }) : null,
        el("button", { type: "button", class: "btn small ghost", onclick: () => setJudging(false) }, "暂停")));
    }
  }
  if (bar.hidden) return;
  // switched on is not running: while it waits for other work it says how far it got, not that it is at it
  bar.querySelector("[data-role=count]").textContent = !j.on ? `${left} 封未分析`
    : j.running ? `正在分析 ${view.judged} / ${view.total}` : `已分析 ${view.judged} / ${view.total}`;
  const pct = bar.querySelector("[data-role=pct]");
  if (pct) pct.style.width = `${Math.round((view.judged / view.total) * 100)}%`;
  const more = bar.querySelector("[data-role=fresh]");
  if (more) more.textContent = `显示新结果（${fresh}）`;
}

async function setJudging(yes) {
  try { view = await api("/api/old/judge", { source: S.source, scope, on: yes }); }
  catch (err) { toast(err.message); return; }
  renderJudge();
  poll();
}

function chosen() {
  const rows = view.groups.filter(picked);
  const of = (a) => rows.filter((g) => g.action === a);
  const kept = of("trash").reduce((a, g) => a + (g.digest ? g.digest.kept : 0), 0);
  return { rows, n: count(rows), archive: count(of("archive")), trash: count(of("trash")) - kept, kept, trashBytes: size(of("trash")),
    labelled: of("trash").filter((g) => g.labels.length).reduce((a, g) => a + g.count, 0) };
}

function actionText(c) {
  if (scope === "archive") return c.n ? `移到垃圾箱（${c.n} 封）` : "移到垃圾箱";
  return c.n ? `处理（${c.n} 封）` : "处理";
}

function renderActions() {
  const bar = part(".old-actions");
  bar.textContent = "";
  bar.hidden = false;
  const c = chosen();
  const detail = scope === "inbox" && c.n ? `：归档 ${c.archive}，移到垃圾箱 ${c.trash}` : c.n ? `，约 ${human(c.trashBytes)}` : "";
  bar.append(el("span", null, el("strong", null, `已选 ${c.n} 封`), el("span", { class: "muted" }, detail),
      c.n && !canApply(c) ? el("span", { class: "hint", style: "display:block" }, NEEDS_MODE) : null),
    el("button", { type: "button", class: "btn primary", disabled: !c.n, onclick: async (e) => {
      const btn = e.currentTarget;
      if (!canApply(chosen()) && !(await needWrite())) return;
      apply({ currentTarget: btn });
    } }, actionText(c)));
}

async function apply(e) {
  const btn = e.currentTarget;
  const c = chosen();
  const byKind = {};
  c.rows.forEach((g) => { const k = `${g.action}|${shownKind(g)}`; byKind[k] = (byKind[k] || 0) + g.count; });
  const lines = Object.entries(byKind).map(([k, n]) => {
    const [action, kind] = k.split("|");
    return el("li", null, `${action === "trash" ? "移到垃圾箱" : "归档"}：${kindLabel(kind)} ${n} 封`);
  });
  // not judged yet: archived on the user's word alone, said apart (luoke-mail#32)
  const unjudged = c.rows.filter((g) => g.kind === "pending").reduce((n, g) => n + g.count, 0);
  const ok = await confirmDialog(scope === "inbox" ? "处理旧邮件" : "移到垃圾箱", [
    el("ul", null, lines),
    unjudged ? el("p", { class: "warn-text" }, `其中 ${unjudged} 封还没有分析，会直接归档。`) : null,
    c.archive ? el("p", null, `归档的邮件将标为已读并移到${allMail()}。`) : null,
    c.trash ? el("p", null, `移到垃圾箱的邮件约 ${human(c.trashBytes)}，${gmailLike() ? "30 天后由 Gmail 自动清空" : "由邮箱定期清空"}。`) : null,
    c.kept ? el("p", null, `其中 ${c.kept} 封含精选内容，将保留并加星标。`) : null,
    c.labelled ? el("p", { class: "hint" }, `其中 ${c.labelled} 封带有你的标签，撤销时会一并恢复。`) : null,
    el("p", { class: "hint" }, S.source === "demo" ? "示例数据，不会更改邮箱。" : "可在操作记录中撤销。"),
  ], actionText(c));
  if (!ok) return;
  await withBusy(btn, "正在处理…", async () => {
    try {
      const count = c.rows.reduce((n, g) => n + g.count, 0);                 // what the user confirmed, no more
      const job = await api("/api/old/apply", { source: S.source, scope, keys: c.rows.map((g) => g.key), count });
      // thousands take minutes: it stops between batches, what is done stays undoable (luoke-mail#32)
      const stop = el("button", { type: "button", class: "btn small ghost", onclick: () => {
        api("/api/jobs/cancel", { id: job.id }).catch(() => {});
        stop.disabled = true;
        stop.textContent = "正在停止…";
      } }, "停止");
      $(`sw-${scope}-status`).after(stop);
      let r;
      try { r = await waitJob(job.id, (j) => setStatus(`sw-${scope}-status`, `${j.stage}`, "busy")); }
      finally { stop.remove(); }
      toast(r.message, r.log_id ? async () => {
        const u = await api("/api/undo", { source: S.source, log_id: r.log_id });
        toast(u.message);
        showSweep(scope);
        loadInbox();
      } : null);
    } catch (err) { toast(err.message); }
  });
  c.rows.forEach((g) => mine(on).delete(g.key));
  loadInbox();
  showSweep(scope);
}
