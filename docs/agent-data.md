# 给 agent 的原始数据和动作（2026-11-13，luoke-agent 核过）

顶层 ADR 0002：螺壳主窗口里邮件助手的页面由 luoke-agent 拼，邮件助手只给原始数据和动作。本文件是两边商定的接口。
luoke-agent 照效果图核过以后，邮件助手照这里实现；改了这里，也同步注册页。

## 约定

- 清单 `capabilities.data` 列出已经给了的几样（`["accounts", "today", "mail"]`……）。核心看到列了一页要的，就自己拼这一页；没列的照旧读 `page/<id>`。所以一页一页上。
- 都是 `resources/read` 的只读 JSON 资源，名字 `data/<名字>`，参数照工具协议 §4 pageArgs 的写法放在查询串里（`data/today?account=me@example.com`）。
- 不带 A2UI，不带拼好的句子。模型写的原文（摘要、对方要什么、为什么挑）原样给，字段名以 `_text` 结尾，页面上怎么用由 agent 定。
- 时间一律 `"YYYY-MM-DD HH:MM:SS"`，本机时间，不带时区（和数据库一样）。没有就是 `null`。
- 代号（动作、分类、状态）用英文小写，列在本文件里；以后加新的代号，agent 不认就当 `other`。
- `account` 是邮箱的 id：小写的完整地址。示例邮件是 `demo`。不给 `account` 时用第一个邮箱；`account=all` 是几个邮箱合起来（`data/today`、`inbox`、`digest`、`clean`、`activity`），每项都带自己的 `account`，按时间排；只讲一个邮箱的（`data/old`、`sender`、`piece`）不认 `all`。
- 列表最多 200 项。能翻页的只有 `data/inbox` 和 `data/activity`：超出的给 `more: true` 和 `cursor`，翻页带 `cursor=`。`data/digest` 只给前 200 条；`data/today`、`data/clean` 不截也不翻页（条数本来就少）。
- 读这些资源不改任何东西。例外只有 `data/piece` 一处，在下面写明。
- 读不出来的（邮箱没连、信不在了）不算协议出错，照样答一段 JSON：`{"error": {"code": "invalid" | "stale", "message": "…"}}`。
- 原文的句子（`problem`、`error`、`text`、`model_note`）是旧界面的，过渡期照旧给；窗口照旁边的代号和数写（顶层 ADR 0002，2026-11-17）。

### 停下的原因

`problem_code`、`error_code` 用同一套代号，下面几处都是：`data/accounts` 每个邮箱的 `error_code`，`data/digest` 的
`reading.problem_code`，`data/old` 的 `judging.problem_code`，`data/job` 的 `error_code`，`data/settings` 的
`more.learn.problem_code` 和 `auto.last.problem_code`，`data/clean` 的 `subscriptions.model_check.problem_code`。
没有原因是 `null`；不认识的代号当 `error`。

- 螺壳和本机模型：`hub_not_running`（螺壳没在运行）、`busy`（模型在忙更急的事）、`paused`（螺壳暂停了后台任务）、
  `interrupted`（让给了用户在等的请求）、`not_ready`（模型在加载）、`memory_full`（内存不够）、`task_unavailable`、
  `unsupported`（这台 Mac 上的螺壳不支持）、`engine_failed`（模型出错）、`input_too_long`（邮件太长）、
  `model_timeout`（模型响应超时）、`model_incomplete`（回答没有完整传回）、`model_unusable`（回答用不了）；
  直连模型服务（开发时）还有 `model_unreachable`、`model_unresponsive`、`model_auth`（要 API Key）、`model_missing`（没有这个模型）、
  `model_error`（其余）。
- 邮箱：`login_refused`（授权码不对、过期或撤销，要重新填）、`missing_secret`（没存授权码）、`keychain_locked`（钥匙串暂时读不了，
  Mac 锁着，下次自己好）、`mail_login_blocked`（Google 拦下了登录，要在浏览器里确认）、`mail_unsafe_login`（网易要求开 IMAP、用授权码）、
  `imap_disabled`（没开 IMAP）、`mail_timeout`（连邮箱超时）、`mail_unreachable`（连不上邮箱服务器）、`mail_tls`（服务器的加密方式不支持）、
  `mail_unresponsive`（邮箱暂时无响应）、`mail_refused`（邮箱没完成这一步）、`no_folder`、`folder_hidden`（QQ 邮箱没开放「我的文件夹」）、
  `label_invalid`、`search_failed`、`no_server`（没填 IMAP 服务器）、`mail_error`（其余）。
- 其余：`timeout`、`network`、`invalid`（请求或设置不允许）、`error`。

## 数据

### `data/accounts`

```json
{"accounts": [{"id": "me@example.com", "address": "Me@Example.com", "name": "工作", "provider": "gmail",
               "own": false, "last_sync": "2026-11-13 09:12:03",
               "status": "ok", "error": null}],
 "demo": true,
 "model": {"status": "downloading", "progress": 0.4, "text": "螺壳正在下载模型（40%）。下载好以后自动开始分析。"},
 "job": {"id": "j17", "kind": "sync", "account": "me@example.com", "stage": "正在分析", "done": 3, "total": 12}}
```

