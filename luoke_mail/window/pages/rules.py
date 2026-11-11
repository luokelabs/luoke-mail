"""规则 in the window: the browser page's rules (static/rules.js, view App.rules_view) -- add one, delete one (behind
确认, then 撤销), and for Gmail what a rule would catch as a filter (/api/rules/filter-preview, a job); below them the
handling habits the mail assistant carries out by itself (memory.auto_habits), as the agent's 规则 page showed.

导出到 Gmail (static/gmail.js): the same file, handed to the window's save dialog (§4 saveFile) -- on an older core it
goes into 「下载」 (window._gmail_filters)."""
from __future__ import annotations

from ... import gmailfilters, memory, providers, triage
from ...store import RULE_KINDS
from .. import kit
from .activity import pager
from .common import mailbox_row, running, screen

HABITS_SHOWN = 30


def _rule_text(r: dict) -> str:
    return f"{RULE_KINDS.get(r['kind'], '规则')}「{r['value']}」→ {triage.RULE_ACTIONS.get(r['action'], '处理')}"


def _preview(w, acct: str, rid: int):
    """The latest 预演 of this rule as a Gmail filter (App.start_filter_preview's job), if one was asked for."""
    jobs = [j for j in list(w.app.jobs.values())
            if j.get("kind") == "filter_preview" and j.get("account") == acct and j.get("rule") == rid]
    return jobs[-1] if jobs else None


def _preview_lines(p: kit.Page, job: dict) -> list:
    if job["status"] == "running":
        return [p.text("正在预演…", "caption")]
    if job["status"] == "error":
        return [p.text(job.get("error") or "预演没有完成。", "caption")]
    res = job.get("result")
    if job["status"] != "done" or not res:
        return []
    out = [p.text(res.get("advice") or "", "caption")]
    for way in res.get("ways") or []:
        risk = "（未检查内容）" if not res.get("checked") else \
            (f"，其中 {way['risky']} 封不该一起处理，例如「" + "」「".join(way.get("examples") or []) + "」"
             if way.get("risky") else "，没有发现不该处理的")
        out.append(p.text(f"按{way.get('label')}「{way.get('value')}」：过去一年 {way.get('count')} 封{risk}", "caption"))
    if not res.get("checked"):
        out.append(p.text("模型暂时不可用，只统计了数量，没有检查内容。", "caption"))
    return out


def _rule(w, p: kit.Page, src: str, acct: str, r: dict, gmail: bool) -> str:
    facts = [f"命中 {r.get('hits') or 0} 次"] + (["已导出到 Gmail"] if r.get("exported_at") and gmail else [])
    lines = [p.text(_rule_text(r), "body"), p.text(" · ".join(facts), "caption")]
    job = _preview(w, acct, r["id"]) if gmail else None
    if job:
        lines += _preview_lines(p, job)
    exportable = gmail and r["action"] in gmailfilters.FILTER_LABELS
    lines.append(p.row(
        p.do("预演 Gmail 过滤器", "/api/rules/filter-preview", {"source": src, "id": r["id"]})
        if exportable and not (job and job["status"] == "running") else None,
        p.do("删除", "/api/rules/delete", {"source": src, "id": r["id"]}, quiet=True,
             confirm=f"删除规则：{_rule_text(r)}？")))
    return p.card(*lines)


IMPORT = ("导入方法：在 Gmail 设置中打开「过滤器和屏蔽的地址」，点击页面底部的「导入过滤器」，选择导出的文件，"
          "Gmail 会列出将要创建的过滤器，确认后点击「创建过滤器」。")


