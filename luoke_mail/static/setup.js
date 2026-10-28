// Setup steps: connect the mailbox, then connect a local model service and pick one of its models. The mailbox
// form also serves 设置: changing a connected mailbox, or connecting another one.
import { $, S, api, el, hasModel, mailboxes, nav, setSource, setStatus, setupDone } from "./core.js";
import { renderCard } from "./cards.js";

const DOMAIN_GUESS = { "gmail.com": "gmail", "googlemail.com": "gmail", "qq.com": "qq", "foxmail.com": "qq", "163.com": "163", "126.com": "126" };
export const flow = { demoIntent: false };

// ---- mail ------------------------------------------------------------------------------------------
let provider = null;
let editing = null;           // the mailbox being changed (its key), or null: connecting another one
function renderProviders() {
  const box = $("providers");
  box.textContent = "";
  for (const p of S.state.providers) {
    box.append(el("button", { type: "button", class: "provider", role: "radio", "aria-checked": String(p.key === provider),
      onclick: () => selectProvider(p.key) }, p.name));
  }
}
function selectProvider(key) {
  provider = key;
  const p = S.state.providers.find((x) => x.key === key);
  renderProviders();
  const g = $("guide");
  g.hidden = false;
  g.textContent = "";
  const shots = SHOTS[key] || {};
  const used = new Set();
  g.append(el("ol", null, p.steps.map((s, i) => el("li", null, el("span", null, ...linked(s, p.links, used)), shotRow(shots.steps?.[i])))));
  if (p.links.length && !STEP_LINKS_ONLY.includes(key)) g.append(el("div", { class: "links" }, p.links.map((l) =>
    el("a", { href: l.url, "data-account": l.account ? "1" : null, target: "_blank", rel: "noopener noreferrer" }, l.label + " ↗"))));
  if (p.notes) g.append(el("p", { class: "note" }, p.notes));
  if (shots.revoke) g.append(el("details", { class: "trouble" }, el("summary", null, "以后不想让螺壳读了？"),
    el("p", null, shots.revoke.text), shotRow([shots.revoke.img])));
  if (p.trouble.length) g.append(el("details", { class: "trouble" }, el("summary", null, "连不上？"),
    el("ul", null, p.trouble.map((t) => el("li", null, t)))));
  accountLinks();
  $("secret-label").textContent = p.secret_label;
  $("secret-hint").textContent = p.secret_hint;
  const cur = editing && S.state.accounts.find((a) => a.key === editing);
  $("mail-secret").placeholder = cur && cur.has_secret && cur.provider === key ? "已保存（无需修改可留空）" : `粘贴${p.secret_label}`;
  $("custom-host").hidden = key !== "custom";
}

// a link's page named in a step is a link right there, the first time: 「两步验证」, or the mailbox's own name for
// 「打开 QQ 邮箱」; Gmail's are only for this step, the others are listed again below the steps
const STEP_LINKS_ONLY = ["gmail"];
function linkTo(l, text) {
  return el("a", { href: l.url, "data-account": l.account ? "1" : null, target: "_blank", rel: "noopener noreferrer" }, text);
}
function linked(text, links, used) {
  let out = [text];
  for (const l of links) {
    if (used.has(l.url)) continue;
    const name = l.label.replace(/^打开\s*/, "");
    out = out.flatMap((part) => {
      if (typeof part !== "string" || used.has(l.url)) return [part];
      const quoted = `「${name}」`, at = part.indexOf(quoted) >= 0 ? part.indexOf(quoted) : part.indexOf(name);
      if (at < 0) return [part];
      used.add(l.url);
      const len = part.startsWith(quoted, at) ? quoted.length : name.length + (part.startsWith("网页版", at + name.length) ? 3 : 0);
      const before = part.slice(0, at).replace(/下方$/, "");          // the link is here now, not below
      return [before, linkTo(l, part.slice(at, at + len) + " ↗"), part.slice(at + len)];
    });
  }
  return out;
}