- `job`：正在跑的任务（同步、清理、读订阅……），没有是 `null`。`kind` 是代号，`stage` 是原文。

- `name`：用户起的显示名，没起是 `null`（agent 自己决定退回什么）。
- `provider`：`gmail`、`qq`、`163`、`126`、`custom`（`providers.py` 的 key，各种的说明见 `data/providers`）。
- `own`：设成了「螺壳自己的邮箱」。
- `last_sync`：上次同步成功的时间，后台和手动的都算。
- `status`：
  - `ok`：连着；
  - `needs_auth`：没存授权码，或邮箱拒绝登录，要重新填授权码；
  - `error`：上次连不上，原因不是授权，原文在 `error`。
- `error_code`：`needs_auth`、`error` 时的原因代号（见「停下的原因」），`ok` 是 `null`。
- `demo`：示例邮件还在不在（连好邮箱以后隐藏）。
- `model`：现在为什么分析不了邮件；能分析是 `null`。收件箱、订阅体检、设置都看这个（旧界面的 `model_note`）。
  - `status`：`none`（没选本机模型）；螺壳那边没准备好时照它说的：`absent`（螺壳没在运行）、`error`（连不上螺壳）、
    `downloading`（在下载模型，`progress` 是 0–1）、`needs_download`（还没开始下载，或下载失败）、`paused`（暂时不能运行模型）、
    `unsupported`（这台 Mac 上不能分析）。以后加新的，不认识的当 `unsupported`。
  - `text`：旧界面收件箱顶上那句，原文；`none` 时是 `null`。用户选的是别的模型服务（不经过螺壳）时不查，是 `null`。

### `data/mail?id=`

收件箱点一行打开的那封：`id` 是 `data/inbox` 的 `id`，也认搜索结果的 `<邮箱>|<Message-ID>`。

```json
{"id": 4019, "account": "me@example.com", "from": {"name": "王老师", "address": "wang@uni.example"},
 "to": ["me@example.com"], "cc": [], "date": "2026-11-12 16:20:00", "subject": "答辩时间确认",
 "body_text": "…", "truncated": false, "attachments": ["议程.pdf"], "ask_text": "…", "draft_text": null,
 "suspicious": false, "action": "reply", "category": "work", "due": "2026-11-18",
 "open_url": "https://mail.google.com/…", "ways": ["draft", "archive", "trash", "flag", "skip"],
 "events": [{"index": 0, "title": "去程", "date": "2026-12-20", "start": "08:00", "end": null, "location": null,
             "past": false, "added": true},
            {"index": 1, "title": "回程", "date": "2026-12-22", "start": "15:30", "end": null, "location": "机场",
             "past": false, "added": false}]}
```

- 正文是纯文本，最多 8000 字（`truncated` 说截没截）；只有邮件头的旧邮件现去邮箱读，读不到是 `null`。
- 处理过的信还能打开：分拣过的信连同正文存在本机，归档、删除、移到垃圾邮件以后照样读得到（`ways` 是 `[]`），直到移除这个邮箱或全部清除。
  `open_url` 只在 Gmail 上有：归档的能直接打开；移到已删除、垃圾邮件的，Gmail 网页上可能找不到，邮箱清空以后就没了。
- `ways`：能做的，建议的在前（`mail_act` 的动作，加 `draft` 写草稿、`calendar` 加入日程）；处理过的是 `[]`。
- `events`：信里的日程，按模型列的顺序，最多 4 个；`index` 交给 `mail_calendar`。`past`：已经过了；`added`：已经交给过「日历」（同一天同一时间、同一个发件域名的）。没有是 `[]`。
- `to`、`cc`、`attachments`：11-15 以前一直是 `[]`（读错了字段），现在有值。

### `data/today?account=`

```json
{"day": "2026-11-13",
 "handled": [{"log_id": 812, "at": "2026-11-13 08:40:11", "by": "habit", "op": "archive", "count": 6,
              "senders": [{"name": "某某商城", "address": "news@shop.example"}], "categories": ["promotion"],
              "mails": [{"id": 4030, "account": "me@example.com", "from": {"name": "某某商城", "address": "news@shop.example"},
                         "subject": "周末促销", "date": "2026-11-13 07:58:00"}],
              "more": 0, "label": null, "undoable": true}],
 "picked": 3,
 "decide": [{"id": 4021, "account": "me@example.com", "from": {"name": "李律师", "address": "li@law.example"},
             "subject": "合同扫描件", "summary_text": "律师发来合同扫描件", "category": "document",
             "suspicious": false, "due": null, "late": null,
             "ways": ["flag", "archive", "skip"], "suggested": "flag"}],
 "replies": 4}
```

- `handled`：今天替用户做的，一次处理一项（一条处理记录）。
  - `by`：`habit`（按记忆里的习惯）、`rule`（按规则）、`own`（螺壳自己的邮箱）、`read`（分析后标为已读）。用户自己点的不在这里，在 `data/activity`。
  - `op`：`archive`、`trash`、`read`、`label`、`flag`……
  - `senders` 最多 5 个，`categories` 是分拣的分类代号（`triage.CATEGORIES`）。
  - `mails`：这一步动了哪几封，新的在前，最多 20 封，每封 `{id, account, from, subject, date}`；`more` 是没列出的封数（没有是 `0`）。
    点一封用 `data/mail?id=` 打开，见下面「处理过的信还能打开」。
  - `label`：加标签时的标签名。
