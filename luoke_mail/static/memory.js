// 记忆: what luoke remembers about the user (luoke-mem, shared by luoke tools). Every entry says where it came
// from and can be opened to its evidence; guesses wait for a yes or no; anything can be changed or forgotten.
import { $, S, api, confirmDialog, el, nav, toast, withBusy } from "./core.js";
import { icon } from "./icons.js";

// a section head with its count, and its rows in one list group (luoke-brand DESIGN.md 6)
const head = (title, n, extra) => el("h3", { class: "subhead section-title" }, extra || null, title, n != null ? el("span", { class: "count" }, String(n)) : null);
const group = (rows) => el("div", { class: "list" }, ...rows);

const SECTIONS = [["fact", "关于你", "例如：我是产品经理，负责 B 端客户"], ["preference", "处理习惯", "例如：银行通知看完就归档"],
  ["interest", "关注的话题", "例如：本地大模型"], ["dislike", "不感兴趣", "例如：购物促销"],
  ["taste", "精选的口味", null], ["term", "常用说法", null]];       // worked out by the memory; shown when there are any
let timer = null;

export function stopMemory() {
  clearTimeout(timer);
  timer = null;
}
const KIND = { fact: "关于你", interest: "关注的话题", dislike: "不感兴趣", preference: "处理习惯", taste: "精选的口味", term: "常用说法" };

let view = null;

export async function showMemory() {
  stopMemory();
  try { view = await api(`/api/memory?source=${S.source}`); }
  catch (err) { $("memory").textContent = ""; $("memory").append(el("p", { class: "status err" }, err.message)); return; }
  render();
  poll();
}

// while learning runs or topics are inferred, keep the progress current and show what was found
function poll() {
  stopMemory();
  const l = view && view.learning;
  if (!l || !(l.running || (l.on && l.stage) || view.profile.running)) return;
  timer = setTimeout(async () => {
    try {
      const v = await api(`/api/memory?source=${S.source}`);
      const shape = (x) => JSON.stringify([x.asks.length, x.fact.length, x.preference.length, x.interest.length,
        x.dislike.length, x.profile.running, x.sites]);
      const grew = shape(v) !== shape(view);
      view = v;
      if (grew) render(); else renderLearning();
    } catch (e) { /* try again */ }
    poll();
  }, 5000);
}

// ---- 从邮件中了解你 -------------------------------------------------------------------------------------
const STAGE = { sent_check: "正在查找你发出的邮件", sent: "正在阅读你发出的邮件", received: "正在了解你收到的往来邮件" };

// rebuilt only when its shape changes; between polls only the numbers move, so a click on 暂停 is never lost
function renderLearning() {
  const box = $("memory-learning");
  if (!box) return;
  const l = view.learning;
  // running: what it is doing; switched on with work left but not running: waiting its turn (or retrying after
  // a problem); an old problem is not shown while it runs again
  // mid-way through the sent mail and not running: another task holds the model. Any other next step (the next
  // look for new sent mail, the mail received) waits for a quiet moment -- not for other work (luoke-mail#14)
  const midway = l.stage === "sent" && l.sent_done < l.sent_total;
  const waiting = l.on && midway && !l.running;
  const between = l.on && !!l.stage && !l.running && !midway;
  const problem = l.running ? "" : l.problem || "";
  const shape = JSON.stringify([S.source, l.enabled, l.on, l.running, problem, !!l.stage, midway]);
  if (box.dataset.shape !== shape) {
    box.dataset.shape = shape;
    box.textContent = "";
    if (!l.enabled) {
      box.append(el("div", { class: "learn-intro" }, el("strong", null, "从邮件中了解你"),
        el("p", { class: "hint" }, "不用自己填写：根据你发出的邮件、收到的往来邮件和你的处理习惯，记下关于你的信息。")),
      el("button", { type: "button", class: "btn primary", onclick: () => setLearning(true) }, "开启"));
      return;
    }
    box.append(el("div", { class: "judge-progress" },
      el("span", null, el("strong", { "data-role": "status" }),
        el("span", { class: "muted" }, !l.on ? "，已暂停" : problem ? "" : l.running ? "，可以离开此页面" : waiting ? "，等待其他任务完成"
        : between ? "，下次空闲时继续" : "")),
      l.on && (l.running || midway) && l.sent_total ? el("div", { class: "bar" }, el("span", { "data-role": "pct" })) : null,
      problem ? el("p", { class: "warn-text" }, `${problem}恢复后将自动继续。`) : null),
    el("span", { class: "judge-acts" },
      el("button", { type: "button", class: "btn small ghost", onclick: () => setLearning(!view.learning.on) }, l.on ? "暂停" : "继续")));
  }
  if (!l.enabled) return;
  const status = box.querySelector("[data-role=status]");
  const sent = l.stage === "sent" ? `（${l.sent_done} / ${l.sent_total} 封）` : "";
  status.textContent = l.running ? `${STAGE[l.stage] || "正在了解你"}${sent}`
    : (l.stage === "sent" && l.sent_done < l.sent_total) ? `了解你：已读你发出的邮件 ${l.sent_done} / ${l.sent_total} 封`
      : l.sent_done ? `已读完你发出的 ${l.sent_done} 封邮件` : "已开启";
  const pct = box.querySelector("[data-role=pct]");
  if (pct && l.sent_total) pct.style.width = `${Math.round((l.sent_done / l.sent_total) * 100)}%`;
}

