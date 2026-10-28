// Entry point: navigation and start-up. The rail shows the setup steps on first run and the everyday pages
// once set up; the mailbox and model forms then live under 设置.
import { $, S, api, el, hasModel, hasToken, lastSource, mailboxes, nav, onSourceChange, renderSources, setSource, setupDone } from "./core.js";
import { flow, initMail, initModel, scanEngines } from "./setup.js";
import { initInbox, loadInbox } from "./inbox.js";
import { initSubs, showSubs } from "./subs.js";
import { initRules, showLog, showRules } from "./rules.js";
import { applyTheme, initSettings, showSettings } from "./settings.js";
import { initClean, leaveClean, showClean } from "./clean.js";
import { showDigest, stopDigest } from "./digest.js";
import { showMemory, stopMemory } from "./memory.js";
import { showReview } from "./review.js";
import { checkActivity, renderRailStatus } from "./auto.js";
import { fillIcons, icon } from "./icons.js";

const SETTINGS_PAGES = ["settings", "mail", "model"];
let current = "welcome";

function paintNav() {
  const daily = setupDone();
  $("steps").hidden = $("steps-label").hidden = $("tools").hidden = daily;
  $("nav").hidden = !daily;
  $("src-label").hidden = !daily;                      // first run: the setup steps only, no mailbox list yet
  document.querySelector("[data-sources]").hidden = !daily;
  // in everyday use the mailbox and model forms are part of 设置
  const here = daily && SETTINGS_PAGES.includes(current) ? "settings" : current;
  document.querySelectorAll(".rail [data-step]").forEach((b) => {
    if (b.dataset.step === here && b.id !== "rail-status") b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
  });
  // the narrow bar scrolls sideways: keep the current page in view
  const cur = document.querySelector('.rail [aria-current="step"]'), rail = document.querySelector(".rail");
  if (cur && matchMedia("(max-width: 820px)").matches) {        // sideways only: the page itself stays where it is
    const r = cur.getBoundingClientRect(), box = rail.getBoundingClientRect();
    rail.scrollLeft += r.left + r.width / 2 - (box.left + box.width / 2);
  }
  // the app (螺壳邮件助手.app) only uses 螺壳主控: no model step
  document.querySelector('#steps [data-step="model"]').parentElement.hidden = !!S.state.hub_only;
  const flags = { mail: mailboxes().length > 0, model: hasModel() };
  document.querySelectorAll("#steps button").forEach((b) => b.classList.toggle("done", !!flags[b.dataset.step]));
  document.querySelectorAll(".back").forEach((b) => { b.hidden = !daily; });
  renderRailStatus();
}

function go(step) {
  if (step === "model" && S.state && S.state.hub_only) {       // nothing to pick: straight to the mail
    if (flow.demoIntent && !mailboxes().length) setSource("demo");
    step = "inbox";
  }
  if (current === "clean" && step !== "clean") leaveClean();
  if (current === "digest" && step !== "digest") stopDigest();
  if (current === "memory" && step !== "memory") stopMemory();
  current = step;
  document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.dataset.panel !== step; });
  paintNav();
  if (step === "model") scanEngines();
  if (step === "inbox") loadInbox();
  if (step === "subs") showSubs();
  if (step === "rules") showRules();
  if (step === "log") showLog();
  if (step === "settings") refresh();            // counts may have moved in the background (了解你, 精选)
  if (step === "clean") showClean();
  if (step === "digest") showDigest();
  if (step === "memory") showMemory();
  if (step === "review") showReview();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function refresh() {
  S.state = await api("/api/state");
  if (renderSources()) setSource(S.source);           // the mailbox shown was removed: show another
  $("version").textContent = S.state.version;
  $("mail-hub-note").hidden = !S.state.hub_only;       // the app: no model step (luoke-mail#6)
  $("store-note").textContent = S.state.store.plaintext
    ? "注意：在此系统上，授权码以明文保存在本机文件中（仅你的账户可读）。"
    : `授权码保存在${S.state.store.label}中。`;
  paintNav();
  if (current === "settings") showSettings();
}

Object.assign(nav, { go, refresh, current: () => current });