- `picked`：今天挑进精选的条数。现在没有「挑出的时间」，按「今天读出来、进了本周精选」算。
- `decide`：拿不准、要用户定的信。可疑的在前，然后是快到期、已过期的。
  - `ways`：可选的处理方式，动作代号见「动作」一节的 `mail_act`，另有 `calendar`（加入日程）。
  - `suggested` 是建议的那个。
  - `late`：`overdue`、`due`、`old` 或 `null`。
- `replies`：等回复的封数（这个邮箱所有的，不只今天的）。

### `data/inbox?account=&list=reply|needs|suspicious|direct|archived|all`、`data/inbox?plan=&part=`

```json
{"list": "reply",
 "items": [{"id": 4019, "account": "me@example.com", "from": {"name": "王老师", "address": "wang@uni.example"},
            "subject": "答辩时间确认", "ask_text": "王老师问答辩时间，周五前回", "date": "2026-11-12 16:20:00",
            "due": "2026-11-18", "overdue": false, "due_soon": true, "kind": "reply", "action": "reply", "priority": "high",
            "suspicious": false, "flagged": false, "draft": false, "open_url": "https://mail.google.com/…"}],
 "counts": {"reply": 4, "needs": 7, "suspicious": 1, "direct": 12, "archived": 1, "unsorted": 2},
 "more": false}
```

- `list`：
  - `reply`：等回复；
  - `needs`：要用户处理的，包括回复、日程、跟进、可疑；
  - `suspicious`：可疑；
  - `direct`：建议归档、删除的；
  - `archived`：用户最近自己归档了、里面还有要回复或日程的（旧界面的「你归档的邮件里还有要处理的」）；不在收件箱里，不算进 `all`；
  - `all`：收件箱里的全部。
- `kind`：`reply`、`schedule`、`follow_up`、`suspicious`、`direct`、`unsorted`。
- `action`：分拣的建议，`triage.ACTIONS` 的代号。
- `ask_text`：模型写的摘要，说对方要什么。
- `open_url`：Gmail 里这封信的地址，只在 Gmail 上有，别的邮箱是 `null`。
- `due_soon`：快到期（截止在三天内或已过），排在最前，按截止日先后；其余新的在前，时间相同的按邮件编号，每次翻页顺序不变。
- 切换列表只读每行要的字段（不读正文、日程、草稿说明），几千封待处理的邮箱一次也在零点几秒内；`counts` 每次都带全。
- `counts`：每个列表各几封，`unsorted` 是还没分析的。
- `draft`：本机有没有写好的草稿（`data/mail` 的 `draft_text`）。
- `plan`、`part`：一张卡片上点按钮会动的那几封（邮箱卡片「待整理」的「查看邮件」带的就是这两个，`part=tidy`）。
  `part` 是 `todo`（待处理）、`tidy`（待整理）、`sus`（判为可疑），不写就是整张卡片的。答复和平时一样，`list` 是
  `"plan"`，只列还在收件箱里、没处理的那几封，后来又分拣出来的不算；另有
  `"plan": {"id", "part", "state"}`，`state` 是 `open`（卡片还在，点过又撤销的也回到这里）、`handled`（在卡片上点过了）、
  `gone`（卡片已经换成新的、收起了，或者没有这个编号）。不是 `open` 时 `items` 是空的。

### `data/digest?account=&range=week|today|month:<YYYY-MM>|saved|archived`

```json
{"range": "week",
 "items": [{"id": 77, "title": "本地模型的新进展", "source": "周刊", "summary_text": "…", "why_text": "…",
            "url": "https://news.example/a", "host": "news.example", "mail_url": null,
            "date": "2026-11-12 08:00:00", "feedback": null, "opened": false, "picked": true}],
 "counts": {"saved": 2, "archived": 5, "sources": 3},
 "sources": [{"key": "list:weekly.news.example", "account": "me@example.com", "name": "周刊", "origin": "user",
              "added_at": "2026-10-02 09:00:00"}],
 "months": ["2026-10", "2026-09"],
 "reading": {"queued": 12, "read": 40, "on": true, "problem": null, "problem_code": null}}
```

- `feedback`：`useful`（多来点、收藏）、`dismissed`（少来点）或 `null`。
- `picked`：挑进了精选。还没挑的时段按评分排，`picked` 都是 `false`。
- `months`：有精选的往月，给翻往月用。
- `range`：`week` 最近 7 天（默认），`today` 是其中今天读出来的，`month:<YYYY-MM>` 往月，`saved` 收藏，`archived` 看过了移出的。
- 一个邮箱时按挑的顺序；`account=all` 和 `saved`、`archived` 按时间，新的在前。
- `url` 只给 https 的，没有就是 `null`（`host` 也是）；`mail_url` 是 Gmail 里那封订阅邮件。
- `sources`：精选读哪些发件人，加进来的先后；`origin` 是 `user`（用户加的）、`subscriptions`（订阅体检里加的）、`clean`（清理里加的）。移出用 `mail_digest_source`（`on: false`）。