// screenshots of the mailbox's own pages, by step: small in the list, full size on a click
const SHOTS = {
  "163": { steps: [["163-1-settings"], ["163-2-service", "163-2-confirm"], null, ["163-4-code"]] },
  gmail: { steps: [null, ["gmail-2-create"], ["gmail-3-code"]] },
  qq: { steps: [["qq-1-settings"], ["qq-2-service"], ["qq-3-verify"], ["qq-4-code"]],
    revoke: { text: "在「账号与安全」→「设备管理」→「授权码管理」里，找到这个授权码，点「停用」。", img: "qq-5-revoke" } },
};
SHOTS["126"] = SHOTS["163"];                    // 126 shares 163's pages
function shotRow(names) {
  if (!names || !names.length) return null;
  return el("span", { class: "shots" }, names.map((n) => el("button", { type: "button", class: "shot", "aria-label": "查看大图",
    onclick: () => zoom(n) }, el("img", { src: `img/guide/${n}.webp`, alt: "", loading: "lazy" }))));
}
function zoom(name) {
  const d = el("dialog", { class: "shot-zoom", onclick: () => d.close() }, el("img", { src: `img/guide/${name}.webp`, alt: "" }));
  d.addEventListener("close", () => d.remove());
  document.body.append(d);
  d.showModal();
}

// Google's pages for the account being connected, not the browser's first signed-in one
function accountLinks() {
  const addr = $("mail-address").value.trim();
  for (const a of $("guide").querySelectorAll("a[data-account]")) {
    const u = new URL(a.href);
    if (addr.includes("@")) u.searchParams.set("authuser", addr); else u.searchParams.delete("authuser");
    a.href = u.toString();
  }
}

// fill the form for one mailbox (`key`), or empty for connecting another one
function fillMail(key) {
  editing = key || null;
  const m = (key && S.state.accounts.find((a) => a.key === key)) || {};
  $("mail-title").textContent = key ? "修改邮箱" : mailboxes().length ? "添加邮箱" : "连接邮箱";
  provider = null;
  renderProviders();
  $("guide").hidden = true;
  $("custom-host").hidden = true;
  $("secret-label").textContent = "授权码";             // until a type is chosen
  $("secret-hint").textContent = "";
  $("mail-secret").placeholder = "";
  if (m.provider) selectProvider(m.provider);
  $("mail-address").value = m.address || "";
  accountLinks();
  $("mail-host").value = m.host || "";
  $("mail-port").value = m.port || 993;
  $("mail-secret").value = "";
  setStatus("mail-status", "");
}

// from 设置: change a connected mailbox, or connect another one
export function editMailbox(key) {
  fillMail(key);
  nav.go("mail");
}

export function initMail() {
  fillMail(S.state.mail.key || null);            // first run: the first mailbox, if it was saved before
  $("mail-address").addEventListener("input", (e) => {    // the address picks its guide
    const d = e.target.value.split("@")[1];
    const guess = d && DOMAIN_GUESS[d.trim().toLowerCase()];
    if (guess && guess !== provider) selectProvider(guess);
    accountLinks();
  });
  $("toggle-secret").addEventListener("click", (e) => {
    const inp = $("mail-secret");
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    e.target.textContent = show ? "隐藏" : "显示";
    e.target.setAttribute("aria-pressed", String(show));
  });
  $("mail-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!provider) return setStatus("mail-status", "请选择邮箱类型。", "err");
    const btn = $("mail-save");
    btn.disabled = true;
    setStatus("mail-status", "正在连接…", "busy");
    try {
      const saved = await api("/api/mail", { key: editing, provider, address: $("mail-address").value, secret: $("mail-secret").value,
        host: $("mail-host").value, port: $("mail-port").value });
      $("mail-secret").value = "";
      editing = saved.key;
      $("mail-title").textContent = "修改邮箱";
      const r = await api("/api/mail/test", { key: saved.key });
      await nav.refresh();
      setSource(saved.key);
      setStatus("mail-status", `连接成功。收件箱共 ${r.total} 封，未读 ${r.unseen} 封。`, "ok");
      $("mail-status").append(" ", el("button", { class: "btn primary", type: "button", onclick: () => nav.go(hasModel() ? "inbox" : "model") },
        hasModel() ? "前往收件箱" : "下一步：选择本机模型"));
      if (setupDone()) $("mail-status").append(" ", el("button", { class: "btn", type: "button", onclick: () => nav.go("settings") }, "返回设置"));
    } catch (err) {
      await nav.refresh();
      setStatus("mail-status", err.message, "err");
      const trouble = $("guide").querySelector("details.trouble");
      if (trouble) trouble.open = true;
    } finally {
      btn.disabled = false;
    }
  });
}