// the sidebar folds to its icons (luoke-brand DESIGN.md 7); remembered in this browser
const FOLD_KEY = "luoke-mail.sidebar";
function fold(folded) {
  $("shell").classList.toggle("folded", folded);
  const b = $("fold");
  b.title = folded ? "展开侧栏" : "收起侧栏";
  b.setAttribute("aria-label", b.title);
  b.replaceChildren(icon(folded ? "panel-left-open" : "panel-left-close"));
  try { localStorage.setItem(FOLD_KEY, folded ? "folded" : "open"); } catch (e) { /* not kept */ }
}

(async function init() {
  applyTheme();
  fillIcons();
  let folded = false;
  try { folded = localStorage.getItem(FOLD_KEY) === "folded"; } catch (e) { /* the default */ }
  fold(folded);
  $("fold").addEventListener("click", () => { folded = !folded; fold(folded); });
  // a narrow window has no fold button: show the full bar there, and bring the kept choice back when it widens
  const narrow = matchMedia("(max-width: 820px)");
  const fit = () => $("shell").classList.toggle("folded", !narrow.matches && folded);
  narrow.addEventListener("change", fit);
  fit();
  if (!hasToken()) {
    $("stage").prepend(el("p", { class: "status err" }, "无法验证访问权限。请从「螺壳邮件助手」App 重新打开页面。"));
  }
  // the service may be restarting (the page was opened or refreshed meanwhile): wait for it, never fall back
  // to the first-run steps -- the setup is fine, the page just could not ask yet
  const notice = el("p", { class: "status busy" }, "正在连接螺壳邮件助手…");
  for (;;) {
    try { await refresh(); break; }
    catch (err) {
      if (!notice.isConnected) $("stage").prepend(notice);
      if (!err.offline) { notice.className = "status err"; notice.textContent = err.message; return; }
      await new Promise((res) => setTimeout(res, 2000));
    }
  }
  notice.remove();
  initMail();
  initModel();
  initInbox();
  initSubs();
  initRules();
  initSettings();
  initClean();
  onSourceChange(() => {
    if (current === "inbox") loadInbox();
    if (current === "subs") showSubs();
    if (current === "rules") showRules();
    if (current === "log") showLog();
    if (current === "clean") showClean();
    if (current === "digest") showDigest();
    if (current === "memory") showMemory();
    if (current === "review") showReview();
  });
  const last = lastSource();                 // the mailbox looked at before a reload, while it is still connected
  setSource(mailboxes().some((a) => a.key === last) ? last : mailboxes().length ? mailboxes()[0].key : "demo");
  document.querySelectorAll(".rail [data-step]").forEach((b) => b.addEventListener("click", () => go(b.dataset.step)));
  document.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => { flow.demoIntent = !!b.dataset.demo; go(b.dataset.go); }));
  // background work runs on its own schedule: keep 进行中, the rail and the inbox in step with it -- every two
  // seconds while something runs, every five otherwise
  const follow = async () => {
    const r = document.visibilityState === "visible" ? await checkActivity() : null;
    setTimeout(follow, r && r.job ? 2000 : 5000);
  };
  follow();
  // focus rings only for keyboard users: a mouse click on a menu or button leaves no coloured frame behind
  document.addEventListener("keydown", (e) => { if (e.key === "Tab") document.documentElement.classList.add("kbd"); });
  document.addEventListener("mousedown", () => document.documentElement.classList.remove("kbd"));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    checkActivity();
    if (current === "inbox") loadInbox();
  });
  // 螺壳's 「在邮件助手里看」/「打开设置」 open the page at #inbox / #settings
  const asked = location.hash.slice(1);
  // a first run starts at 开始: nothing set up yet (in the app the model is always there, 螺壳主控's)
  const fresh = S.state.hub_only ? !setupDone() : !hasModel();
  // a notification (今日处理, a habit's work) asked for its page: once
  const start = S.state.start_page;
  if (start) api("/api/start-page/clear", {}).catch(() => {});
  const DAILY = ["review", "inbox", "digest", "clean", "subs", "rules", "log", "memory", "settings"];
  go(fresh ? (asked === "mail" ? "mail" : "welcome") : DAILY.includes(asked) ? asked : start || "inbox");
})();