### `data/piece?account=&id=`

打开一条精选：和 `data/digest` 里那一条一样。读它就是用户打开了它，记成「打开过」，见下面「有副作用的那一处」。

### `data/clean?account=`

```json
{"subscriptions": {"checked_at": "2026-11-10 21:00:00", "days": 60, "scanned": 1800,
                   "senders": [{"key": "list:promo.shop.example", "account": "me@example.com", "name": "某某商城",
                              "address": "news@shop.example",
                              "count": 24, "per_week": 2.8, "read": 0, "last": "2026-11-12 10:00:00",
                              "one_click": true, "unsubscribe_url": "https://shop.example/u", "method": "one_click",
                              "mailto": null, "reply_keyword": null, "size": 1200000, "subjects": ["双十一预热", "…"],
                              "suspicious": false, "group": "unsubscribe", "decision": null, "still_sending": false,
                              "rule": null}]},
 "archive": {"scanned_at": "2026-11-09 20:10:00", "days": 30, "judged": 1180, "total": 1211, "judging": false,
             "groups": [{"key": "notice|from:noreply@post.example", "account": "me@example.com", "kind": "notice",
                         "name": "某快递",
                         "address": "noreply@post.example", "count": 41, "size": 820000,
                         "earliest": "2025-03-02 08:00:00", "latest": "2026-05-10 19:00:00"}]}}
```

- `subscriptions`：订阅体检的全部结果，`scanned` 是读了多少封。没体检过是 `null`。
  `model_note`：这次体检本机模型没能看完时的说明（原文，例如「有 3 个发件人未能分析，它们的建议仅依据收信和阅读情况。」），看完了是 `null`。
  `model_check` 是同一件事的数据：`reason`（`no_model` 没选本机模型、`model_error` 模型出错，原因在 `problem_code`、
  `unanswered` 模型答了但没答到这几个）、`unchecked` 几个发件人没看、`of` 一共要看几个；看完了是 `null`（2026-11-17 以前的体检没有这一项）。
  每个发件人：
  - 为什么这样建议（旧的 `why`，这里不给拼好的句子）：`verdict` 是体检的结论，`unsubscribe`（收得多、读得少、能退订）、
    `filter`（收得多、读得少，但没有退订方式）、`never`（发过可疑邮件）、`keep`（常读）、`important`（读得少，但本机模型看了内容，
    建议保留）；读得多少看 `count`、`read`、`days`。`kind` 是本机模型看最近几封主题判的内容：`marketing` 广告促销、`newsletter`
    订阅内容、`notification` 一般通知、`transactional` 账单订单和交易记录、`security` 登录和安全提醒、`personal` 个人或工作往来、
    `other` 其他；没看过是 `null`。`important` 的理由就是 `kind`。
  - `domain`：发件域名，拉黑整个域名时填。
  - `count` 是 `days` 天里收到的封数，`per_week` 由它算出。
  - `read`：读过几封。「多久没打开过」现在没有，只有读过的封数，和最近一封的时间 `last`。
  - `method`：`one_click`、`web`、`body_link`、`mailto`、`reply`。只有 `one_click` 能在螺壳里直接退订；`unsubscribe_url` 只给 https 的。它是给用户在浏览器里打开的（和点邮件里的链接一样），邮件助手自己不去请求，所以不做公网判断；一键退订是邮件助手自己发请求，发之前才判（`netguard`），内网地址不发。
  - `group`：`unsubscribe` 建议退订、`no_way` 无法退订（读得少，但没有退订方式）、`suspicious` 可疑发件人（发过可疑邮件，不给退订）、`handled` 已处理、`keep` 建议保留（常读）。排序也是这个顺序，同组里大的在前。
  - `decision`：用户定了的，`{"status", "at"}`；`status` 是 `unsubscribed`、`unsubscribe_opened`（打开过退订网页）、`keep`、`archive_rule`、`trash_rule`、`spam_rule`、`digest`（加进了精选）。没定是 `null`。
  - `rule`：因这个发件人建的规则 `{"id", "action"}`，没有是 `null`。
  - `still_sending`：退订两天以后还在发信。
  - `size`：这些邮件占多少字节；`subjects`：最近几封的主题（凭证打码）。
  - `mailto`：`method` 是 `mailto` 时退订要发信的地址（`mailto:` 开头，可能带主题）；`reply_keyword`：`method` 是 `reply` 时回信里写的关键词，回给 `address`。用户自己发了以后用 `mail_sender_unsubscribed` 记下。
- 原来的 `unsubscribe`（建议退订、还没定的）11-15 去掉了，就是 `subscriptions` 里 `group` 是 `unsubscribe` 的那些。
- `archive`：收件箱里超过 `days` 天、建议归档、没被「留着别再问」的几组。没读过旧邮件是 `null`。
  - `kind` 是 `triage.OLD_KINDS` 的代号。
  - `earliest`、`latest` 是这一组最早和最晚的一封。
- 没有自动整理（11-13 定）：清理都要用户点，所以这里不给周期和上次时间。

### `data/sender?account=&key=`

订阅体检里的一个发件人（字段同上），加上清掉它的历史邮件要动多少：