// ---- model: connect a service first, then pick one of *its* models ---------------------------------
const START_HINTS = {
  "oMLX": "打开 oMLX 应用即可",
  "LM Studio": "在 LM Studio 的 Developer 页面点击 Start Server",
  "Ollama": "打开 Ollama 应用即可",
  "llama.cpp": "用 llama-server 启动一个模型",
};
const typedKeys = {};       // base_url -> API key typed in this session (saved only on "保存")
let scan = null;
let service = null;         // a model service, or HUB
let chosen = null;          // {base_url, engine_name, model}, or {via: "hub"}
const HUB = { hub: true, name: "螺壳" };
// luoke-mail's tasks as the user knows them (what pauses when 螺壳主控 does not offer one)
const TASK_NAMES = { triage: "分析邮件", draft: "起草回复", senders: "订阅体检", cleanup: "清理", "digest-read": "阅读订阅",
  "digest-pick": "挑选精选", interests: "推测关注的话题", "learn-sent": "了解你", "learn-orgs": "了解你" };

function recsFor(eng) {
  const mem = scan.memory_gb;
  const out = [];
  scan.recommendations.forEach((rec, tier) => {
    // the quant we actually benchmarked first, then MTP builds (they decode faster)
    const rank = (m) => { const l = m.toLowerCase(); return (rec.prefer || []).some((p) => l.includes(p)) ? 0 : 2 + Number(!l.includes("mtp")); };
    const hits = eng.models.filter((m) => rec.match.some((k) => m.toLowerCase().includes(k))).sort((a, b) => rank(a) - rank(b));
    hits.forEach((m) => out.push({ model: m, rec, tier, fits: mem == null || mem >= rec.min_gb }));
  });
  return out;
}

export async function scanEngines() {
  const mem = S.state.machine.memory_gb;
  $("machine-line").textContent = mem
    ? `本机内存 ${mem} GB。连接正在运行的模型服务，然后选择模型。`
    : "连接正在运行的模型服务，然后选择模型。";
  const box = $("services");
  box.textContent = "";
  box.append(el("p", { class: "status busy" }, "正在查找本机模型服务…"));
  try {
    scan = await api("/api/engines/scan", { keys: typedKeys, custom: $("custom-base").value });
  } catch (err) {
    box.textContent = "";
    box.append(el("p", { class: "status err" }, err.message));
    return;
  }
  const usable = scan.engines.filter((e) => e.ok && e.models.length);
  const hubOk = scan.hub && scan.hub.ok;
  if ((service === HUB || (!service && S.state.engine.via === "hub")) && hubOk) {
    service = HUB;
    chosen = { via: "hub" };
    renderServices();
    renderModels();
    return;
  }
  const prev = service && scan.engines.find((e) => e.base_url === service.base_url && e.ok && e.models.length);
  const saved = scan.engines.find((e) => e.base_url === S.state.engine.base_url && e.ok && e.models.length);
  service = prev || saved || usable.find((e) => recsFor(e).some((r) => r.fits)) || usable[0] || null;
  if (service && (!chosen || chosen.base_url !== service.base_url)) {
    const keep = S.state.engine.base_url === service.base_url && service.models.includes(S.state.engine.model) ? S.state.engine.model : null;
    const best = recsFor(service).find((r) => r.fits);
    chosen = { base_url: service.base_url, engine_name: service.name, model: keep || (best ? best.model : null) };
  }
  renderServices();
  renderModels();
}

function hubCard() {
  const h = scan.hub;
  const selected = service === HUB;
  const status = h.ok ? el("span", { class: "dot on" }, h.status === "available" ? "运行中" : "运行中 · 正在准备模型")
    : el("span", { class: "dot off" }, "未运行");
  const card = el("div", { class: "service" + (h.ok ? " usable" : "") + (selected ? " selected" : ""),
    role: h.ok ? "radio" : null, "aria-checked": h.ok ? String(selected) : null, tabindex: h.ok ? "0" : null },
    el("div", { class: "service-name" }, "螺壳"), el("div", { class: "service-addr" }, "按本机配置选择模型"), status);
  if (h.ok) {
    const pick = () => { service = HUB; chosen = { via: "hub" }; renderServices(); renderModels(); };
    card.addEventListener("click", pick);
    card.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  }
  return card;
}

