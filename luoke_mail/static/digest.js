// 精选: the local model reads subscription mail, then chooses the few pieces worth the user's time: about 20 for
// the last seven days, about 10 for each earlier month. What the user cares about is inferred from their own things
// (kept in their memory, changed on the 记忆 page); 收藏 / 没兴趣 go back to the memory and steer later choices.
// 收藏 keeps a piece however old it gets; 归档 puts a read piece aside (found again under 已归档).
// Newsletter text is untrusted: every piece is rendered as text, links open only through the button.
import { icon } from "./icons.js";
import { $, S, api, el, nav, setStatus, shortDate, toast } from "./core.js";

const MONTHS_FIRST = 6;
let view = null;
let timer = null;
let allMonths = false;
let shelf = "picks";                       // 精选 | 收藏 (saved) | 已归档 (archived)

export function stopDigest() {
  clearTimeout(timer);
  timer = null;
}

export async function showDigest() {
  stopDigest();
  allMonths = false;
  shelf = "picks";
  try { view = await api(`/api/digest?source=${S.source}`); }
  catch (err) { setStatus("digest-status", err.message, "err"); return; }
  setStatus("digest-status", "");
  render();
  poll();
}

// while newsletters are read, picks chosen or topics inferred, keep the page current
const listShape = (v) => JSON.stringify([v.week.map((i) => i.id), v.week_pending,
  v.months.map((m) => [m.month, m.pending, m.items.length])]);

function busy(v) {
  return (v.reading.queued && (v.reading.on || v.reading.running)) || v.editing.running;
}

function poll() {
  stopDigest();
  if (!view || !busy(view)) return;
  timer = setTimeout(async () => {
    try {
      const v = await api(`/api/digest?source=${S.source}`);
      const changed = listShape(v) !== listShape(view);
      const taste = JSON.stringify([v.interests, v.dislikes, v.proposed, v.profile]) !==
        JSON.stringify([view.interests, view.dislikes, view.proposed, view.profile]);
      view = v;
      renderReading();
      if (taste) renderTaste();
      if (changed && shelf === "picks") renderList();
      renderTabs();
    } catch (e) { /* try again */ }
    poll();
  }, 5000);
}

function render() {
  renderTaste();
  renderReading();
  renderTabs();
  renderList();
  renderSources();
}

// ---- 关注的话题: inferred, shown, changed on the 记忆 page ------------------------------------------------------
function renderTaste() {
  const box = $("digest-taste");
  box.textContent = "";
  const p = view.profile;
  const chips = (notes, cls) => notes.map((n) => el("span", { class: `chip ${cls}` }, n.text));
  const manage = el("button", { type: "button", class: "link", onclick: () => nav.go("memory") },
    view.interests.length || view.dislikes.length ? "在「记忆」中修改" : "在「记忆」中添加");
  if (view.interests.length || view.dislikes.length) {
    const all = chips(view.interests, "soft"), FEW = 8;
    const row = el("div", { class: "taste-row" }, el("span", { class: "taste-label" }, "关注的话题"),
      ...(all.length ? all.slice(0, FEW) : [el("span", { class: "muted" }, "暂无")]));
    if (all.length > FEW) row.append(el("button", { type: "button", class: "link", onclick: (e) => { e.currentTarget.replaceWith(...all.slice(FEW)); } },
      `全部 ${all.length} 个`));
    box.append(row);
    if (view.dislikes.length) box.append(el("div", { class: "taste-row" }, el("span", { class: "taste-label" }, "不感兴趣"), ...chips(view.dislikes, "soft")));
  }
  const note = view.interests.length ? "按你的阅读和反馈归纳。" : "暂时按热度挑选，读得多了会按你的口味挑。";
  const siteHint = !p.sites ? el("button", { type: "button", class: "link", onclick: () => nav.go("memory") }, "添加你的网站或博客") : null;
  const some = view.interests.length || view.dislikes.length;
  box.append(el("p", { class: "hint taste-note" }, `${note} `, some ? manage : siteHint || manage));
  view.proposed.forEach((n) => box.append(el("div", { class: "proposal" },
    el("span", null, n.kind === "interest" ? "你可能关注：" : "你可能不感兴趣：", el("strong", null, n.text),
      n.why ? el("span", { class: "muted" }, `（${n.why}）`) : null),
    el("span", { class: "card-actions" },
      el("button", { type: "button", class: "btn small primary", onclick: () => decide(n.id, "confirm") }, "确认"),
      el("button", { type: "button", class: "btn small", onclick: () => decide(n.id, "reject") }, "不对")))));
}

async function decide(id, op) {
  try { await api("/api/memory", { source: S.source, op, id }); }
  catch (err) { toast(err.message); }
  await showDigest();
  nav.refresh();
}