```json
{"key": "from:notice@forum.example", "…": "…", "history_state": "ready",
 "history": {"count": 25, "size": 3400000, "purge_days": 30}}
```

- `history` 是去邮箱查的（收件箱和归档，Gmail 是「所有邮件」），只查不动；星标的、按标签留着的、精选留下的、用户自己发的不算。`purge_days`：移到「已删除」后邮箱多少天清空，不知道是 `null`。
- 查邮箱在后台做，读 `data/sender` 最多等 2 秒，其余字段总是有。`history_state`：
  - `ready`：带 `history`；
  - `pending`：邮箱还没答完（比如正忙着读几千封旧邮件），不带 `history`。过一会儿再读；再读不会再去查一次邮箱，查完的那次就带上；
  - `error`：邮箱报错，不带 `history`，给 `history_error_code`（`login_refused`、`network`、`timeout` 等）。
- 查到的封数留 10 分钟（出错的留 30 秒），对这个邮箱的发件人做过操作或撤销以后重新查。
- 点「清理历史邮件」（`mail_sender_history`）之前先读这个，让用户看到封数和大小。

### `data/old?account=&scope=inbox|archive`

清理旧邮件，一个邮箱、一处：`inbox` 是收件箱里超过保留天数的，`archive` 是归档了的。和旧界面的列表一样，每组带点了会怎样、建不建议：

```json
{"account": "me@example.com", "scope": "archive", "scanned_at": "2026-11-09 20:10", "days": null,
 "stale": false, "total": 5200, "judged": 4100, "size": 912000000, "held": 37,
 "judging": {"on": true, "running": false, "problem": null},
 "groups": [{"key": "promotion|from:ad@store.example", "account": "me@example.com", "kind": "promotion",
             "name": "某店", "address": "ad@store.example", "count": 310, "size": 41000000, "unread": 290,
             "earliest": "2023-01-02 08:00", "latest": "2026-08-01 10:00", "subjects": ["…"],
             "action": "trash", "suggested": true, "judged": true, "kept": false}]}
```

- 没读过是 `scanned_at: null`、`groups: []`；读用 `mail_old_scan`（带 `scope`）。`stale`：读完以后邮箱又变了，要再读。
- `action`：勾了以后做什么。收件箱的是 `archive` 或 `trash`（一次性通知、广告、订阅内容，不是和人的往来）；归档的只有 `trash`。用 `mail_old_archive` 或 `mail_old_trash` 执行。
- 三种组：`suggested` 是建议（归档的叫「建议删除」），`judged` 但没建议是「待确认」，`judged: false`（`kind` 是 `pending`）是「未分析」，本机模型还没看到。`kept`：用户说过「留着别再问」（`mail_old_keep`），不再建议。
- `held`：星标的、按标签留着的，从不进组。`judging`：本机模型在不在看（`mail_old_judge` 开停）；`problem` 是它停下的原因（原文），
  `problem_code` 是原因的代号（见「停下的原因」），没出问题都是 `null`。

### `data/job?id=`

动作答复里 `job` 的那个任务，正在跑的或跑完的（进程退出前都在）：

```json
{"id": "a1b2c3", "kind": "clean", "account": "me@example.com", "scope": "inbox", "status": "done",
 "stage": null, "done": 310, "total": 310, "error": null, "error_code": null,
 "result": {"ok": true, "moved": 310, "stuck": 0, "log_id": 5521}}
```

- `status`：`running`、`done`、`cancelled`（停了，做完的照留）、`error`（`error` 是原文，`error_code` 是原因的代号，见「停下的原因」）。跑着时 `result` 是 `null`。
- `kind`：`sync`、`subscriptions`、`old_scan`、`clean`（清理旧邮件）、`filter_preview`……
- `result` 只给数：清理旧邮件的 `moved`、`stuck`、`stopped`，`log_id` 交给 `mail_undo_step` 撤销整次清理。
- `filter_preview` 的 `result`：`{"rule", "checked", "ways": [{"kind", "value", "count", "judged", "risky", "examples"}]}`。`ways` 第一个是这条规则本身，其余是放宽的写法（同域名、同列表）；`count` 过去一年会命中几封，`judged` 其中本机模型看过几封，`risky` 看着不该一起处理的封数，`examples` 是这几封的主题（凭证打码）。`checked: false` 是模型没空看，只有封数。

### `data/providers`

```json
{"providers": [{"key": "gmail", "name": "Gmail", "host": "imap.gmail.com", "port": 993,
                "secret_label": "应用专用密码", "secret_hint": "16 位字母，带不带空格都可以；不是 Google 账号的登录密码",
                "steps": ["打开下方「两步验证」，确认已开启。…"],
                "links": [{"label": "两步验证", "url": "https://myaccount.google.com/signinoptions/twosv", "account": true}],
                "notes": "Gmail 的 IMAP 一直开着，不用另外设置。", "trouble": ["找不到「应用专用密码」：…"],
                "purge_days": 30,
                "block": {"title": "在 Gmail 中屏蔽或过滤", "steps": ["…"],
                          "link": {"label": "打开 Gmail 过滤器设置", "url": "https://mail.google.com/mail/u/0/#settings/filters"}}}]}
```