function renderServices() {
  const box = $("services");
  box.textContent = "";
  if (scan.hub && scan.hub.found) box.append(hubCard());
  for (const eng of scan.engines) {
    const usable = eng.ok && eng.models.length;
    const selected = service && service.base_url === eng.base_url;
    let status;
    if (usable) status = el("span", { class: "dot on" }, `运行中 · ${eng.models.length} 个模型`);
    else if (eng.auth_required) status = el("span", { class: "dot key" }, "正在运行，需要 API Key");
    else if (eng.ok) status = el("span", { class: "dot on" }, "运行中 · 没有模型");
    else status = el("span", { class: "dot off" }, "未运行");
    const card = el("div", { class: "service" + (usable ? " usable" : "") + (selected ? " selected" : ""),
      role: usable ? "radio" : null, "aria-checked": usable ? String(!!selected) : null, tabindex: usable ? "0" : null },
      el("div", { class: "service-name" }, eng.name), el("div", { class: "service-addr" }, eng.base_url.replace("http://", "")), status);
    if (usable) {
      const pick = () => { service = eng; const best = recsFor(eng).find((r) => r.fits);
        chosen = { base_url: eng.base_url, engine_name: eng.name, model: best ? best.model : null }; renderServices(); renderModels(); };
      card.addEventListener("click", pick);
      card.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    } else if (eng.auth_required) {
      const key = el("input", { type: "password", placeholder: eng.name === "oMLX" ? "oMLX 设置中的 API Key" : "API Key",
        "aria-label": eng.name + " 的 API Key", autocomplete: "new-password", spellcheck: "false" });
      const go = () => { if (!key.value.trim()) return key.focus(); typedKeys[eng.base_url] = key.value.trim(); service = eng; chosen = null; scanEngines(); };
      key.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      card.append(el("div", { class: "keyrow" }, key, el("button", { type: "button", class: "btn small", onclick: go }, "连接")));
    } else if (!eng.ok && START_HINTS[eng.name]) {
      card.append(el("div", { class: "service-hint" }, START_HINTS[eng.name]));
    }
    box.append(card);
  }
  if (!scan.engines.some((e) => e.ok) && !(scan.hub && scan.hub.ok)) box.append(el("p", { class: "hint wide" }, "未找到正在运行的模型服务。启动后点击「重新扫描」。"));
}

function renderModels() {
  const wrap = $("models-wrap");
  const box = $("models");
  box.textContent = "";
  if (!service) { wrap.hidden = true; updateSaveButton(); return; }
  wrap.hidden = false;
  $("models-service").textContent = service.name;
  $("models-head").textContent = service === HUB ? "模型" : "选择模型";
  if (service === HUB) {
    const off = [...new Set((scan.hub.unavailable || []).map((t) => TASK_NAMES[t] || "其他功能"))];
    box.append(el("div", { class: "note-card" }, el("strong", null, "由螺壳按本机配置选择模型。"),
      scan.hub.status === "available" ? null : el("p", null, "螺壳正在准备模型，准备好之后才能分析邮件。"),
      off.length ? el("p", null, `螺壳暂不提供：${off.join("、")}。这些功能会暂停。`) : null));
    updateSaveButton();
    return;
  }
  const recs = recsFor(service);
  const fitting = [];                    // at most three cards: one per tested family, highest tiers first
  for (const r of recs) {
    if (r.fits && !fitting.some((f) => f.tier === r.tier)) fitting.push(r);
    if (fitting.length === 3) break;
  }
  const choose = (m) => { chosen = { base_url: service.base_url, engine_name: service.name, model: m }; renderModels(); };
  if (fitting.length) {
    const list = el("div", { class: "model-cards", role: "radiogroup", "aria-label": "推荐的模型" });
    fitting.forEach((r, i) => {
      const sel = chosen && chosen.model === r.model;
      const card = el("div", { class: "model-card" + (sel ? " selected" : ""), role: "radio", "aria-checked": String(sel), tabindex: "0" },
        el("div", { class: "model-top" }, el("span", { class: "model-id" }, r.model),
          i === 0 ? el("span", { class: "tag" }, "首选") : el("span", { class: "tag soft" }, "推荐")),
        el("div", { class: "model-family" }, r.rec.title), el("p", null, r.rec.why));
      card.addEventListener("click", () => choose(r.model));
      card.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(r.model); } });
      list.append(card);
    });
    box.append(list);
  } else {
    const want = scan.recommendations.filter((r) => scan.memory_gb == null || scan.memory_gb >= r.min_gb).slice(0, 2);
    box.append(el("div", { class: "note-card" }, el("strong", null, scan.too_small
      ? `本机内存 ${scan.memory_gb} GB，不足以可靠地分拣邮件。` : `${service.name} 中没有推荐的模型。`),
    el("p", null, scan.too_small
      ? "能装进这台电脑的模型会漏掉较多诈骗和可疑邮件，所以不推荐。仍可从下方选择模型自行试用，示例收件箱也照常可用。"
      : want.length ? `根据本机内存，建议在 ${service.name} 中下载 ${want.map((w) => w.title).join(" 或 ")}，下载后点击「重新扫描」。也可以从下方选择其他模型。` : "可以从下方选择其他模型。")));
  }
  const others = service.models.filter((m) => !fitting.some((r) => r.model === m));
  if (others.length) {
    const details = el("details", { class: "others", open: !fitting.length || (chosen && others.includes(chosen.model)) });
    details.append(el("summary", null, fitting.length ? `其他模型（${others.length} 个）` : `${service.name} 中的模型（${others.length} 个）`));
    let filter = null;
    if (others.length > 10) {
      filter = el("input", { type: "search", class: "filter", placeholder: "按名称筛选", "aria-label": "筛选模型", spellcheck: "false" });
      details.append(filter);
    }
    const ul = el("div", { class: "model-list", role: "radiogroup", "aria-label": "其他模型" });
    const draw = () => {
      ul.textContent = "";
      const q = filter ? filter.value.trim().toLowerCase() : "";
      others.filter((m) => !q || m.toLowerCase().includes(q)).forEach((m) => {
        const r = recs.find((x) => x.model === m);
        const note = !r ? null : !r.fits ? `需要约 ${r.rec.min_gb} GB 内存`
          : (r.rec.prefer || []).some((p) => m.toLowerCase().includes(p)) ? `已评测 · ${r.rec.title}` : `${r.rec.title} 系列的其他版本`;
        ul.append(el("label", { class: "model-row" },
          el("input", { type: "radio", name: "other-model", checked: chosen && chosen.model === m, onchange: () => choose(m) }),
          el("span", { class: "model-id" }, m), note ? el("span", { class: "muted small" }, note) : null));
      });
    };
    if (filter) filter.addEventListener("input", draw);
    draw();
    details.append(ul);
    box.append(details);
  }
  updateSaveButton();
}

