// Rules (sender / domain / list -> fixed action, per inbox), their Gmail export, and the action log with undo.
import { icon } from "./icons.js";
import { $, S, api, confirmDialog, el, setStatus, toast, waitJob, withBusy } from "./core.js";
import { exportGmailFilters } from "./gmail.js";
import { loadInbox } from "./inbox.js";

export function initRules() {
  const kinds = S.state.rule_kinds;
  $("rule-kind").append(...Object.entries(kinds).map(([k, v]) => el("option", { value: k }, v)));
  $("rule-action").append(...Object.entries(S.state.rule_actions).map(([k, v]) => el("option", { value: k }, v)));
  $("rule-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/rules", { source: S.source, kind: $("rule-kind").value, value: $("rule-value").value, action: $("rule-action").value });
      $("rule-value").value = "";
      setStatus("rule-status", "");
      toast(r.message, () => undo(r.log_id));
      showRules();
    } catch (err) { setStatus("rule-status", err.message, "err"); }
  });
}

async function undo(logId) {
  try { const u = await api("/api/undo", { source: S.source, log_id: logId }); toast(u.message); }
  catch (err) { toast(err.message); }
  showRules();
  loadInbox();
}

function gmailBar(view) {
  const bar = $("rules-gmail");
  bar.textContent = "";
  const exportable = view.rules.filter((r) => S.state.gmail_filter_actions[r.action]);
  bar.hidden = !exportable.length;
  if (!exportable.length) return;
  if (!view.gmail) {
    bar.append(el("span", null, "规则仅在螺壳邮件助手分析邮件时生效。",
      el("span", { class: "muted" }, "如需由邮箱直接拦截发件人，可在「订阅体检」中查看拉黑方法。")));
    return;
  }
  const fresh = exportable.filter((r) => !r.exported_at);
  const again = !fresh.length;
  bar.append(
    el("span", null, again ? "规则已全部导出到 Gmail。" : "规则仅在螺壳邮件助手分析邮件时生效。",
      el("span", { class: "muted" }, again ? "新增的规则需要再次导出。"
        : `将 ${fresh.length} 条规则导出为 Gmail 过滤器后，Gmail 会自动处理之后的邮件。`)),
    el("button", { type: "button", class: "btn small" + (again ? "" : " primary"),
      onclick: () => exportGmailFilters(view.rules, { includeExported: again, onDone: showRules }) }, again ? "全部重新导出" : "导出到 Gmail"));
}

// Before a rule becomes a Gmail filter: tried on the past year -- how much each way of matching catches, what it
// should not touch -- and the user picks the way (2026-10-17)
async function previewFilter(rule, btn, view) {
  let p;
  await withBusy(btn, "正在预演…", async () => {
    try {
      const job = await api("/api/rules/filter-preview", { source: S.source, id: rule.id });
      p = await waitJob(job.id);                  // the stage shows in 进行中; the button stays short
    } catch (err) { toast(err.message); }
  });
  if (!p) return;
  const best = p.ways.find((w) => w.kind !== rule.kind && !w.risky && w.count > p.ways[0].count);
  let chosen = best ? best.kind : rule.kind;
  const options = p.ways.map((w) => {
    const radio = el("input", { type: "radio", name: "filter-way", value: w.kind });
    radio.checked = w.kind === chosen;
    radio.addEventListener("change", () => { chosen = w.kind; });
    const risk = !p.checked ? "（未检查内容）"
      : w.risky ? `，其中 ${w.risky} 封不该一起处理，例如「${w.examples.join("」「")}」` : "，没有发现不该处理的";
    return el("label", { class: "check" }, radio, ` 按${w.label}「${w.value}」：过去一年 ${w.count} 封${risk}`);
  });
  const ok = await confirmDialog("生成 Gmail 过滤器", [
    el("p", null, p.advice),
    ...options,
    p.checked ? null : el("p", { class: "hint" }, "模型暂时不可用，只统计了数量，没有检查内容。"),
  ], "继续");
  if (!ok) return;
  let ids = [rule.id];
  if (chosen !== rule.kind) {                     // a wider way: a rule of its own, then exported
    const w = p.ways.find((x) => x.kind === chosen);
    try {
      await api("/api/rules", { source: S.source, kind: w.kind, value: w.value, action: rule.action });
      const fresh = await api(`/api/rules?source=${S.source}`);
      const made = fresh.rules.find((x) => x.kind === w.kind && x.value === w.value && x.action === rule.action);
      ids = made ? [made.id] : ids;
      view = fresh;
    } catch (err) { toast(err.message); return; }
  }
  exportGmailFilters(view.rules, { ids, onDone: showRules });
}

export async function showRules() {
  const box = $("rules");
  box.textContent = "";
  let view;
  try { view = await api(`/api/rules?source=${S.source}`); }
  catch (err) { $("rules-gmail").hidden = true; box.append(el("p", { class: "empty-state" }, err.message)); return; }
  gmailBar(view);
  const { rules } = view;
  if (!rules.length) { box.append(el("p", { class: "empty-state" }, "还没有规则。在收件箱中更改建议时可以创建规则，也可以在订阅体检中设置。")); return; }
  for (const r of rules) {
    box.append(el("div", { class: "rule-row" },
      el("span", { class: "chip outline" }, S.state.rule_kinds[r.kind] || "规则"), el("span", { class: "rule-value" }, r.value),
      el("span", { class: "muted" }, "→"), el("span", null, S.state.rule_actions[r.action] || "处理"),
      r.exported_at && view.gmail ? el("span", { class: "chip soft", title: `导出于 ${r.exported_at.slice(0, 16)}` }, "已导出到 Gmail") : null,
      el("span", { class: "row-acts" }, el("span", { class: "muted small" }, `命中 ${r.hits} 次`),
      view.gmail && S.state.gmail_filter_actions[r.action]
        ? el("button", { type: "button", class: "btn tiny ghost", onclick: (e) => previewFilter(r, e.currentTarget, view) }, "生成 Gmail 过滤器") : null,
      el("button", { type: "button", class: "btn tiny ghost", title: "删除规则", "aria-label": "删除规则", onclick: async () => {
        try { const d = await api("/api/rules/delete", { source: S.source, id: r.id }); toast(d.message, () => undo(d.log_id)); }
        catch (err) { toast(err.message); }
        showRules();
      } }, icon("x")))));
  }
}

export async function showLog() {
  const box = $("log");
  box.textContent = "";
  let entries;
  try { entries = (await api(`/api/log?source=${S.source}`)).entries; }
  catch (err) { box.append(el("p", { class: "empty-state" }, err.message)); return; }
  if (!entries.length) { box.append(el("p", { class: "empty-state" }, "暂无操作记录。")); return; }
  for (const e of entries) {
    box.append(el("div", { class: "log-row" + (e.undone ? " undone" : "") },
      el("span", { class: "log-time" }, e.ts.slice(5, 16)),
      el("span", { class: "log-what" }, e.detail, e.undone ? el("span", { class: "muted" }, "（已撤销）") : null,
        e.mail ? el("span", { class: "muted log-mail" }, `${e.mail.from ? e.mail.from + " · " : ""}${e.mail.subject}`) : null),
      e.undoable ? el("button", { type: "button", class: "btn tiny ghost row-acts", onclick: (ev) => withBusy(ev.currentTarget, "正在撤销…", async () => {
        try { const r = await api("/api/undo", { source: S.source, log_id: e.id }); toast(r.message); }
        catch (err) { toast(err.message); }
        showLog();
        loadInbox();          // keeps the inbox badge in step
      }) }, icon("undo-2"), "撤销") : null));
  }
}