- 「添加邮箱」用的邮箱类型，`key` 就是 `mail_connect` 的 `provider`。顺序照这里排，最后一种是 `custom`（手动填 IMAP）。
- `secret_label`：密码框上写什么；`secret_hint`：密码框下面那行小字。
- `steps`：怎么拿授权码，一步一行；`links`：步骤里说的「下方」那几个链接。`account: true` 的链接要带上用户填的地址：在 url 上加 `authuser=<地址>`，打开的才是这个地址的账号（浏览器里登录了多个账号时）。地址还没填就不加。
- `notes`：一句补充，可能是 `null`。`trouble`：连不上时可以看的几条，可能是 `null`。
- `host`、`port`：这种邮箱的服务器。`custom` 的 `host` 是空的，要用户填。
- `block`：在这种邮箱里拉黑一个发件人（订阅体检的「去邮箱里拉黑」）：标题、步骤、设置页的链接（可能是 `null`）。要填的地址、域名
  是 `data/clean` 里那个发件人的 `address`、`domain`。发件人退订后还在发信（`still_sending`）时给用户看。
- `purge_days`：移到「已删除」以后邮箱多少天清空，不知道是 `null`。
- 文字和网页版设置里的一样，改的时候两边一起变。

### `data/settings`

```json
{"accounts": "…同 data/accounts…",
 "rules": [{"id": 3, "account": "me@example.com", "kind": "list", "value": "promo.shop.example",
            "who": "某某商城", "action": "archive", "hits": 12, "created_at": "…", "exported_at": null}],
 "habits": [{"note_id": 7, "text": "某某商城的推广邮件都归档"}],
 "auto": {"enabled": true, "every": 30, "intervals": [15, 30, 60, 120],
          "last": {"at": "2026-11-15 08:00", "ok": true, "text": "归档 3 封"}, "next_in_min": 12, "stalled": null,
          "notify": {"authorization": "authorized"}},
 "more": {"mode_write": false, "mark_read": true, "threads": true, "fetch_days": 30, "fetch_limit": 100,
          "signature": "", "power": "full",
          "learn": {"on": true, "stage": "sent", "running": false, "sent_done": 40, "sent_total": 120,
                    "problem": null, "waiting": 2, "learnt": 5},
          "sites": [{"id": 3, "url": "https://blog.example", "title": "", "posts": 0, "problem": true,
                     "error": "打不开（404）", "checked_at": "2026-11-15 07:30:00"}],
          "model": "…"}}
```

- 规则没有「启用 / 停用」，只有有和删掉。`gmail`：这个邮箱是 Gmail，能导出成过滤器。
- `more.power_by_luoke`：功耗由螺壳统一管，这一项不用在这里改。`more.learn`、`more.sites`：从邮件中了解你、你的网站和博客。
- 添加、移除邮箱和清除全部数据还不在这里（见动作表）。
- `habits`：按记忆里的习惯自动做的，原文。
- `auto`：后台自动整理。
  - `last`：上次整理的时间、成没成（`ok`）、结果（`text` 原文，几个邮箱时每个一段）；还没整理过是 `null`。`next_in_min`：几分钟后下一次。
    同样的结果在数里（2026-11-17 起的整理才有）：`new` 新邮件几封、`needs_you` 其中要用户处理的、`auto` 按习惯自动处理的、
    `unanalyzed` 没分析成的，几个邮箱时是加起来的；`accounts` 是每个邮箱各自的一份（`account`、`at`、`ok`、这四个数，
    加 `unanalyzed_why`：`hub_not_running` 螺壳没在运行、`model_not_running` 模型服务没在运行、`model_error` 模型出错；
    `cut_at` / `cut_of`：读新邮件时连接中断，读到第几封、一共几封，已读到的已保存；`problem_code`：连不上邮箱时的原因，见「停下的原因」）。
  - `stalled`：开着却没在跑（旧界面的「未运行」）：`removed` 登录项被移除了、`moved` 程序的位置变了；正常是 `null`。修法是再开一次：
    `mail_settings` 的 `auto_enabled` 设 `true`，会重新添加登录项。
- `more.learn`：从邮件中了解你。`sent_done` / `sent_total`：发件箱读到第几封、一共几封；`stage` 是下一步读什么（`sent` 发件箱、
  `sent_check` 看有没有新发的、`received` 收件，`null` 是读完了或关着）；`running` 正在读；`problem` 上次出错的原文，`problem_code` 是原因的代号（见「停下的原因」），没有都是 `null`；
  `waiting` 等用户确认的、`learnt` 已经记下的条数。只看第一个邮箱。
- `more.sites`：`problem` 上次打不开，`error` 是原因的原文，`checked_at` 上次去读的时间（没读过是 `null`）。
- `more`：现在「更多设置」里的各项，原样给值。

### `data/activity?account=&cursor=`

```json
{"items": [{"log_id": 812, "account": "me@example.com", "at": "2026-11-13 08:40:11", "by": "habit", "op": "archive",
            "count": 6, "senders": [{"name": "某某商城", "address": "news@shop.example"}], "categories": ["promotion"],
            "mails": [{"id": 4030, "…": "…"}], "more": 0,
            "label": null, "ok": true, "undoable": true, "undone": false, "mail": null}],
 "more": true, "cursor": "812"}
```