async function setLearning(on) {
  try { await api("/api/memory/learn", { source: S.source, on }); }
  catch (err) { toast(err.message); return; }
  await showMemory();
}

// true when done; a note from the server (e.g. where something sensitive went) replaces the caller's toast
async function change(body) {
  try { view = await api("/api/memory", { source: S.source, ...body }); }
  catch (err) { toast(err.message); return false; }
  render();
  nav.refresh();
  if (view.message) { toast(view.message); return false; }
  return true;
}

function render() {
  const box = $("memory");
  box.textContent = "";
  box.append(el("div", { class: "judge-bar", id: "memory-learning" }));
  renderLearning();
  if (view.asks.length) box.append(head("想问你的", view.asks.length, el("span", { class: "brand-ico" }, icon("lightbulb"))), group(view.asks.map(askRow)));
  for (const [kind, title, example] of SECTIONS) {
    if (!example && !(view[kind] || []).length) continue;
    box.append(head(title, view[kind].length));
    if (kind === "interest") box.append(el("p", { class: "hint shelf-hint" }, "根据你的网站、邮箱标签、发起的邮件、订阅的阅读情况和精选里的反馈归纳。不对的可以点「不对」。"));
    if (!view[kind].length) box.append(el("p", { class: "empty-state small-empty" }, "暂无"));
    else box.append(group(view[kind].map((n) => entry(n, false))));
    if (!example) continue;
    const input = el("input", { placeholder: example, maxlength: "200", "aria-label": `添加${title}` });
    box.append(el("form", { class: "form inline taste-add", onsubmit: async (e) => {
      e.preventDefault();
      if (input.value.trim() && await change({ op: "add", kind, text: input.value })) toast("已添加");
    } }, input, el("button", { type: "submit", class: "btn small" }, "添加")));
    if (kind === "fact") renderSites(box);
  }
  box.append(el("div", { class: "memory-foot" },
    el("p", { class: "hint" }, "清空全部记忆、删除其他螺壳工具记下的内容，在螺壳记忆中进行。"),
    el("span", { class: "card-actions" },
      view.whole ? el("button", { type: "button", class: "btn small", onclick: openWhole }, "查看整份记忆 ↗") : null,
      el("button", { type: "button", class: "btn small", onclick: forgetMine },
        S.source === "demo" ? "清除示例记忆" : "忘掉邮件助手记下的内容"))));
}

// ---- the user's own websites: read to infer 关注的话题 -------------------------------------------------------
function renderSites(box) {
  box.append(head("你的网站和博客", view.sites.length),
    el("p", { class: "hint shelf-hint" }, "螺壳会读取这些网站上的文章，用来了解你关注的话题。只读取你填写的网址。"));
  if (!view.sites.length) box.append(el("p", { class: "empty-state small-empty" }, "暂无"));
  const sites = el("div", { class: "list" });
  if (view.sites.length) box.append(sites);
  view.sites.forEach((s) => sites.append(el("div", { class: "mem-row site-row" },
    el("div", { class: "mem-text" }, s.title || s.url),
    el("div", { class: "mem-meta muted" }, [s.title ? s.url : null, s.problem ? "最近一次无法打开" : `${s.posts} 篇文章`].filter(Boolean).join(" · ")),
    el("div", { class: "card-actions" }, el("button", { type: "button", class: "btn small ghost", onclick: async () => {
      const ok = await confirmDialog("删除网站", [el("p", null, `将不再读取 ${s.url}。已经推测出的话题不受影响，可以在下面单独修改。`)], "删除");
      if (ok) await siteChange({ op: "remove", id: s.id }, "已删除");
    } }, "删除")))));
  const input = el("input", { placeholder: "例如：https://example.com", maxlength: "300", inputmode: "url", "aria-label": "网站或博客的网址" });
  const button = el("button", { type: "submit", class: "btn small" }, "添加");
  box.append(el("form", { class: "form inline taste-add", onsubmit: async (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    await withBusy(button, "正在读取…", () => siteChange({ op: "add", url: input.value }));
  } }, input, button));
}

async function siteChange(body, done) {
  let r;
  try { r = await api("/api/memory/site", { source: S.source, ...body }); }
  catch (err) { toast(err.message); return; }
  view = r;
  render();
  poll();
  toast(r.message || done);
}