// ---- reading progress ---------------------------------------------------------------------------------
// rebuilt only when its shape changes; between polls only the numbers move, so a click on 暂停 is never lost
function renderReading() {
  const bar = $("digest-reading");
  const r = view.reading;
  const problem = r.running ? "" : r.problem || "";          // an old problem is not shown while it reads again
  const shape = JSON.stringify([S.source, !r.queued, r.on, r.running, problem]);
  if (bar.dataset.shape !== shape) {
    bar.dataset.shape = shape;
    bar.textContent = "";
    bar.hidden = !r.queued;
    if (bar.hidden) return;
    bar.append(el("div", { class: "judge-progress" },
      el("span", null, el("strong", { "data-role": "count" }),
        el("span", { class: "muted" }, !r.on ? (r.running ? "，读完当前这封后暂停" : "，已暂停")
          : problem ? "" : r.running ? "，可以离开此页面" : "，等待其他任务完成")),
      el("div", { class: "bar" }, el("span", { "data-role": "pct" })),
      problem ? el("p", { class: "warn-text" }, `${problem}恢复后将自动继续。`) : null),
    el("span", { class: "judge-acts" }, el("button", { type: "button", class: "btn small ghost",
      onclick: () => setReading(!view.reading.on) }, r.on ? "暂停" : "继续")));
  }
  if (bar.hidden) return;
  const done = r.read + r.failed, total = r.queued + done;
  bar.querySelector("[data-role=count]").textContent = `${r.on ? "正在阅读" : "已读"} ${done} / ${total} 封`;
  bar.querySelector("[data-role=pct]").style.width = `${Math.round((done / total) * 100)}%`;
}

async function setReading(on) {
  try { view = await api("/api/digest/reading", { source: S.source, on }); }
  catch (err) { toast(err.message); return; }
  renderReading();
  poll();
}

// ---- 精选 / 收藏 / 已归档 -----------------------------------------------------------------------------------
// the tabs show up once something was saved or put aside
function renderTabs() {
  const box = $("digest-tabs");
  box.textContent = "";
  box.hidden = shelf === "picks" && !view.saved.length && !view.archived.length;
  if (box.hidden) return;
  for (const [key, label] of [["picks", "精选"], ["saved", `收藏（${view.saved.length}）`], ["archived", `已归档（${view.archived.length}）`]]) {
    box.append(el("button", { type: "button", role: "tab", "aria-selected": String(shelf === key),
      onclick: () => { shelf = key; renderTabs(); renderList(); } }, label));
  }
}

// counts in the tabs follow what was just done, without redrawing the list (the card shows its own 撤销)
async function recount() {
  try {
    const v = await api(`/api/digest?source=${S.source}`);
    Object.assign(view, { saved: v.saved, archived: v.archived });
  } catch (e) { return; }
  renderTabs();
}

function renderList() {
  const box = $("digest-list");
  box.textContent = "";
  if (shelf === "saved") {
    box.append(el("p", { class: "hint shelf-hint" }, "收藏的内容会一直保留在这里，不随时间移出。"));
    if (!view.saved.length) box.append(el("p", { class: "empty-state" }, "还没有收藏的内容。"));
    if (view.saved.length) box.append(el("div", { class: "list" }, ...view.saved.map((it) => card(it, "saved"))));
    return;
  }
  if (shelf === "archived") {
    box.append(el("p", { class: "hint shelf-hint" }, "已经看过、移出列表的内容。"));
    if (!view.archived.length) box.append(el("p", { class: "empty-state" }, "没有已归档的内容。"));
    if (view.archived.length) box.append(el("div", { class: "list" }, ...view.archived.map((it) => card(it, "archived"))));
    return;
  }
  if (!view.sources.length && !view.week.length && !view.months.length) {
    box.append(el("div", { class: "empty-state" },
      el("p", null, "还没有精选来源。"),
      el("p", null, "在「订阅体检」里把发件人加入精选，或在「清理」的「订阅内容」一栏选择「先读一遍」。")));
    return;
  }
  const waitNote = (pending) => (!pending ? null : el("p", { class: "hint" },
    view.editing.running ? "正在挑选，暂按评分显示。" : "尚未挑选，暂按评分显示。"));
  box.append(el("h3", { class: "subhead section-title" }, "最近 7 天", view.week.length ? el("span", { class: "count" }, String(view.week.length)) : null));
  if (view.week.length) box.append(...[waitNote(view.week_pending)].filter(Boolean));
  else box.append(el("p", { class: "empty-state" }, view.week_total ? "最近 7 天没有值得一看的内容。" : "最近 7 天没有新的订阅邮件。"));
  if (view.week.length) box.append(el("div", { class: "list" }, ...view.week.map((it) => card(it, "picks"))));
  const months = view.months.filter((m) => m.items.length);
  if (!months.length) return;
  box.append(el("h3", { class: "subhead section-title" }, "以往精选"),
    el("p", { class: "hint" }, "更早的订阅邮件中，每个月最值得一读的内容。"));
  const shown = allMonths ? months : months.slice(0, MONTHS_FIRST);
  shown.forEach((m) => {
    box.append(el("h4", { class: "month-title" }, `${m.month.slice(0, 4)} 年 ${Number(m.month.slice(5))} 月 `,
      el("span", { class: "muted" }, String(m.items.length))));
    const w = waitNote(m.pending);
    if (w) box.append(w);
    box.append(el("div", { class: "list" }, ...m.items.map((it) => card(it, "picks"))));
  });
  if (shown.length < months.length) {
    box.append(el("button", { type: "button", class: "btn small", onclick: () => { allMonths = true; renderList(); } },
      `显示更早的 ${months.length - shown.length} 个月`));
  }
}