- 一次处理一项，新的在前。
- `by`：`user`、`habit`、`rule`、`own`、`read`、`plan`（照卡片上的方案点的）。
- `mails`、`more`：同 `data/today` 的 `handled`。只列分拣过的信（收件箱里的）；清理旧邮件、清历史邮件、退订这些步骤动的是
  只有邮件头的旧邮件或整个发件人，`mails` 是 `[]`，封数看 `count`。
- 只有一封时带 `mail`（主题、发件人，记录里存的原样），不然是 `null`。和 `mails` 重了，留着给旧的写法。
- `op`：除了 `mail_act` 的那些，还有 `draft`、`clean`（清理旧邮件）、`unsubscribe`、`rule`、`sender`（订阅体检里的决定）、`label`……
- 翻页：`cursor` 是这一页最后一项的 `log_id`，下一页带 `cursor=` 读比它早的。

### `summary`（侧栏的概览）

不是 `data/` 下的，是原来就有的资源 `summary`：

```json
{"count": 4, "text": "3 封待处理，1 封可疑，3 封待整理", "connected": true, "needs": 3, "suspicious": 1, "tidy": 3, "auto_today": 0}
```

- `count`：要用户答的，`needs` 加 `suspicious`；`tidy`（建议归档、删除的）和 `auto_today`（今天按习惯自动处理的）只在那一行里说，不算进 `count`。
- `connected: false`：一个邮箱都没连，几个数都是 0。
- `text` 是旧的那一行（过渡期照旧给）；窗口照这几个数写（2026-11-17 起）。

### 有副作用的那一处

读 `data/piece` 把这一条记成「打开过」，记进用户的记忆（一条活动），同一条只记一次。agent 打开一条精选的详情时读它；读 `data/digest` 不记。agent 读页是只读的，所以记在读上（11-15，luoke-agent 提）；`mail_digest_open` 做同样的事，留给用户点按的地方。

## 动作

都是工具（`tools/call`）。写入的要用户点按（工具协议 §5 的 `ai.luoke/user-action`），在清单 `capabilities.actions` 里登记。