def _gmail_note(w, p: kit.Page, src: str, acct: str, rules: list, gmail: bool) -> None:
    """What the browser page's bar above the rules says (static/rules.js gmailBar), and 导出到 Gmail (gmail.js)."""
    exportable = [r for r in rules if r["action"] in gmailfilters.FILTER_LABELS]
    if not exportable:
        return
    if not gmail:
        p.add(p.text("规则仅在螺壳邮件助手分析邮件时生效。如需由邮箱直接拦截发件人，可在「订阅体检」中查看拉黑方法。", "caption"))
        return
    fresh = [r for r in exportable if not r.get("exported_at")]
    p.add(p.text("规则已全部导出到 Gmail。新增的规则需要再次导出。" if not fresh else
                 f"规则仅在螺壳邮件助手分析邮件时生效。将 {len(fresh)} 条规则导出为 Gmail 过滤器后，Gmail 会自动处理之后的邮件。",
                 "caption"))
    chosen = fresh or exportable
    spam = any(r["action"] == "spam" for r in chosen)
    say = (f"将导出 {len(chosen)} 个过滤器，导入后由 Gmail 自动处理之后的邮件。"
           + ("" if fresh else "这些规则以前导出过，再导入一次会在 Gmail 里多出重复的过滤器。")
           + ("Gmail 过滤器不支持移到垃圾邮件，这类规则会改为删除。" if spam else "")
           + ("示例数据，无需导入。" if acct == "demo" else IMPORT + "如需停用，请在 Gmail 的过滤器设置中删除。在此删除规则不会影响 Gmail。"))
    p.add(p.row(p.do(f"导出到 Gmail（{len(fresh)} 条）" if fresh else "重新导出全部", "gmail_filters",
                     {"source": src} if fresh else {"source": src, "include_exported": True}, confirm=say),
                p.link("打开 Gmail 过滤器设置", providers.gmail_url(w.app.address(acct), "settings/filters"))
                if acct != "demo" else None))


def _habits(w, p: kit.Page, acct: str) -> None:
    try:
        habits = memory.auto_habits(w.app.memory_for(acct), acct)
    except Exception:  # noqa: BLE001 -- the memory unreadable just now: the rules still show
        habits = None
    if not p.room(3):
        return
    p.add(p.text("按习惯自动做的", "h5"))
    if habits is None:
        p.add(p.text("暂时读不到记忆。", "caption"))
        return
    if not habits:
        p.add(p.text("还没有。你确认过的处理习惯会列在这里。", "caption"))
        return
    p.add(p.text("螺壳照你确认过的习惯自动处理，每封都列在「今日处理」里，能逐封撤销。", "caption"))
    shown = 0
    for h in habits[:HABITS_SHOWN]:
        if not p.room(2):
            break
        p.add(p.text(h.get("text") or "", "body"))
        shown += 1
    if len(habits) > shown:
        p.add(p.text(f"另有 {len(habits) - shown} 条，在「记忆」里查看。", "caption"))


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，可以在这里告诉螺壳某类邮件以后怎么处理。", "caption"))
        return
    src = w.source(p.page)
    acct = w.app.account(src)
    data = w.app.rules_view(src)
    rules, gmail = data["rules"], bool(data.get("gmail"))
    p.add(p.text("告诉螺壳某类邮件以后怎么处理。", "caption"))
    _gmail_note(w, p, src, acct, rules, gmail)
    kinds = list(RULE_KINDS.items())
    p.add(p.card(p.text("添加规则", "h5"),
                 p.choice("kind", "类型", kinds, kinds[0][0]),
                 p.field("value", "内容（例如 news.shop.example）"),
                 p.choice("action", "处理方式", list(triage.RULE_ACTIONS.items()), "archive"),
                 p.do("添加", "/api/rules", {"source": src, "kind": {"$input": "choices/kind"},
                                            "value": {"$input": "fields/value"}, "action": {"$input": "choices/action"}},
                      inputs=["choices", "fields"], primary=True)))
    p.add(p.text("规则", "h5"))
    if not rules:
        p.add(p.text("还没有规则。在收件箱中更改建议时可以创建规则，也可以在订阅体检中设置。", "caption"))
    p.reserve = 40                                   # the pager and the habits below
    for r in screen(p, view, rules):
        if not p.room(16):
            break
        p.add(_rule(w, p, src, acct, r, gmail))
    p.reserve = 0
    pager(p, view, len(rules))
    _habits(w, p, acct)