// a card on one of the shelves; what takes it off this shelf leaves a line with 撤销 in its place
function card(it, where) {
  const c = el("article", { class: "pick" });
  const gone = () => (where === "picks" ? it.feedback === "dismissed" || it.archived
    : where === "saved" ? it.feedback !== "useful" : !it.archived);
  const fill = () => {
    c.textContent = "";
    c.className = "pick" + (gone() ? " dismissed" : "");
    if (gone()) {
      const [text, back] = it.feedback === "dismissed" ? ["已标记没兴趣", () => mark(null)]
        : where === "picks" ? ["已归档", () => archive(false)]
          : where === "saved" ? ["已取消收藏", () => mark("useful")] : ["已移回精选", () => archive(true)];
      c.append(el("span", { class: "muted" }, `${text}：${it.title}`),
        el("button", { type: "button", class: "link", onclick: back }, "撤销"));
      return;
    }
    // the sample's row: a book, where it came from, the title, what it says (luoke-brand ui/src/mail Digest)
    c.append(el("span", { class: "pick-ico" }, icon("book-open")),
      el("div", { class: "meta" }, [it.source, it.date ? shortDate(it.date.slice(0, 10)) : null, it.also ? `另有 ${it.also} 处提到` : null]
        .filter(Boolean).join(" · ")),
      el("div", { class: "pick-title" }, el("h4", null, it.title),
        it.score >= 3 ? el("span", { class: "chip brand" }, "推荐") : null,
        it.feedback === "useful" && where !== "saved" ? el("span", { class: "chip soft" }, "已收藏") : null));
    if (it.summary) c.append(el("p", { class: "summary" }, it.summary));
    if (it.pick_why || it.why) c.append(el("p", { class: "reason" }, it.pick_why || it.why));
    const row = el("div", { class: "card-actions" });
    if (it.url) row.append(el("button", { type: "button", class: "btn tiny", title: it.url, onclick: () => open(it.url) }, icon("external-link", 14), `查看原文（${it.host}）`));
    if (it.mail_url) row.append(el("button", { type: "button", class: "btn tiny ghost", onclick: () => open(it.mail_url) }, "在 Gmail 中打开 ↗"));
    const saved = it.feedback === "useful";
    row.append(el("button", { type: "button", class: "btn tiny ghost", "aria-pressed": String(saved),
      onclick: () => mark(saved ? null : "useful") }, icon("bookmark", 14), saved ? "取消收藏" : "收藏"));
    if (where === "archived") row.append(el("button", { type: "button", class: "btn tiny ghost", onclick: () => archive(false) }, "移回精选"));
    if (where === "picks") {
      row.append(el("button", { type: "button", class: "btn tiny ghost", onclick: () => archive(true) }, icon("archive", 14), "归档"),
        el("button", { type: "button", class: "btn tiny ghost", onclick: () => mark("dismissed") }, "没兴趣"));
    }
    c.append(row);
  };
  const open = (url) => {
    window.open(url, "_blank", "noopener,noreferrer");
    api("/api/digest/open", { source: S.source, id: it.id }).catch(() => {});
  };
  const mark = async (value) => {
    try { Object.assign(it, await api("/api/digest/feedback", { source: S.source, id: it.id, value })); }
    catch (err) { toast(err.message); return; }
    fill();
    recount();
  };
  const archive = async (on) => {
    try { Object.assign(it, await api("/api/digest/archive", { source: S.source, id: it.id, on })); }
    catch (err) { toast(err.message); return; }
    fill();
    recount();
  };
  fill();
  return c;
}

// ---- 精选来源 -------------------------------------------------------------------------------------------
function renderSources() {
  const box = $("digest-sources");
  box.textContent = "";
  $("digest-sources-title").textContent = `精选来源（${view.sources.length}）`;
  $("digest-sources-box").hidden = !view.sources.length;
  view.sources.forEach((s) => box.append(el("div", { class: "rule-row source-row" },
    el("span", null, s.name || s.key), el("span", { class: "muted mono" }, s.key.replace(/^(list|from):/, "")),
    el("button", { type: "button", class: "link", onclick: async () => {
      try { view = await api("/api/digest/source", { source: S.source, key: s.key, on: false }); }
      catch (err) { toast(err.message); return; }
      toast(`已将「${s.name || s.key}」移出精选来源`);
      render();
    } }, "移出"))));
}