function updateSaveButton() {
  const btn = $("engine-save");
  btn.disabled = !(chosen && (chosen.model || chosen.via === "hub"));
  btn.textContent = "保存并测试";
}

export function initModel() {
  $("rescan").addEventListener("click", scanEngines);
  $("custom-connect").addEventListener("click", () => {
    const base = $("custom-base").value.trim().replace(/\/$/, "");
    if (!base) return $("custom-base").focus();
    if ($("custom-key").value.trim()) typedKeys[base] = $("custom-key").value.trim();
    service = { base_url: base };
    chosen = null;
    scanEngines();
  });
  $("engine-save").addEventListener("click", async () => {
    if (!chosen || !(chosen.model || chosen.via === "hub")) return;
    const btn = $("engine-save");
    btn.disabled = true;
    $("engine-sample").textContent = "";
    const what = mailboxes().length ? "收件箱中最新的邮件" : "示例邮件";
    setStatus("engine-status", `正在${chosen.via === "hub" ? "通过螺壳" : "用 " + chosen.model + " "}分析${what}…首次加载模型可能需要一两分钟。`, "busy");
    try {
      await api("/api/engine", { ...chosen, api_key: typedKeys[chosen.base_url] || "" });
      const r = await api("/api/engine/test", {});
      await nav.refresh();
      const used = r.source === "inbox" ? "收件箱中最新的邮件" : "示例邮件";
      setStatus("engine-status", `测试成功，用时 ${r.result.seconds} 秒。以下是对${used}的分析：`, "ok");
      if (r.warning) $("engine-sample").append(el("p", { class: "hint" }, r.warning));
      $("engine-sample").append(renderCard({ ...r.mail, ...r.result, id: 0 }, { preview: true }));
      const demo = !mailboxes().length;            // with a mailbox connected there is no sample inbox (luoke-mail#6)
      $("engine-status").append(" ", el("button", { class: "btn primary", type: "button",
        onclick: () => { setSource(demo ? "demo" : mailboxes()[0].key); nav.go("inbox"); } }, demo ? "下一步：打开示例收件箱" : setupDone() ? "前往收件箱" : "下一步：前往收件箱"));
      if (setupDone()) $("engine-status").append(" ", el("button", { class: "btn", type: "button", onclick: () => nav.go("settings") }, "返回设置"));
    } catch (err) {
      setStatus("engine-status", err.message, "err");
    } finally {
      updateSaveButton();
    }
  });
}
