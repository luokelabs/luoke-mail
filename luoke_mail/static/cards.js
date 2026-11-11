// Cards for single emails and for groups of the same bulk sender, with the actions each suggestion needs.
// inbox.js fills `handlers`; cards never talk to the API directly.
import { S, currentMailbox, el, shortDate, withBusy } from "./core.js";
import { icon } from "./icons.js";

export const ACTION_ORDER = ["reply", "schedule", "follow_up", "archive", "ignore"];
// wording comes from the server (triage.ACTIONS), so the page, rules and notifications say the same thing
export const actionLabel = (a) => S.state.actions[a] || "处理";      // never a code on the page
export const handlers = { act: null, override: null, draft: null, saveDraft: null, calendar: null, canWrite: () => false,
  needWrite: async () => false };

// the one-click mailbox operation that carries out each suggestion
export function primaryOp(it) {
  if (it.suspicious || it.junk) return { op: "spam", label: S.state.ops.spam };     // junk: the user's rule says so
  return { archive: { op: "archive", label: S.state.ops.archive }, ignore: { op: "trash", label: S.state.ops.trash } }[it.action] || null;
}

// a button that changes the mailbox; once clicked it says what it is doing. Only what cannot be taken back, done
// to many at once (`bulk`: to the trash or junk), waits for 处理模式 (ADR 0016)
function writeBtn(label, onclick, cls = "btn small", pending = "处理中…", bulk = false, ico = null) {
  const can = !bulk || handlers.canWrite();
  return el("button", { type: "button", class: cls, title: can ? null : "删除前需要先开启处理模式，点击时会说明",
    onclick: async (e) => {
      const btn = e.currentTarget;
      if (bulk && !handlers.canWrite() && !(await handlers.needWrite())) return;       // luoke-mail#18
      withBusy(btn, pending, onclick);
    } }, ico ? icon(ico) : null, label);
}
const IRREVERSIBLE = ["trash", "spam"];

// mail with a request still under way: another card's request finishing redraws the list, and the card came
// back with its buttons live while its own archive was still running (10-23). Drawn busy until done.
const pending = new Map();                      // message id -> what is being done
export function markPending(ids, label) { ids.forEach((id) => pending.set(id, label)); }
export function clearPending(ids) { ids.forEach((id) => pending.delete(id)); }
function showPending(card, ids) {
  const label = ids.map((id) => pending.get(id)).find(Boolean);
  if (!label) return card;
  card.classList.add("working");
  card.querySelectorAll("button, select").forEach((b) => { b.disabled = true; });
  const row = card.querySelector(".card-actions");
  if (row) row.prepend(el("span", { class: "status busy" }, label));
  return card;
}
const doing = (op, n = 1) => `正在${S.state.ops[op]}${n > 1 ? ` ${n} 封` : ""}…`;
const OP_ICON = { archive: "archive", trash: "trash-2", spam: "shield-alert", complete: "archive", flag: "star" };
// the suggestion's badge (luoke-brand DESIGN.md 6): what needs the user stands out, the rest is quiet
const BADGE = { reply: "brand", schedule: "warning", follow_up: "warning", archive: "", ignore: "" };

function eventText(ev) {
  return [shortDate(ev.date), ev.start ? ev.start + (ev.end ? "–" + ev.end : "") : "全天", ev.title, ev.location].filter(Boolean).join(" · ");
}