| 名字 | 参数 | 做什么 | 可逆 |
|---|---|---|---|
| `mail_act` | `account, ids[], op` | `op`：`archive` 归档、`trash` 移到已删除、`spam` 移到垃圾邮件、`flag` 加星标、`read` 标已读、`unread` 标未读、`skip` 留在收件箱、`complete` 完成并归档 | 可撤销（`mail_undo_step`）。`trash`、`spam` 在邮箱清空前可恢复；一次多封的 `trash`、`spam` 要先开处理模式 |
| `mail_undo_step` | `account, log_id` | 撤销 `data/today`、`data/activity` 里的一项 | 撤销本身不再撤销 |
| `mail_apply_plan` / `mail_undo` | 照旧 | 卡片上的方案、撤销 | 可撤销 |
| `mail_plan` | `ids[]` | 给选中的信出方案（只读），再用 `mail_apply_plan` 执行 | — |
| `mail_calendar` | `account, id, index?` | 把信里的日程交给「日历」，用户在日历里确认 | 日历里删 |
| `mail_draft` | `account, id` | 用本机模型写回复草稿，存在本机；答复带 `draft_text`，草稿里有原信没有的联系方式时带 `draft_note_text` 提醒 | 可再写 |
| `mail_draft_save` | `account, id, text` | 草稿存进邮箱的「草稿」 | 邮箱里删 |
| `mail_change` | `account, ids[], action, remember?` | 改建议；`remember`：`sender`、`domain`、`list` 就同时建一条规则 | 可撤销 |
| `mail_sync` | `account` | 同步新邮件（后台跑，进度见 `data/accounts` 的 `job`、`data/job`）。`account=all` 每个邮箱各起一个，排着队跑；答复带 `jobs`（起了的）和 `failed`（起不了的，`account`、`message`），一个都起不了才出错 | — |
| `mail_job_stop` | `id` | 停下正在跑的任务，做完的照留 | — |
| `mail_subscriptions_check` | `account, days?` | 检查订阅（后台跑，`days` 默认 60）。认 `account=all`，同 `mail_sync` | — |
| `mail_unsubscribe` | `account, key` 或 `account, keys[]` | 一键退订（只对 `one_click`）。`keys`：用户在确认里看到名字、一次点按同意的几个。答复带 `done`、`web`（对方不接受一键退订，带它的退订网页 `open_url`）、`failed` | **不可逆** |
| `mail_sender_keep` | `account, key` | 订阅体检里「留着别再问」 | 可撤销 |
| `mail_sender_rule` | `account, key, action` | 这个发件人以后都 `archive`、`trash`、`spam` | 可撤销 |
| `mail_sender_history` | `account, key` | 清掉这个发件人的历史邮件（移到已删除）；先读 `data/sender` 给用户看封数和大小 | 邮箱清空前可撤销；要处理模式 |
| `mail_sender_reset` | `account, key` | 撤回订阅体检里的决定，移回建议处理：因它建的规则删掉，加进的精选来源移出 | 可撤销 |
| `mail_sender_unsubscribed` | `account, key` | 用户自己退订了（发了 `mailto`、回了 `reply_keyword`、在网页上退了），记成已退订；之后还在发信会标 `still_sending` | 可撤销 |
| `mail_old_scan` | `account, scope?` | 读旧邮件（只读邮件头，后台跑）：`scope` 是 `inbox`（默认）或 `archive`。认 `account=all`，同 `mail_sync` | — |
| `mail_old_judge` | `account, on, scope?` | 让本机模型接着看旧邮件，或暂停 | — |
| `mail_old_archive` | `account, keys[], count` | 按组归档收件箱里的旧邮件（`action` 是 `archive` 的组；`count` 是用户看到的封数，对不上就拒；后台跑，答复带 `job`，结果和撤销用的 `log_id` 在 `data/job`） | 可撤销 |
| `mail_old_trash` | `account, scope, keys[], count` | 按组把旧邮件移到已删除（`action` 是 `trash` 的组，收件箱的或归档的；精选留下过的改加星标、不删）；`count` 同上 | 邮箱清空前可撤销；要处理模式 |
| `mail_old_keep` | `account, key, on, scope?` | 这一组「留着别再问」 | 可撤销 |
| `mail_digest_feedback` | `account, id, value` | `useful` 多来点、`dismissed` 少来点，`null` 取消；记进记忆 | 可撤销 |
| `mail_digest_archive` | `account, id, on` | 看过了，移出精选 | 可撤销 |
| `mail_digest_open` | `account, id` | 记一次打开 | — |
| `mail_digest_reading` | `account, on` | 精选读订阅：`false` 读完手上这一封就停，`true` 接着读 | 可撤销（再点一次） |
| `mail_digest_source` | `account, key, on, name?` | 把一个发件人加进精选来源（`on: true`）或移出；`key` 是 `data/clean` 里发件人的 `key` | 可撤销（再点一次） |
| `mail_rule_add` / `mail_rule_delete` | `account, kind, value, action` / `account, id` | 加、删规则 | 删了可撤销 |
| `mail_gmail_filters` | `account, all?` | 导出 Gmail 过滤器文件（§4 saveFile） | — |
| `mail_set_name` | `account, name` | 改显示名，空的改回默认 | 可改回 |
| `mail_settings` | `key, value`（数可以写成文字，`"30"`） | 改一项：`mode_write`（开之前页上要先说明、用户确认）、`mark_read`、`threads`、`fetch_days`、`fetch_limit`、`signature`、`power`、`auto_enabled`（开之前同样先说明）、`auto_every` | 可改回 |
| `mail_set_own` | `account, own` | 设为（或不再是）螺壳自己的邮箱：那里分拣完直接归档、删除，页上先说明 | 可改回 |
| `mail_learn` | `account, on` | 从邮件中了解你：开或停 | 可改回 |
| `mail_site` | `account, op, url?, id?` | 你的网站和博客：`add` 一个网址，`remove` 一个 | 可再加 |
| `mail_rule_preview` | `account, id` | 预演这条规则做成 Gmail 过滤器会拦下什么（后台跑，答复带 `job`，结果在 `data/job`） | — |
| `mail_connect` | `provider, address, secret?, host?, port?, key?` | 添加邮箱；带 `key` 是改这个邮箱的授权码或地址。`secret` 只能来自窗口的密码框（§4 secureField），只随这一次点按交来，存进钥匙串，不进答复、出错的话、数据、日志和记忆；改的时候不填就用原来的，但 `provider`、`host`、`port` 有一个变了就必须重填，否则出错（`failed`，「换了服务器，请重新填写授权码。」，没有 `saved`：什么都没存），原来的授权码不交给新服务器。改了地址，旧地址在本机的数据一起删掉。地址、服务器、授权码里有换行等控制字符的拒绝。`key` 认配置里的邮箱，钥匙串里授权码丢了的也能重填。存好后试连一次，答复带 `account`、`total`、`unseen`；连不上时出错（`failed`，「已保存，但连不上：…」，structuredContent 里另有 `saved: true` 和 `account`），邮箱留着让用户改 | 可移除 |
| `mail_remove` | `account` | 移除邮箱：授权码从钥匙串删掉，本机关于它的数据（列表、规则、清理、精选、处理记录、交给「日历」的日程文件）删掉；邮箱里的邮件和记忆不动。授权码丢了的邮箱也能移除。窗口要先问、写明是哪个邮箱（§4 confirm）。答复带 `exiting`：最后一个邮箱移除后后台整理关掉，进程在答复后退出 | **不可逆**（邮件不受影响） |
| `mail_forget` | `memory?` | 全部清除：所有邮箱的授权码、列表、设置、规则、记录、日程文件、日志；`memory` 是 `true`（只认布尔值）时连同邮件助手写进记忆的内容（用户勾了才删，记忆是用户的）。窗口要先问。答复带 `exiting`，同上 | **不可逆** |

现有的 `mail_search`、`mail_read`、`mail_show`、`outbox_ack` 照旧。页面层搬完以后去掉：`mail_view`、`mail_do`、`mail_open_page`、`mail_open_settings`、`mail_open`、`mail_disconnect`，以及 outbox 里拼卡片的那层（outbox 改成给原始数据，另议）。