// a question about something remembered, or a suggestion (以后「X」的邮件都自动归档？): the memory decides what is
// worth asking; the answer goes back to it as it is
function askRow(a) {
  const row = el("div", { class: "mem-row" });
  row.append(el("div", { class: "mem-text" }, el("span", { class: "chip soft" }, a.kind === "suggestion" ? "建议" : KIND[a.about_kind] || "问题"), ` ${a.text}`),
    a.why ? el("div", { class: "mem-meta muted" }, a.why) : null,
    el("div", { class: "card-actions" }, ...a.options.map((o, i) => el("button", {
      type: "button", class: i === 0 ? "btn small primary" : i === 1 ? "btn small" : "btn small ghost",
      onclick: (e) => withBusy(e.target, "正在保存…", () => change({ op: "reply", id: a.id, choice: o.choice })),
    }, o.label))));
  return row;
}

function entry(n, proposed) {
  const row = el("div", { class: "mem-row" });
  const fill = () => {
    row.textContent = "";
    row.append(el("div", { class: "mem-text" }, proposed ? el("span", { class: "chip soft" }, KIND[n.kind]) : null, ` ${n.text}`),
      el("div", { class: "mem-meta muted" }, [n.origin_text, n.why].filter(Boolean).join(" · ")));
    // learnt from mail: 「不对」 keeps it out for good; deleting would let it be learnt again
    // the evidence: the mail assistant's own records in full; another tool's only as where from and how many
    const others = n.others || [];
    const count = n.cites.length + others.reduce((k, o) => k + o.count, 0);
    if (count) {
      row.append(el("details", { class: "mem-cites" }, el("summary", null, `依据（${count}）`),
        el("ul", null, ...n.cites.map((c) => el("li", null, el("span", { class: "mono muted" }, `${(c.happened_at || "").slice(0, 10)} `), c.text)),
          ...others.map((o) => el("li", { class: "muted" }, `${o.source}记下的 ${o.count} 条`)))));
    }
    const acts = el("div", { class: "card-actions" });
    if (proposed) {
      acts.append(el("button", { type: "button", class: "btn small primary", onclick: () => change({ op: "confirm", id: n.id }) }, "确认"),
        el("button", { type: "button", class: "btn small", onclick: () => change({ op: "reject", id: n.id }) }, "不对"));
    } else if (n.learnt) {
      acts.append(el("button", { type: "button", class: "btn small", onclick: edit }, "修改"),
        el("button", { type: "button", class: "btn small ghost", onclick: () => change({ op: "reject", id: n.id }) }, "不对"));
    } else {
      acts.append(el("button", { type: "button", class: "btn small", onclick: edit }, "修改"),
        el("button", { type: "button", class: "btn small ghost", onclick: forget }, "删除"));
    }
    row.append(acts);
  };
  const edit = () => {
    const input = el("input", { value: n.text, maxlength: "200", "aria-label": "修改内容" });
    row.textContent = "";
    row.append(el("form", { class: "form inline taste-add", onsubmit: async (e) => {
      e.preventDefault();
      if (input.value.trim() && await change({ op: "revise", id: n.id, text: input.value })) toast("已修改");
    } }, input, el("button", { type: "submit", class: "btn small primary" }, "保存"),
    el("button", { type: "button", class: "btn small ghost", onclick: fill }, "取消")));
    input.focus();
  };
  const forget = async () => {
    const ok = await confirmDialog("删除这条记忆", [el("p", null, `「${n.text}」将被删除，它的依据记录也会一并删除。`)], "删除");
    if (ok && await change({ op: "forget", id: n.id })) toast("已删除");
  };
  fill();
  return row;
}

// luoke-mem's own page for the whole memory: a tab is opened at once (a tab opened after waiting may be blocked),
// then sent to the page once it is up
async function openWhole(e) {
  const tab = window.open("", "_blank");
  if (tab) tab.opener = null;
  await withBusy(e.target, "正在打开…", async () => {
    try {
      const r = await api("/api/memory/whole", { source: S.source });
      if (r.opened) { if (tab) tab.close(); }                 // 螺壳记忆 opened its own page
      else if (tab) tab.location = r.url; else window.open(r.url, "_blank", "noopener,noreferrer");
    } catch (err) {
      if (tab) tab.close();
      toast(err.message);
    }
  });
}

// the demo's memory is the mail assistant's own; the real one is the user's, shared by every luoke tool -- here
// only what the mail assistant wrote is forgotten (the whole memory is managed in luoke-mem, top-level ADR 0002)
async function forgetMine() {
  const demo = S.source === "demo";
  const ok = await confirmDialog(demo ? "清除示例记忆" : "忘掉邮件助手记下的内容", demo
    ? [el("p", null, "将清除示例中的全部记忆。")]
    : [el("p", null, "删除邮件助手记下和推测的内容：关于你、处理习惯、关注的话题、不感兴趣的，以及它们的依据。"),
      el("p", { class: "hint" }, "你确认过、改过或自己写的会保留。邮件、精选和其他螺壳工具的记忆不受影响。删除后无法撤销。")], demo ? "清除" : "删除");
  if (ok && await change({ op: "forget_mine" })) toast(demo ? "已清除" : "已删除");
}
