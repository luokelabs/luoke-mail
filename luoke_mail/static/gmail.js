// Gmail filter export: local rules only act when luoke-mail runs; a Gmail filter runs on Google's side.
// The user imports the downloaded file in Gmail's settings -- no OAuth, nothing leaves this machine.
import { S, api, confirmDialog, download, el, toast } from "./core.js";

const STEPS = [
  "下载过滤器文件。",
  "在 Gmail 设置中打开「过滤器和屏蔽的地址」（下载后会自动打开），点击页面底部的「导入过滤器」。",
  "选择刚下载的文件，点击「打开文件」，Gmail 会列出将要创建的过滤器。",
  "确认后点击「创建过滤器」。",
];

// rules: [{id, kind, value, action, exported_at}]; ids: export exactly these; otherwise the ones not exported yet
// (or all of them with includeExported) -- importing the same filter twice makes a duplicate in Gmail.
export async function exportGmailFilters(rules, { ids = null, includeExported = false, onDone = null } = {}) {
  const labels = S.state.gmail_filter_actions;
  const chosen = rules.filter((r) => (ids ? ids.includes(r.id) : includeExported || !r.exported_at));
  const ok = chosen.filter((r) => labels[r.action]);
  const skipped = chosen.filter((r) => !labels[r.action]);
  if (!ok.length) { toast("没有可导出的规则。「回复」「加入日程」类规则无法转为过滤器。"); return; }
  const kinds = S.state.rule_kinds;
  const demo = S.source === "demo";
  const go = await confirmDialog("导出到 Gmail", [
    el("p", null, `将导出 ${ok.length} 个过滤器，导入后由 Gmail 自动处理之后的邮件：`),
    el("ul", null, ok.map((r) => el("li", null, `${kinds[r.kind]}「${r.value}」→ ${labels[r.action]}`))),
    ok.some((r) => r.action === "spam")
      ? el("p", { class: "hint" }, "Gmail 过滤器不支持移到垃圾邮件，这类规则会改为移到垃圾箱。") : null,
    skipped.length ? el("p", { class: "hint" }, `另有 ${skipped.length} 条「回复」「加入日程」类规则不会导出。`) : null,
    el("ol", null, STEPS.map((x) => el("li", null, x))),
    el("p", { class: "hint" }, demo ? "示例数据，无需导入。"
      : "如需停用，请在 Gmail 的过滤器设置中删除。在此删除规则不会影响 Gmail。"),
  ], "下载过滤器文件");
  if (!go) return;
  try {
    const r = await api("/api/rules/gmail-filters", { source: S.source, ids, include_exported: includeExported });
    download(r.filename, r.xml, "application/xml");
    if (r.settings_url) window.open(r.settings_url, "_blank", "noopener,noreferrer");
    toast(r.message + (r.settings_url ? "，请在已打开的 Gmail 设置页中导入。" : "。"));
    if (onDone) onDone();
  } catch (err) { toast(err.message); }
}
