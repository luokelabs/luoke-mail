// 进行中: what is running and what waits behind it, at the bottom of the rail on every page. A page's own progress
// goes away when you leave the page; this stays, and leads back to the page the work belongs to.
import { $, api, el, nav } from "./core.js";

const KIND = { older: ["分析以前的未读邮件", "review"], sync: ["同步邮件", "inbox"], auto: ["后台自动整理", "inbox"], old_scan: ["清理：读取邮件", "clean"],
  clean: ["清理：处理邮件", "clean"], judge: ["清理：分析邮件", "clean"], read: ["精选：阅读订阅", "digest"],
  edit: ["精选：挑选", "digest"], profile: ["推测关注的话题", "memory"], learn: ["了解你", "memory"],
  subscriptions: ["订阅体检", "subs"], filter_preview: ["预演过滤器", "rules"] };
const QUEUE = [["older", "以前的未读邮件", "review"], ["judge", "清理", "clean"], ["read", "精选", "digest"], ["learn", "了解你", "memory"]];

// rebuilt only when its shape changes; between polls only the text and the bar move
export function renderActivity(r) {
  const box = $("rail-activity");
  const job = r.job, q = r.queue || {};
  const waiting = QUEUE.filter(([k]) => q[k] > 0 && !(job && job.kind === k));     // the running one is shown above
  const asked = r.waiting_jobs || [];                                               // asked for, waiting their turn
  const hidden = !job && !waiting.length && !q.wait && !asked.length;
  const shape = JSON.stringify([hidden, job ? job.kind : null, !!(job && job.total)]);
  if (box.dataset.shape !== shape) {
    box.dataset.shape = shape;
    box.textContent = "";
    box.hidden = hidden;
    if (hidden) return;
    // the bar only when there are numbers to show (append() would write a missing one as "null", issue #7)
    box.append(...[el("span", { class: "ra-title", "data-role": "title" }), el("span", { class: "ra-stage", "data-role": "stage" }),
      job && job.total ? el("span", { class: "ra-bar" }, el("span", { "data-role": "pct" })) : null,
      el("span", { class: "ra-queue", "data-role": "queue" }),
      // 清理's processing can run for many minutes: stopped from any page (luoke-mail#32)
      job && job.kind === "clean" ? el("button", { type: "button", class: "link ra-stop", onclick: (e) => {
        e.stopPropagation();
        api("/api/jobs/cancel", { id: box.dataset.job }).catch(() => {});
        e.currentTarget.disabled = true;
        e.currentTarget.textContent = "正在停止…";
      } }, "停止") : null].filter(Boolean));
  }
  box.dataset.job = job ? job.id : "";
  if (hidden) return;
  const page = job ? (KIND[job.kind] || ["", "inbox"])[1] : (waiting[0] || [null, null, "clean"])[2];
  box.onclick = () => nav.go(page);
  box.title = "查看详情";
  box.classList.toggle("running", !!job);
  box.querySelector("[data-role=title]").textContent = job ? `进行中 · ${(KIND[job.kind] || ["任务"])[0]}` : "排队中";
  box.querySelector("[data-role=stage]").textContent = job
    ? `${job.stage || "准备中"}${job.total ? `（${job.done} / ${job.total}）` : ""}`
    : q.wait ? `模型服务内存不足，约 ${Math.max(1, Math.round(q.wait / 60))} 分钟后重试` : "空闲时自动进行";
  const pct = box.querySelector("[data-role=pct]");
  if (pct) pct.style.width = `${Math.round((job.done / Math.max(1, job.total)) * 100)}%`;
  box.querySelector("[data-role=queue]").textContent = [
    asked.length ? `等待中：${asked.map((j) => (KIND[j.kind] || ["任务"])[0]).join(" · ")}` : "",
    waiting.length ? `待处理：${waiting.map(([k, label]) => `${label} ${q[k]} 封`).join(" · ")}` : ""].filter(Boolean).join("\n");
}