// "今天 10:40", "昨天 18:02", "10/21" otherwise: a bare time read as today's even when it was not
function when(date) {
  if (!date) return "";
  const day = (n) => { const t = new Date(); t.setDate(t.getDate() - n);
    return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`; };
  const d = date.slice(0, 10);
  return d === day(0) ? "今天 " + date.slice(11, 16) : d === day(1) ? "昨天 " + date.slice(11, 16) : shortDate(d);
}

function overrideSelect(ids, sample, count) {
  const sel = el("select", { "aria-label": "更改建议" },
    el("option", { value: "" }, "更改建议"), ACTION_ORDER.filter((a) => a !== sample.action).map((a) => el("option", { value: a }, actionLabel(a))));
  sel.addEventListener("change", () => { const a = sel.value; sel.value = ""; if (a) handlers.override(ids, a, sample, count); });
  // a ghost button that hugs its text; the transparent select on top still opens the native list
  return el("span", { class: "override push" }, "更改建议", icon("chevron-down", 14), sel);
}

function suggestionBadge(it) {
  if (it.suspicious) return el("span", { class: "chip danger" }, icon("shield-alert", 12), "可疑：请勿点击链接或转账");
  if (!it.action) return el("span", { class: "chip soft" }, "未分析");
  return el("span", { class: "chip " + (BADGE[it.action] || "") }, actionLabel(it.action));
}

// the first rows: the subject (what you read first), then who · address · when, then the summary;
// a group card has no subject, so the sender leads
function top(name, addr, date, subject, summary, count) {
  const who = name || addr || "";
  return el("div", { class: "card-top" },
    el("span", { class: "avatar", "aria-hidden": "true" }, who.trim().charAt(0) || "?"),
    el("div", { class: "card-main" },
      subject
        ? el("div", { class: "card-line" }, el("h4", { class: "card-subject" }, subject), el("span", { class: "card-time" }, date))
        : el("div", { class: "card-line" }, el("span", { class: "card-from" }, who),
          count ? el("span", { class: "chip soft" }, `${count} 封`) : null,
          name && addr ? el("span", { class: "card-addr" }, addr) : null, el("span", { class: "card-time" }, date)),
      subject ? el("div", { class: "card-line card-who" }, el("span", null, who), name && addr ? el("span", { class: "card-addr" }, addr) : null) : null,
      summary ? el("p", { class: "summary" }, summary) : null));
}

export function renderCard(it, opts = {}) {
  const preview = !!opts.preview;
  const card = el("article", { class: "card" + (it.suspicious ? " suspicious" : "") });
  card.append(top(it.from_name, it.from_addr, when(it.date), it.subject, it.summary && it.summary !== it.subject ? it.summary : null));
  const body = el("div", { class: "card-indent" });
  // the fourth row: the suggestion and why
  const tags = el("div", { class: "card-tags" }, suggestionBadge(it));
  if (!it.suspicious && it.priority === "high") tags.append(el("span", { class: "chip outline" }, "高优先级"));
  // over already: an appointment that has been, a deadline that passed (10-22, luoke-mail#22)
  if (it.past_event) tags.append(el("span", { class: "chip soft" }, `日程已过（${shortDate(it.past_event)}）`));
  else if (it.in_calendar) tags.append(el("span", { class: "chip success" }, "已在日程中"));
  else if (it.due_date) tags.append(el("span", { class: "chip " + (it.overdue ? "danger" : "warning") }, (it.overdue ? "已过期 " : "截止 ") + shortDate(it.due_date)));
  if (it.thread) tags.append(el("span", { class: "chip soft" }, it.thread.what ? `${it.thread.label}：${it.thread.what}` : it.thread.label));
  if (it.flagged) tags.append(el("span", { class: "chip soft" }, "已加星标"));
  if (opts.archived) tags.append(el("span", { class: "chip soft" }, "已归档"));
  if (it.source === "rule-differs") tags.append(el("span", { class: "chip outline", title: "这封邮件可能需要你亲自处理，详见理由" }, "与规则不同"));
  else if (it.suggested_by) tags.append(el("span", { class: "chip soft" }, it.suggested_by));
  if (it.reason) tags.append(el("span", { class: "reason" }, it.reason));
  body.append(tags);
  const events = it.events && it.events.length ? it.events : it.event ? [it.event] : [];
  const several = events.length > 1;        // each has its own line; Google 日历 takes one at a time (luoke-mail#34)
  const google = (currentMailbox() || {}).provider === "gmail";
  const open = !preview && it.action === "schedule" && !it.suspicious;
  events.forEach((ev) => {
    const line = el("p", { class: "event" }, icon("calendar-plus", 14), " ", eventText(ev));
    if (several && open && google && !ev.past && !ev.added) line.append(" ", el("button", { type: "button", class: "btn small", onclick: () => handlers.calendar(it, ev.index) }, "添加"));
    else if (several && (ev.past || ev.added)) line.append(" ", el("span", { class: "chip soft" }, ev.past ? "已过" : "已添加"));
    body.append(line);
  });
  if (it.action === "reply" && it.reply_points && it.reply_points.length) body.append(el("ul", { class: "points" }, it.reply_points.map((p) => el("li", null, p))));
  if (it.error) body.append(el("p", { class: "err" }, "分析失败：" + it.error));

  // the fifth row: what to do
  const calendarBtn = () => el("button", { type: "button", class: "btn small primary", onclick: () => handlers.calendar(it) }, icon("calendar-plus"),
    google ? "添加到 Google 日历" : "下载日历文件");
  const draftBtn = (box) => el("button", { type: "button", class: "btn small primary", onclick: () => openDraft(it, box) }, icon("pen-line"),
    it.draft_text ? "查看草稿" : "起草回复");
  if (!preview && it.action && opts.archived) {
    // archived by the user: only what is still for them -- the calendar, a reply -- and 知道了; nothing is moved
    const row = el("div", { class: "card-actions" });
    const draftBox = el("div", { class: "draft", hidden: true });
    if (it.action === "schedule" && it.event && !it.past_event && !it.in_calendar && !(several && google)) row.append(calendarBtn());
    if (it.action === "reply") row.append(draftBtn(draftBox));
    row.append(el("button", { type: "button", class: "btn small", title: "不再列出，不更改邮箱", onclick: () => handlers.act([it.id], "skip") }, icon("check"), "知道了"));
    body.append(row, draftBox);
  } else if (!preview && it.action) {
    const row = el("div", { class: "card-actions" });
    const draftBox = el("div", { class: "draft", hidden: true });
    const p = primaryOp(it);
    const archive = () => writeBtn("归档", () => handlers.act([it.id], "archive"), "btn small ghost", doing("archive"), false, "archive");
    if (it.action === "reply" && !it.suspicious) {
      row.append(draftBtn(draftBox), archive());
    } else if (it.action === "schedule" && !it.suspicious) {
      // over, or already put in the calendar from another mail about it: nothing left but to file it
      if (it.past_event || it.in_calendar) row.append(writeBtn("归档", () => handlers.act([it.id], "archive"), "btn small primary", doing("archive"), false, "archive"));
      else {
        if (it.event) { if (!(several && google)) row.append(calendarBtn()); }   // several in Gmail: a button on each line
        // no day and time to put in a calendar: keep it in sight instead (luoke-mail#27)
        else if (!it.flagged) row.append(writeBtn("加星标", () => handlers.act([it.id], "flag"), "btn small primary", doing("flag"), false, "star"));
        row.append(archive());
      }
    } else if (it.action === "follow_up" && !it.suspicious) {
      if (!it.flagged) row.append(writeBtn("加星标", () => handlers.act([it.id], "flag"), "btn small primary", doing("flag"), false, "star"));
      row.append(writeBtn("完成并归档", () => handlers.act([it.id], "complete"), "btn small ghost", doing("archive"), false, "archive"));
    } else if (p) {
      row.append(writeBtn(p.label, () => handlers.act([it.id], p.op), IRREVERSIBLE.includes(p.op) ? "btn small danger" : "btn small primary",
        doing(p.op), false, OP_ICON[p.op]));
    }
    row.append(el("button", { type: "button", class: "btn small ghost", title: "从列表中移除，不更改邮箱", onclick: () => handlers.act([it.id], "skip") }, "跳过"));
    row.append(overrideSelect([it.id], it, 1));
    body.append(row, draftBox);
  }
  const acts = body.querySelector(".card-actions");
  const more = acts || el("div", { class: "card-actions" });
  if (it.open_url) more.append(el("a", { href: it.open_url, target: "_blank", rel: "noopener noreferrer", class: acts?.querySelector(".override") ? null : "push" }, "在 Gmail 中打开 ↗"));
  if (!acts && more.childElementCount) body.append(more);
  if (it.body) body.append(el("details", { class: "source" }, el("summary", null, "查看原文"),
    it.date ? el("p", { class: "meta" }, "发送时间：" + it.date) : null, el("pre", null, it.body)));
  card.append(body);
  return showPending(card, [it.id]);
}

async function openDraft(it, box) {
  box.hidden = false;
  box.textContent = "";
  const area = el("textarea", { rows: "8", "aria-label": "回复草稿", spellcheck: "false" });
  const status = el("p", { class: "status busy" }, "正在生成草稿…");
  box.append(area, status);
  // contact details the mail never had come first: they are to be checked before sending
  const said = (plain) => { status.textContent = it.draft_note || plain; status.className = it.draft_note ? "status warn" : "status hint"; };
  if (it.draft_text) { area.value = it.draft_text; said(""); }
  else {
    try { area.value = await handlers.draft(it); said("修改后保存到草稿箱，由你在邮箱中发送。"); }
    catch (err) { status.textContent = err.message; status.className = "status err"; }
  }
  const actions = el("div", { class: "card-actions" },
    writeBtn("保存到草稿箱", () => handlers.saveDraft(it, area.value), "btn small primary", "正在保存…"),
    el("button", { type: "button", class: "btn small", onclick: async () => {
      status.textContent = "正在重新生成…"; status.className = "status busy";
      try { it.draft_text = null; area.value = await handlers.draft(it); said(""); }
      catch (err) { status.textContent = err.message; status.className = "status err"; }
    } }, "重新生成"));
  box.append(actions);
}

export function renderGroup(entry) {
  const first = entry.items[0];
  const ids = entry.items.map((i) => i.id);
  const lead = entry.items.find((i) => i.source === "model") || first;
  const card = el("article", { class: "card group" });
  card.append(top(entry.name, first.from_addr, when(entry.items[0].date), null,
    lead.summary && lead.summary !== lead.subject ? lead.summary : null, entry.count));
  const body = el("div", { class: "card-indent" });
  const tags = el("div", { class: "card-tags" }, suggestionBadge(first));
  if (first.suggested_by) tags.append(el("span", { class: "chip soft" }, first.suggested_by));
  if (lead.reason) tags.append(el("span", { class: "reason" }, lead.reason));
  body.append(tags);
  const row = el("div", { class: "card-actions" });
  const p = primaryOp(first);
  if (p) row.append(writeBtn(`全部${p.label}`, () => handlers.act(ids, p.op), IRREVERSIBLE.includes(p.op) ? "btn small danger" : "btn small primary",
    doing(p.op, ids.length), IRREVERSIBLE.includes(p.op) && ids.length > 1, OP_ICON[p.op]));
  row.append(el("button", { type: "button", class: "btn small ghost", onclick: () => handlers.act(ids, "skip") }, "全部跳过"));
  row.append(overrideSelect(ids, first, entry.count));
  body.append(row, el("details", { class: "members" }, el("summary", null, `查看全部 ${entry.count} 封`),
    el("ul", null, entry.items.map((i) => el("li", null, el("span", { class: "meta" }, (i.date || "").slice(5, 10) + " "), i.subject)))));
  card.append(body);
  return showPending(card, ids);
}
