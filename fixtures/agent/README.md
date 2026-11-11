# 给 agent 的场景样例

邮件助手（luoke-mail）对 agent 那一路（工具协议版本 1：MCP、outbox、A2UI 卡片、基础页、权限）在几种用户处境下**真实**的答复。
都是 `scripts/record_agent_fixtures.py` 在假数据上起真的服务录的，不是手写的：agent 拿它们当样例数据和测试的输入，就是两边之间的
接口说明。代码改了 agent 看到的东西，`--check` 就不过，要重录、提交，并且在 PR 里说清 agent 那边要不要跟着改。

一个场景一个进程：`LUOKE_MAIL_HOME` 指到新建的临时目录，真的 `App` 和 `agent.sock`，请求经那条 socket 发；假邮箱是
`tests/fakes.py` 的 FakeIMAP，假模型是 FakeChat（答复写在录制脚本的 `MAILS` 里，不连真模型），时钟和时区固定。

**本文件也是录的时候生成的**，不要手改；要改说明，改 `scripts/record_agent_fixtures.py` 里的场景。

## 重录、核对

```bash
python3 scripts/record_agent_fixtures.py            # 重录，写进 fixtures/agent/（旁边要有 luoke-mem）
python3 scripts/record_agent_fixtures.py --check    # 重录进临时目录和这里比，不一样就列出差别、退出码 1
python3 scripts/record_agent_fixtures.py --only new-mail --keep   # 只录一个场景，留下假数据看看
```

`tests/test_agent_fixtures.py` 跑 `--check`，所以 `python3 -m unittest discover -s tests` 里就有这一步。

## 每个场景里有什么

一个场景一个文件夹，一个请求一个文件，名字照请求起：

| 文件 | 请求 |
|---|---|
| `manifest.json` | 配件清单 `packaging/accessory.json`（主控经 `GET /accessories` 交给 agent 的那份，`capabilities` 在里面） |
| `scenario.json` | 这个场景：处境、怎么录的（假数据、时钟、时区、每一步）、窗口上应该是什么样 |
| `server-discover.json` | `server/discover` |
| `tools-list.json` | `tools/list`（`annotations.readOnlyHint`、只在 Mac 上的 `_meta.ai.luoke/where`） |
| `resources-list.json` | `resources/list` |
| `read-outbox*.json` | `resources/read` `luoke://luoke-mail/outbox`（agent 每分钟读） |
| `read-summary*.json` | `resources/read` `summary` |
| `read-permissions*.json` | `resources/read` `permissions` |
| `read-page-<页>*.json` | `resources/read` `page/<页>`：今日处理、收件箱、清理、精选、订阅体检、规则、处理记录、设置（`window/pages`） |
| `call-<工具>*.json` | `tools/call`：卡片、页上的点按（带 `ai.luoke/user-action`），或只读的调用 |

带 `-after-…` 的是点完以后再读的。每个文件是 `{"request": …, "reply": …}`：`request` 是 agent 发的那条 JSON-RPC
（`_meta` 里照 agent 带协议版本、`clientInfo`、`clientCapabilities`，点按的再带 `ai.luoke/user-action`），`reply` 是邮件助手的答复。
JSON 按键排序、缩进两格。

**和线上不一样的地方**（重放时要换回去）：

- `resources/read` 的 `contents[0].text` 在线上是一段 JSON **文字**（`mimeType: application/json`），这里解开成了 JSON 值；
  重放时 `json.dumps` 回去。
- 方案编号（`new-mail-…`、`suspicious-…`、`auto-done-…` 后面的 16 位十六进制）按在场景里出现的先后换成 `000000000001`、
  `000000000002`……；同一个场景里前后对得上（outbox 里的方案编号就是点按时发的那个），不同场景之间不对应。
- 版本号换成 `$VERSION`（`server/discover` 的 `serverInfo`、`manifest.json` 的 `version`）：邮件助手几乎每个 PR 升一次版本，
  不换的话每个 PR 都要重录，看不出 agent 看到的东西有没有变。
- 路径：`LUOKE_MAIL_HOME` 是 `~/.luoke-mail`，别的临时目录是 `$TMPDIR`。
- 时钟停在固定的时间（各场景的 `scenario.json`），时区是 Asia/Shanghai：`created` 这类写的是上海的本机时间，不带时区。

## 重放时要知道的

- agent 要问的：`tools/list`（认 `readOnlyHint`）、`luoke://luoke-mail/outbox`、`summary`、`permissions`；打开应用页时读
  `page/<页>`。`server/discover`、`resources/list` 邮件助手都答，agent 现在不一定问。
- 会改动东西的（清单 `actions` 里的那几个）只认带 `ai.luoke/user-action`（`surfaceId` 是方案编号或页 id、`componentId`、`at`）的调用，
  没带的答 `isError: true`、`structuredContent.code: "needs_user_action"`。`outbox_ack` 是只读的记账。
- 按钮的 `event.context` 里的 `{"path": "/choices"}` 换成点的时候卡片数据模型里的值，方案编号这类写死的值照卡片上的。
- `mail_undo` 的 `execution_id` 有三种：方案编号（方案的结果卡片上的）、`auto:<记录>`（自动处理的卡片上的）、`log:<记录>`（「处理记录」页上的）。
- 点按的答复：`content[0].text` 一句话；`structuredContent.card` 是**更新原卡片**的消息（没有 `createSurface`）；`done: true` 表示
  办完了。基础页上点的，答复里的卡片不画，窗口重新读那一页。
- 没录的：`mail_open`、`mail_open_settings`、`mail_open_page`、`mail_show`、`mail_open_calendar`、`mail_disconnect`（都打开真的
  浏览器或「日历」，答一句「已在浏览器里打开……」，没有卡片）；`mail_read`、`mail_plan`（对话里用的；`mail_plan` 答一张新卡片）。
- 「今天」里分在哪一段，下面照 luoke-agent 现在的分段规则（`luoke_agent/agent.py` 的 `_section`）写的，不是邮件助手定的。

## 要对齐的

- **`count` 和 outbox 可能对不上**：撤销以后邮件回到收件箱，概览和「邮箱」页照实数着它们；同一批邮件用户答过了，不再出方案，outbox 里
  没有这件（`new-mail` 的 `-after-undo`）。有新邮件来才一起出新方案。
- **搜索的结果窗口还画不出来**：`mail_search` 答 `hits`、`card` 是空的（工具协议 §1b，`card` 只是后备）；agent 的 `_search_one`
  没有卡片就不画，要等窗口的 `HitsList`。
- **`created` 没带时区**：邮件的写收到的时间，`needs-access`、`auto-done` 和没有收到时间的写这一件出来的时间，都是
  `2026-10-20T03:42:00` 这样。工具协议写「不带时区的按本机时间」，邮件助手一直这样写。
- 邮件助手这边要改的，改了样例跟着变，agent 别照着它排版：`auto-done` 的摘要和每组的说明都写「按你的习惯自动归档 1 封」，
  分不出是哪条习惯（工具协议 §8a 要说按了哪几条），只有下面一行的主题看得出来。

## 场景

## `no-mailbox`：还没连邮箱

刚装好，还没连邮箱。邮件助手在运行，要等用户在自己的页面里填授权码。

**怎么录的**：LUOKE_MAIL_HOME 指到新建的临时目录；假邮箱是 tests/fakes.py 的 FakeIMAP（gmail），假模型是 FakeChat，分拣和草稿的答复写在 scripts/record_agent_fixtures.py 的 `MAILS` 里。

**邮件助手报的**

- 概览（`summary`）：count 0，「还没连邮箱」
- 权限（`permissions`）：`mailbox` ask
- outbox：1 件
  - `needs-access/mailbox`：`needs-access`、`low` → 要你决定（decide）
    - 标题：还没连邮箱
    - 摘要：连上邮箱，新邮件就能在这里分拣好。授权码在邮件助手自己的页面里填，只存在这台 Mac 的钥匙串里。
    - 卡片（surface `needs-mailbox`）：
      - **还没连邮箱**
      - 连上邮箱，新邮件就能在这里分拣好。授权码在邮件助手自己的页面里填，只存在这台 Mac 的钥匙串里。
      - 按钮：「去设置」→ `agent.open_page`
- 基础页 `page/today`：
  - **还没有连接邮箱。**
  - 在「设置」里添加邮箱以后，这里会列出螺壳每天处理过的邮件。
- 基础页 `page/inbox`：
  - **还没有连接邮箱。**
  - 在「设置」里添加邮箱以后，这里会列出要你处理的邮件。
- 基础页 `page/clean`：
  - **还没有连接邮箱。**
  - 在「设置」里添加邮箱以后，这里会列出可以清理的旧邮件。
- 基础页 `page/digest`：
  - **还没有连接邮箱。**
  - 在「设置」里添加邮箱以后，这里会列出订阅邮件中值得一读的内容。
- 基础页 `page/subs`：
  - **还没有连接邮箱。**
  - 在「设置」里添加邮箱以后，可以在这里查看哪些订阅可以退订。
- 基础页 `page/rules`：
  - **还没有连接邮箱。**
  - 在「设置」里添加邮箱以后，可以在这里告诉螺壳某类邮件以后怎么处理。
- 基础页 `page/activity`：
  - **还没有连接邮箱。**
  - 在「设置」里添加邮箱以后，螺壳对邮箱做过的每一步都会记在这里。
- 基础页 `page/settings`：
  - **邮箱**
  - 每个邮箱的规则、清理和精选分开管理。
  - 按钮：「连接邮箱…」→ `mail_view`
  - **分析**
  - 同步范围：同步和整理只看这个范围内的邮件
  - 按钮：「保存范围」→ `mail_do`
  - 回复署名：写在回复草稿的末尾
  - 按钮：「保存署名」→ `mail_do`
  - **后台自动整理**
  - 连接邮箱后可用。
  - **已读**
  - 分析过的邮件标为已读（已开启）
  - 按钮：「关闭」→ `mail_do`
  - **往来邮件**
  - 提醒在等对方回复的邮件（已开启）
  - 按钮：「关闭」→ `mail_do`
  - 会读「已发送」里最近一个月的邮件，只在本机分析。
  - **处理模式**
  - 未开启
  - 连接邮箱后可开启。
  - **本机模型**
  - test-model · 本机模型服务
  - **功耗**
  - 全速（当前）
  - 按钮：「均衡」→ `mail_do`
  - 按钮：「安静」→ `mail_do`
  - 模型连续运行，速度最快，风扇可能全速转。
  - **记忆**
  - 关于你 0 条 · 关注的话题 0 个
  - 从邮件中了解你
  - 不用自己填写：根据你发出的邮件、收到的往来邮件和你的处理习惯，记下关于你的信息。
  - 按钮：「开启」→ `mail_do`
  - 你的网站和博客
  - 螺壳会读取这些网站上的文章，用来了解你关注的话题。只读取你填写的网址。
  - 按钮：「添加」→ `mail_do`
  - **本机数据**
  - 账号、授权码和分析结果只保存在本机。
  - 注意：在此系统上，授权码以明文保存在本机文件中（仅你的账户可读）。
  - 不勾选，记忆留给其他螺壳工具用。勾选也只删邮件助手自己记下和推测的；你确认过、改过或自己写的会保留。
  - 按钮：「全部清除」→ `mail_do`

**窗口上**

- 「今天 · 要你决定」里一张「还没连邮箱」，按钮「去设置」（`mail_open_page`，`{"page": "accounts"}`，打开邮件助手自己的页面）。
- 应用页的 `needs` 里有 `mailbox`；权限页这一行 `ask`，能「打开」，不能「移除」。
- 概览写「还没连邮箱」，计数 0；三页都是一句话加「去设置」。

**文件**（按请求的先后）

| 文件 | 请求 | 什么时候 |
|---|---|---|
| `server-discover.json` | server/discover | 2026-10-20T01:30:00Z：server/discover |
| `tools-list.json` | tools/list | 2026-10-20T01:30:00Z：列工具（agent 认 readOnlyHint） |
| `resources-list.json` | resources/list | 2026-10-20T01:30:00Z：列资源 |
| `read-outbox.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:30:00Z：读 outbox（每分钟一次） |
| `read-summary.json` | resources/read `summary` | 2026-10-20T01:30:00Z：读概览（侧栏的计数和一句话） |
| `read-permissions.json` | resources/read `permissions` | 2026-10-20T01:30:00Z：读权限的状态 |
| `read-page-today.json` | resources/read `page/today` | 2026-10-20T01:30:00Z：读基础页 page/today |
| `read-page-inbox.json` | resources/read `page/inbox` | 2026-10-20T01:30:00Z：读基础页 page/inbox |
| `read-page-clean.json` | resources/read `page/clean` | 2026-10-20T01:30:00Z：读基础页 page/clean |
| `read-page-digest.json` | resources/read `page/digest` | 2026-10-20T01:30:00Z：读基础页 page/digest |
| `read-page-subs.json` | resources/read `page/subs` | 2026-10-20T01:30:00Z：读基础页 page/subs |
| `read-page-rules.json` | resources/read `page/rules` | 2026-10-20T01:30:00Z：读基础页 page/rules |
| `read-page-activity.json` | resources/read `page/activity` | 2026-10-20T01:30:00Z：读基础页 page/activity |
| `read-page-settings.json` | resources/read `page/settings` | 2026-10-20T01:30:00Z：读基础页 page/settings |

## `new-mail`：新邮件分拣好了

夜里到了五封：一封要回、一封周会、一封律师发来的合同，一封周刊打算归档，一封促销打算删除。早上分拣好了。

**怎么录的**：LUOKE_MAIL_HOME 指到新建的临时目录；假邮箱是 tests/fakes.py 的 FakeIMAP（gmail），假模型是 FakeChat，分拣和草稿的答复写在 scripts/record_agent_fixtures.py 的 `MAILS` 里；连上了假邮箱 me@example.com（Gmail，处理模式开着）；收件箱里放了：答辩时间确认、项目周会、合同扫描件、本周精选、双十一预告；2026-10-20T01:30:00Z 同步了一次（App.sync_mailbox：真的读信、分拣、做习惯里的事）。

**邮件助手报的**

- 概览（`summary`）：count 3，「3 封要你处理，2 封可以整理」
- 权限（`permissions`）：`mailbox` granted
- outbox：2 件
  - `new-mail/new-mail-000000000001`：`new-mail`、`normal` → 要你决定（decide）
    - 标题：1 封邮件要你处理
    - 摘要：王老师：王老师问答辩时间，周五前回。
    - 卡片（surface `new-mail-000000000001`）：
      - **1 封要处理**
      - me@example.com
      - 王老师
      - 答辩时间确认
      - 10-23 截止
      - 王老师问答辩时间，周五前回
      - 选项：王老师：答辩时间确认（draft/flag/archive/keep，默认 draft）
      - 按钮：「写草稿 1」→ `mail_apply_plan`、「查看全部 3 封」→ `agent.open_page`
  - `mailbox/mailbox-000000000002-3a316d6d`：`mailbox`、`low` → 顺便看看（later）
    - 标题：2 封待处理，2 封可以整理
    - 摘要：项目组：项目周会；李律师：合同扫描件；整理：归档 1 · 删除 1，多是某某商城、周刊。
    - 卡片（surface `mailbox-000000000002-3a316d6d`）：
      - **邮箱**
      - me@example.com
      - 待处理
      - 项目组
      - 项目周会
      - 周四下午三点的周会
      - 选项：项目组：项目周会（flag/archive/keep，默认 flag）
      - 按钮：「10-22 15:00」→ `agent.open_url`
      - 李律师
      - 合同扫描件
      - 律师发来合同扫描件
      - 选项：李律师：合同扫描件（flag/archive/keep，默认 flag）
      - 按钮：「加星标 2」→ `mail_apply_plan`、「查看全部 3 封」→ `agent.open_page`
      - 可以整理
      - 多是某某商城、周刊。
      - 按钮：「归档 1 · 删除 1」→ `mail_apply_plan`、「先不动」→ `mail_apply_plan`
- 基础页 `page/today`：
  - 螺壳今天处理了哪些邮件。不对的改一下，它会记住。
  - 今天
  - 按钮：「昨天」→ `mail_view`
  - 按钮：「10/18」→ `mail_view`
  - 按钮：「10/17」→ `mail_view`
  - 按钮：「10/16」→ `mail_view`
  - 按钮：「10/15」→ `mail_view`
  - 按钮：「10/14」→ `mail_view`
  - 全部 5
  - 按钮：「等你处理 3」→ `mail_view`
  - 按钮：「留在收件箱 2」→ `mail_view`
  - 等你处理
  - 3
  - 请到「收件箱」处理。
  - 已读，留在收件箱
  - 2
- 基础页 `page/inbox`：
  - 回复 1、加入日程 1、加星标 1、归档 1、删除 1
  - 按钮：「同步新邮件」→ `mail_do`
  - 快到期
  - 需要你处理
  - 按钮：「可以直接处理」→ `mail_view`
  - 按钮：「判为可疑」→ `mail_view`
  - 按钮：「全部」→ `mail_view`
- 基础页 `page/clean`：
  - 收件箱中的旧邮件
  - 按钮：「已归档的邮件」→ `mail_view`
  - 收件箱里超过 30 天的邮件。加了星标或「收藏」「重要」这类标签的不会动。
  - 还没有读取这些邮件。
  - 按钮：「读取邮件」→ `mail_do`
- 基础页 `page/digest`：
  - 暂时按热度挑选，读得多了会按你的口味挑。在「设置」里添加你的网站或博客。
  - 精选
  - 按钮：「收藏（0）」→ `mail_view`
  - 按钮：「已归档（0）」→ `mail_view`
  - 按钮：「精选来源（0）」→ `mail_view`
  - 还没有精选来源。
  - 在「订阅体检」里把发件人加入精选，或在「清理」的「订阅内容」一栏选择「先读一遍」。
- 基础页 `page/subs`：
  - 选项：体检范围（30/60/90/180/365，默认 60）
  - 按钮：「开始体检」→ `mail_do`
  - 还没体检过。
  - 设好天数，点「开始体检」。
- 基础页 `page/rules`：
  - 告诉螺壳某类邮件以后怎么处理。
  - 规则
  - 还没有规则。在收件箱中更改建议时可以创建规则，也可以在订阅体检中设置。
  - 按习惯自动做的
  - 还没有。你确认过的处理习惯会列在这里。
- 基础页 `page/activity`：
  - 螺壳对邮箱做过的每一步，退订以外都能撤销。
- 基础页 `page/settings`：
  - **邮箱**
  - 推荐给螺壳一个专用邮箱，把订阅、通知转发过去，由它自动整理。
  - 按钮：「添加邮箱…」→ `mail_view`
  - **分析**
  - 同步范围：同步和整理只看这个范围内的邮件
  - 按钮：「保存范围」→ `mail_do`
  - 回复署名：写在回复草稿的末尾
  - 按钮：「保存署名」→ `mail_do`
  - **后台自动整理**
  - 已关闭：开启后定时整理，有要你处理的邮件时发通知。
  - 选项：整理间隔（15/30/60/120，默认 30）
  - 按钮：「开启」→ `mail_do`
  - **已读**
  - 分析过的邮件标为已读（已开启）
  - 按钮：「关闭」→ `mail_do`
  - **往来邮件**
  - 提醒在等对方回复的邮件（已开启）
  - 按钮：「关闭」→ `mail_do`
  - 会读「已发送」里最近一个月的邮件，只在本机分析。
  - **处理模式**
  - 已开启：批量删除、移到垃圾邮件时，每次都需要你确认。
  - 按钮：「关闭」→ `mail_do`
  - **本机模型**
  - test-model · 本机模型服务
  - **功耗**
  - 全速（当前）
  - 按钮：「均衡」→ `mail_do`
  - 按钮：「安静」→ `mail_do`
  - 模型连续运行，速度最快，风扇可能全速转。
  - **记忆**
  - 关于你 0 条 · 关注的话题 0 个
  - 从邮件中了解你
  - 已开启，下次空闲时继续
  - 按钮：「暂停」→ `mail_do`
  - 你的网站和博客
  - 螺壳会读取这些网站上的文章，用来了解你关注的话题。只读取你填写的网址。
  - 按钮：「添加」→ `mail_do`
  - **本机数据**
  - 账号、授权码和分析结果只保存在本机。
  - 注意：在此系统上，授权码以明文保存在本机文件中（仅你的账户可读）。
  - 不勾选，记忆留给其他螺壳工具用。勾选也只删邮件助手自己记下和推测的；你确认过、改过或自己写的会保留。
  - 按钮：「全部清除」→ `mail_do`

**窗口上**

- 两件（luoke-mail#81，「今天」的新模板）：`new-mail`、`normal`，在「要你决定」：只放优先级高、没过期限的（这里是王老师那封），标题「1 封要处理」后面一个邮箱地址的标签，一行发件人（加粗）、主题和截止的标签，选择框带图标；主按钮写清做什么（「写草稿 1」），旁边「查看全部 3 封」（`agent.open_page` 到 `inbox`，`args` 是 `box` 和 `list: needs`，收件箱切到这个邮箱、开着「需要你处理」）。
- `mailbox`、`low`，在「顺便看看」：一个邮箱一张「邮箱」，分块：「待处理」（周会、合同，每封一个选择框；周会那行右边一个「10-22 15:00」加入日程的按钮，`agent.open_url`，悬停提示「加入日程」），「可以整理」（一句「多是…」，按钮「归档 1 · 删除 1」「先不动」）。有较早的可疑邮件时还有「判为可疑」一块，`collapsed` 先收起。每块的按钮上下文带 `part`，只做这一块。
- 点「写草稿 1」把 `/choices` 换成当时的选项转给 `mail_apply_plan`：答复带 `done: true`，卡片换成结果和 [撤销]（结果只有一种时 root 带 `group`，「已经替你做了」里同一个邮箱、同一种结果合成一行），这一件从 outbox 撤下。
- 点 [撤销]（`mail_undo`，参数是方案编号）：邮件回到收件箱，答复带 `done: true`。同一批不再出方案（用户答过了），所以概览还数着它们、outbox 里没有，见下面「要对齐的」。
- 「邮箱」卡片上点「归档 1 · 删除 1」（上下文 `{"plan_id", "part": "tidy", "choices": {"tidy": ["suggested"]}}`）：只整理这一块，卡片换成结果和 [撤销]；下一次读 outbox，「邮箱」卡片重出，只剩「待处理」。

**文件**（按请求的先后）

| 文件 | 请求 | 什么时候 |
|---|---|---|
| `server-discover.json` | server/discover | 2026-10-20T01:30:00Z：server/discover |
| `tools-list.json` | tools/list | 2026-10-20T01:30:00Z：列工具（agent 认 readOnlyHint） |
| `resources-list.json` | resources/list | 2026-10-20T01:30:00Z：列资源 |
| `read-outbox.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:30:00Z：读 outbox（每分钟一次） |
| `read-summary.json` | resources/read `summary` | 2026-10-20T01:30:00Z：读概览（侧栏的计数和一句话） |
| `read-permissions.json` | resources/read `permissions` | 2026-10-20T01:30:00Z：读权限的状态 |
| `read-page-today.json` | resources/read `page/today` | 2026-10-20T01:30:00Z：读基础页 page/today |
| `read-page-inbox.json` | resources/read `page/inbox` | 2026-10-20T01:30:00Z：读基础页 page/inbox |
| `read-page-clean.json` | resources/read `page/clean` | 2026-10-20T01:30:00Z：读基础页 page/clean |
| `read-page-digest.json` | resources/read `page/digest` | 2026-10-20T01:30:00Z：读基础页 page/digest |
| `read-page-subs.json` | resources/read `page/subs` | 2026-10-20T01:30:00Z：读基础页 page/subs |
| `read-page-rules.json` | resources/read `page/rules` | 2026-10-20T01:30:00Z：读基础页 page/rules |
| `read-page-activity.json` | resources/read `page/activity` | 2026-10-20T01:30:00Z：读基础页 page/activity |
| `read-page-settings.json` | resources/read `page/settings` | 2026-10-20T01:30:00Z：读基础页 page/settings |
| `call-mail_apply_plan.json` | tools/call `mail_apply_plan`（带 user-action） | 2026-10-20T01:35:00Z：用户照默认的选项点「要你决定」那张的「写草稿 1」 |
| `read-outbox-after-apply.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:35:00Z：点完再读 outbox |
| `read-summary-after-apply.json` | resources/read `summary` | 2026-10-20T01:35:00Z：点完再读概览 |
| `read-page-today-after-apply.json` | resources/read `page/today` | 2026-10-20T01:35:00Z：读基础页 page/today |
| `read-page-inbox-after-apply.json` | resources/read `page/inbox` | 2026-10-20T01:35:00Z：读基础页 page/inbox |
| `read-page-clean-after-apply.json` | resources/read `page/clean` | 2026-10-20T01:35:00Z：读基础页 page/clean |
| `read-page-digest-after-apply.json` | resources/read `page/digest` | 2026-10-20T01:35:00Z：读基础页 page/digest |
| `read-page-subs-after-apply.json` | resources/read `page/subs` | 2026-10-20T01:35:00Z：读基础页 page/subs |
| `read-page-rules-after-apply.json` | resources/read `page/rules` | 2026-10-20T01:35:00Z：读基础页 page/rules |
| `read-page-activity-after-apply.json` | resources/read `page/activity` | 2026-10-20T01:35:00Z：读基础页 page/activity |
| `read-page-settings-after-apply.json` | resources/read `page/settings` | 2026-10-20T01:35:00Z：读基础页 page/settings |
| `call-mail_undo.json` | tools/call `mail_undo`（带 user-action） | 2026-10-20T01:36:00Z：用户在结果卡片上点「撤销」 |
| `read-outbox-after-undo.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:36:00Z：撤销完再读 outbox |
| `read-summary-after-undo.json` | resources/read `summary` | 2026-10-20T01:36:00Z：撤销完再读概览 |
| `read-page-today-after-undo.json` | resources/read `page/today` | 2026-10-20T01:36:00Z：读基础页 page/today |
| `read-page-inbox-after-undo.json` | resources/read `page/inbox` | 2026-10-20T01:36:00Z：读基础页 page/inbox |
| `read-page-clean-after-undo.json` | resources/read `page/clean` | 2026-10-20T01:36:00Z：读基础页 page/clean |
| `read-page-digest-after-undo.json` | resources/read `page/digest` | 2026-10-20T01:36:00Z：读基础页 page/digest |
| `read-page-subs-after-undo.json` | resources/read `page/subs` | 2026-10-20T01:36:00Z：读基础页 page/subs |
| `read-page-rules-after-undo.json` | resources/read `page/rules` | 2026-10-20T01:36:00Z：读基础页 page/rules |
| `read-page-activity-after-undo.json` | resources/read `page/activity` | 2026-10-20T01:36:00Z：读基础页 page/activity |
| `read-page-settings-after-undo.json` | resources/read `page/settings` | 2026-10-20T01:36:00Z：读基础页 page/settings |
| `call-mail_apply_plan-tidy.json` | tools/call `mail_apply_plan`（带 user-action） | 2026-10-20T01:37:00Z：用户在「邮箱」卡片的「可以整理」点「归档 1 · 删除 1」 |
| `read-outbox-after-tidy.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:37:00Z：整理完再读 outbox |

`call-mail_apply_plan.json` 答：「已写好 1 封草稿，放在草稿箱，请检查后自行发送。」（done: true，更新原卡片）

`call-mail_undo.json` 答：「已撤销：1 封草稿删掉了。」（done: true，更新原卡片）

`call-mail_apply_plan-tidy.json` 答：「已归档 1 封；已删除 1 封。」（done: true，更新原卡片）

## `suspicious`：可疑邮件

一封冒充银行的邮件被判成可疑，同时来了一封要回的信。

**怎么录的**：LUOKE_MAIL_HOME 指到新建的临时目录；假邮箱是 tests/fakes.py 的 FakeIMAP（gmail），假模型是 FakeChat，分拣和草稿的答复写在 scripts/record_agent_fixtures.py 的 `MAILS` 里；连上了假邮箱 me@example.com（Gmail，处理模式开着）；收件箱里放了：账户已冻结、答辩时间确认；2026-10-20T01:30:00Z 同步了一次（App.sync_mailbox：真的读信、分拣、做习惯里的事）。

**邮件助手报的**

- 概览（`summary`）：count 2，「1 封要你处理，1 封可疑」
- 权限（`permissions`）：`mailbox` granted
- outbox：2 件
  - `suspicious/suspicious-000000000001`：`suspicious`、`urgent` → 急事（urgent）
    - 标题：可疑邮件「某银行：账户已冻结」
    - 摘要：某银行：账户已冻结
    - 卡片（surface `suspicious-000000000001`）：
      - **可疑邮件**
      - me@example.com
      - 某银行
      - 账户已冻结
      - 发件域名仿冒银行
      - 按钮：「移到垃圾邮件」→ `mail_apply_plan`、「先不动」→ `mail_apply_plan`、「在邮件助手里看」→ `agent.open_page`
  - `new-mail/new-mail-000000000002`：`new-mail`、`normal` → 要你决定（decide）
    - 标题：1 封邮件要你处理
    - 摘要：王老师：王老师问答辩时间，周五前回。
    - 卡片（surface `new-mail-000000000002`）：
      - **1 封要处理**
      - me@example.com
      - 王老师
      - 答辩时间确认
      - 10-23 截止
      - 王老师问答辩时间，周五前回
      - 选项：王老师：答辩时间确认（draft/flag/archive/keep，默认 draft）
      - 按钮：「写草稿 1」→ `mail_apply_plan`、「在邮件助手里看」→ `agent.open_page`
- 基础页 `page/today`：
  - 螺壳今天处理了哪些邮件。不对的改一下，它会记住。
  - 今天
  - 按钮：「昨天」→ `mail_view`
  - 按钮：「10/18」→ `mail_view`
  - 按钮：「10/17」→ `mail_view`
  - 按钮：「10/16」→ `mail_view`
  - 按钮：「10/15」→ `mail_view`
  - 按钮：「10/14」→ `mail_view`
  - 全部 2
  - 按钮：「等你处理 2」→ `mail_view`
  - 等你处理
  - 2
  - 请到「收件箱」处理。
- 基础页 `page/inbox`：
  - 回复 1、删除 1
  - 按钮：「同步新邮件」→ `mail_do`
  - 快到期
  - 需要你处理
  - 按钮：「可以直接处理」→ `mail_view`
  - 按钮：「判为可疑」→ `mail_view`
  - 按钮：「全部」→ `mail_view`
- 基础页 `page/clean`：
  - 收件箱中的旧邮件
  - 按钮：「已归档的邮件」→ `mail_view`
  - 收件箱里超过 30 天的邮件。加了星标或「收藏」「重要」这类标签的不会动。
  - 还没有读取这些邮件。
  - 按钮：「读取邮件」→ `mail_do`
- 基础页 `page/digest`：
  - 暂时按热度挑选，读得多了会按你的口味挑。在「设置」里添加你的网站或博客。
  - 精选
  - 按钮：「收藏（0）」→ `mail_view`
  - 按钮：「已归档（0）」→ `mail_view`
  - 按钮：「精选来源（0）」→ `mail_view`
  - 还没有精选来源。
  - 在「订阅体检」里把发件人加入精选，或在「清理」的「订阅内容」一栏选择「先读一遍」。
- 基础页 `page/subs`：
  - 选项：体检范围（30/60/90/180/365，默认 60）
  - 按钮：「开始体检」→ `mail_do`
  - 还没体检过。
  - 设好天数，点「开始体检」。
- 基础页 `page/rules`：
  - 告诉螺壳某类邮件以后怎么处理。
  - 规则
  - 还没有规则。在收件箱中更改建议时可以创建规则，也可以在订阅体检中设置。
  - 按习惯自动做的
  - 还没有。你确认过的处理习惯会列在这里。
- 基础页 `page/activity`：
  - 螺壳对邮箱做过的每一步，退订以外都能撤销。
- 基础页 `page/settings`：
  - **邮箱**
  - 推荐给螺壳一个专用邮箱，把订阅、通知转发过去，由它自动整理。
  - 按钮：「添加邮箱…」→ `mail_view`
  - **分析**
  - 同步范围：同步和整理只看这个范围内的邮件
  - 按钮：「保存范围」→ `mail_do`
  - 回复署名：写在回复草稿的末尾
  - 按钮：「保存署名」→ `mail_do`
  - **后台自动整理**
  - 已关闭：开启后定时整理，有要你处理的邮件时发通知。
  - 选项：整理间隔（15/30/60/120，默认 30）
  - 按钮：「开启」→ `mail_do`
  - **已读**
  - 分析过的邮件标为已读（已开启）
  - 按钮：「关闭」→ `mail_do`
  - **往来邮件**
  - 提醒在等对方回复的邮件（已开启）
  - 按钮：「关闭」→ `mail_do`
  - 会读「已发送」里最近一个月的邮件，只在本机分析。
  - **处理模式**
  - 已开启：批量删除、移到垃圾邮件时，每次都需要你确认。
  - 按钮：「关闭」→ `mail_do`
  - **本机模型**
  - test-model · 本机模型服务
  - **功耗**
  - 全速（当前）
  - 按钮：「均衡」→ `mail_do`
  - 按钮：「安静」→ `mail_do`
  - 模型连续运行，速度最快，风扇可能全速转。
  - **记忆**
  - 关于你 0 条 · 关注的话题 0 个
  - 从邮件中了解你
  - 已开启，下次空闲时继续
  - 按钮：「暂停」→ `mail_do`
  - 你的网站和博客
  - 螺壳会读取这些网站上的文章，用来了解你关注的话题。只读取你填写的网址。
  - 按钮：「添加」→ `mail_do`
  - **本机数据**
  - 账号、授权码和分析结果只保存在本机。
  - 注意：在此系统上，授权码以明文保存在本机文件中（仅你的账户可读）。
  - 不勾选，记忆留给其他螺壳工具用。勾选也只删邮件助手自己记下和推测的；你确认过、改过或自己写的会保留。
  - 按钮：「全部清除」→ `mail_do`

**窗口上**

- 一件 `suspicious`、`urgent`：急事横幅，按「主动提醒」的设置发通知。卡片标题「可疑邮件」后面一个邮箱地址的标签，一行发件人（加粗）和主题，一行理由；每个选项一个按钮，只有「移到垃圾邮件」（`destructive`，浅红底）「先不动」，永远不给退订；root 带 `subtitle`（发件人 · 主题）。
- `outbox_ack` `shown` 只是记账：agent 说过了，邮件助手不再发自己的通知。
- 点了以后答复带 `done: true`，这一件从 outbox 撤下，结果卡的 root 带 `group`（`<邮箱>/spam`）；概览的「可疑」减一。

**文件**（按请求的先后）

| 文件 | 请求 | 什么时候 |
|---|---|---|
| `server-discover.json` | server/discover | 2026-10-20T01:30:00Z：server/discover |
| `tools-list.json` | tools/list | 2026-10-20T01:30:00Z：列工具（agent 认 readOnlyHint） |
| `resources-list.json` | resources/list | 2026-10-20T01:30:00Z：列资源 |
| `read-outbox.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:30:00Z：读 outbox（每分钟一次） |
| `read-summary.json` | resources/read `summary` | 2026-10-20T01:30:00Z：读概览（侧栏的计数和一句话） |
| `read-permissions.json` | resources/read `permissions` | 2026-10-20T01:30:00Z：读权限的状态 |
| `read-page-today.json` | resources/read `page/today` | 2026-10-20T01:30:00Z：读基础页 page/today |
| `read-page-inbox.json` | resources/read `page/inbox` | 2026-10-20T01:30:00Z：读基础页 page/inbox |
| `read-page-clean.json` | resources/read `page/clean` | 2026-10-20T01:30:00Z：读基础页 page/clean |
| `read-page-digest.json` | resources/read `page/digest` | 2026-10-20T01:30:00Z：读基础页 page/digest |
| `read-page-subs.json` | resources/read `page/subs` | 2026-10-20T01:30:00Z：读基础页 page/subs |
| `read-page-rules.json` | resources/read `page/rules` | 2026-10-20T01:30:00Z：读基础页 page/rules |
| `read-page-activity.json` | resources/read `page/activity` | 2026-10-20T01:30:00Z：读基础页 page/activity |
| `read-page-settings.json` | resources/read `page/settings` | 2026-10-20T01:30:00Z：读基础页 page/settings |
| `call-outbox_ack-shown.json` | tools/call `outbox_ack` | 2026-10-20T01:32:00Z：agent 把急事说给用户以后记一下（outbox_ack shown） |
| `call-mail_apply_plan.json` | tools/call `mail_apply_plan`（带 user-action） | 2026-10-20T01:32:00Z：用户在急事卡片上点「移到垃圾邮件」 |
| `read-outbox-after-apply.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:32:00Z：点完再读 outbox |
| `read-summary-after-apply.json` | resources/read `summary` | 2026-10-20T01:32:00Z：点完再读概览 |
| `read-page-today-after-apply.json` | resources/read `page/today` | 2026-10-20T01:32:00Z：读基础页 page/today |
| `read-page-inbox-after-apply.json` | resources/read `page/inbox` | 2026-10-20T01:32:00Z：读基础页 page/inbox |
| `read-page-clean-after-apply.json` | resources/read `page/clean` | 2026-10-20T01:32:00Z：读基础页 page/clean |
| `read-page-digest-after-apply.json` | resources/read `page/digest` | 2026-10-20T01:32:00Z：读基础页 page/digest |
| `read-page-subs-after-apply.json` | resources/read `page/subs` | 2026-10-20T01:32:00Z：读基础页 page/subs |
| `read-page-rules-after-apply.json` | resources/read `page/rules` | 2026-10-20T01:32:00Z：读基础页 page/rules |
| `read-page-activity-after-apply.json` | resources/read `page/activity` | 2026-10-20T01:32:00Z：读基础页 page/activity |
| `read-page-settings-after-apply.json` | resources/read `page/settings` | 2026-10-20T01:32:00Z：读基础页 page/settings |

`call-outbox_ack-shown.json` 答：「好的」

`call-mail_apply_plan.json` 答：「已移到垃圾邮件 1 封。」（done: true，更新原卡片）

## `auto-done`：按习惯自动处理了

用户确认过两条处理习惯（两家店的推广邮件都归档），另有一条规则。同步时两家各来一封推广，邮件助手自己归档了，又来了一封要回的信。

**怎么录的**：LUOKE_MAIL_HOME 指到新建的临时目录；假邮箱是 tests/fakes.py 的 FakeIMAP（gmail），假模型是 FakeChat，分拣和草稿的答复写在 scripts/record_agent_fixtures.py 的 `MAILS` 里；连上了假邮箱 me@example.com（Gmail，处理模式开着）；记忆里有一条用户确认过的习惯：「某某商城的推广邮件都归档」；记忆里有一条用户确认过的习惯：「某某书店的推广邮件都归档」；有一条规则：sender news@weekly.example → archive；收件箱里放了：周末促销、新品上架、答辩时间确认；2026-10-20T01:30:00Z 同步了一次（App.sync_mailbox：真的读信、分拣、做习惯里的事）。

**邮件助手报的**

- 概览（`summary`）：count 1，「1 封要你处理，今天自动处理了 2 封」
- 权限（`permissions`）：`mailbox` granted
- outbox：2 件
  - `new-mail/new-mail-000000000001`：`new-mail`、`normal` → 要你决定（decide）
    - 标题：1 封邮件要你处理
    - 摘要：王老师：王老师问答辩时间，周五前回。
    - 卡片（surface `new-mail-000000000001`）：
      - **1 封要处理**
      - me@example.com
      - 王老师
      - 答辩时间确认
      - 10-23 截止
      - 王老师问答辩时间，周五前回
      - 选项：王老师：答辩时间确认（draft/flag/archive/keep，默认 draft）
      - 按钮：「写草稿 1」→ `mail_apply_plan`、「在邮件助手里看」→ `agent.open_page`
  - `auto-done/auto-done-000000000002`：`auto-done`、`normal` → 已经替你做了（done）
    - 标题：已自动处理 2 封
    - 摘要：按你的习惯自动归档 1 封；按你的习惯自动归档 1 封
    - 卡片（surface `auto-done-000000000002`）：
      - **已自动处理 2 封**
      - 按你的习惯自动归档 1 封
      - 周末促销
      - 按钮：「撤销」→ `mail_undo`
      - 按你的习惯自动归档 1 封
      - 新品上架
      - 按钮：「撤销」→ `mail_undo`
- 基础页 `page/today`：
  - 螺壳今天处理了哪些邮件。不对的改一下，它会记住。
  - 今天
  - 按钮：「昨天」→ `mail_view`
  - 按钮：「10/18」→ `mail_view`
  - 按钮：「10/17」→ `mail_view`
  - 按钮：「10/16」→ `mail_view`
  - 按钮：「10/15」→ `mail_view`
  - 按钮：「10/14」→ `mail_view`
  - 全部 3
  - 按钮：「等你处理 1」→ `mail_view`
  - 按钮：「自动处理 2」→ `mail_view`
  - 等你处理
  - 1
  - 请到「收件箱」处理。
  - 已自动处理
  - 2
- 基础页 `page/inbox`：
  - 回复 1
  - 按钮：「同步新邮件」→ `mail_do`
  - 快到期
  - 需要你处理
  - 按钮：「可以直接处理」→ `mail_view`
  - 按钮：「判为可疑」→ `mail_view`
  - 按钮：「全部」→ `mail_view`
  - 这里没有邮件。
- 基础页 `page/clean`：
  - 收件箱中的旧邮件
  - 按钮：「已归档的邮件」→ `mail_view`
  - 收件箱里超过 30 天的邮件。加了星标或「收藏」「重要」这类标签的不会动。
  - 还没有读取这些邮件。
  - 按钮：「读取邮件」→ `mail_do`
- 基础页 `page/digest`：
  - 暂时按热度挑选，读得多了会按你的口味挑。在「设置」里添加你的网站或博客。
  - 精选
  - 按钮：「收藏（0）」→ `mail_view`
  - 按钮：「已归档（0）」→ `mail_view`
  - 按钮：「精选来源（0）」→ `mail_view`
  - 还没有精选来源。
  - 在「订阅体检」里把发件人加入精选，或在「清理」的「订阅内容」一栏选择「先读一遍」。
- 基础页 `page/subs`：
  - 选项：体检范围（30/60/90/180/365，默认 60）
  - 按钮：「开始体检」→ `mail_do`
  - 还没体检过。
  - 设好天数，点「开始体检」。
- 基础页 `page/rules`：
  - 告诉螺壳某类邮件以后怎么处理。
  - 规则仅在螺壳邮件助手分析邮件时生效。将 1 条规则导出为 Gmail 过滤器后，Gmail 会自动处理之后的邮件。
  - 按钮：「导出到 Gmail（1 条）」→ `mail_do`、「打开 Gmail 过滤器设置」→ `agent.open_url`
  - 规则
  - 按习惯自动做的
  - 螺壳照你确认过的习惯自动处理，每封都列在「今日处理」里，能逐封撤销。
  - 某某书店的推广邮件都归档
  - 某某商城的推广邮件都归档
- 基础页 `page/activity`：
  - 螺壳对邮箱做过的每一步，退订以外都能撤销。
- 基础页 `page/settings`：
  - **邮箱**
  - 推荐给螺壳一个专用邮箱，把订阅、通知转发过去，由它自动整理。
  - 按钮：「添加邮箱…」→ `mail_view`
  - **分析**
  - 同步范围：同步和整理只看这个范围内的邮件
  - 按钮：「保存范围」→ `mail_do`
  - 回复署名：写在回复草稿的末尾
  - 按钮：「保存署名」→ `mail_do`
  - **后台自动整理**
  - 已关闭：开启后定时整理，有要你处理的邮件时发通知。
  - 选项：整理间隔（15/30/60/120，默认 30）
  - 按钮：「开启」→ `mail_do`
  - **已读**
  - 分析过的邮件标为已读（已开启）
  - 按钮：「关闭」→ `mail_do`
  - **往来邮件**
  - 提醒在等对方回复的邮件（已开启）
  - 按钮：「关闭」→ `mail_do`
  - 会读「已发送」里最近一个月的邮件，只在本机分析。
  - **处理模式**
  - 已开启：批量删除、移到垃圾邮件时，每次都需要你确认。
  - 按钮：「关闭」→ `mail_do`
  - **本机模型**
  - test-model · 本机模型服务
  - **功耗**
  - 全速（当前）
  - 按钮：「均衡」→ `mail_do`
  - 按钮：「安静」→ `mail_do`
  - 模型连续运行，速度最快，风扇可能全速转。
  - **记忆**
  - 关于你 0 条 · 关注的话题 0 个
  - 从邮件中了解你
  - 已开启，下次空闲时继续
  - 按钮：「暂停」→ `mail_do`
  - 你的网站和博客
  - 螺壳会读取这些网站上的文章，用来了解你关注的话题。只读取你填写的网址。
  - 按钮：「添加」→ `mail_do`
  - **本机数据**
  - 账号、授权码和分析结果只保存在本机。
  - 注意：在此系统上，授权码以明文保存在本机文件中（仅你的账户可读）。
  - 不勾选，记忆留给其他螺壳工具用。勾选也只删邮件助手自己记下和推测的；你确认过、改过或自己写的会保留。
  - 按钮：「全部清除」→ `mail_do`

**窗口上**

- 一件 `auto-done`、`normal`，在「已经替你做了」：每组一行说明和 [撤销]（`mail_undo`，参数 `auto:<记录>`）。
- 卡片上撤一组：答复换上只剩另一组的卡片（`done: false`）；这件还在 outbox 里，标题的数字减一。
- 「处理记录」页上撤（`mail_undo`，参数 `log:<记录>`，`surfaceId` 是页 id `activity`）：答复里的卡片不画，窗口重新读这一页，那一行写「已撤销」，按钮没了；两组都撤了，这件从 outbox 撤下。
- 「规则」页列着两条习惯和一条规则，「编辑规则」打开邮件助手自己的页面。

**文件**（按请求的先后）

| 文件 | 请求 | 什么时候 |
|---|---|---|
| `server-discover.json` | server/discover | 2026-10-20T01:31:00Z：server/discover |
| `tools-list.json` | tools/list | 2026-10-20T01:31:00Z：列工具（agent 认 readOnlyHint） |
| `resources-list.json` | resources/list | 2026-10-20T01:31:00Z：列资源 |
| `read-outbox.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:31:00Z：读 outbox（每分钟一次） |
| `read-summary.json` | resources/read `summary` | 2026-10-20T01:31:00Z：读概览（侧栏的计数和一句话） |
| `read-permissions.json` | resources/read `permissions` | 2026-10-20T01:31:00Z：读权限的状态 |
| `read-page-today.json` | resources/read `page/today` | 2026-10-20T01:31:00Z：读基础页 page/today |
| `read-page-inbox.json` | resources/read `page/inbox` | 2026-10-20T01:31:00Z：读基础页 page/inbox |
| `read-page-clean.json` | resources/read `page/clean` | 2026-10-20T01:31:00Z：读基础页 page/clean |
| `read-page-digest.json` | resources/read `page/digest` | 2026-10-20T01:31:00Z：读基础页 page/digest |
| `read-page-subs.json` | resources/read `page/subs` | 2026-10-20T01:31:00Z：读基础页 page/subs |
| `read-page-rules.json` | resources/read `page/rules` | 2026-10-20T01:31:00Z：读基础页 page/rules |
| `read-page-activity.json` | resources/read `page/activity` | 2026-10-20T01:31:00Z：读基础页 page/activity |
| `read-page-settings.json` | resources/read `page/settings` | 2026-10-20T01:31:00Z：读基础页 page/settings |
| `call-mail_undo-card.json` | tools/call `mail_undo`（带 user-action） | 2026-10-20T01:40:00Z：用户在自动处理的卡片上点第一组的「撤销」 |
| `read-outbox-after-undo-card.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:40:00Z：点完再读 outbox（还剩一组） |
| `read-summary-after-undo-card.json` | resources/read `summary` | 2026-10-20T01:40:00Z：点完再读概览 |
| `read-page-today-after-undo-card.json` | resources/read `page/today` | 2026-10-20T01:40:00Z：读基础页 page/today |
| `read-page-inbox-after-undo-card.json` | resources/read `page/inbox` | 2026-10-20T01:40:00Z：读基础页 page/inbox |
| `read-page-clean-after-undo-card.json` | resources/read `page/clean` | 2026-10-20T01:40:00Z：读基础页 page/clean |
| `read-page-digest-after-undo-card.json` | resources/read `page/digest` | 2026-10-20T01:40:00Z：读基础页 page/digest |
| `read-page-subs-after-undo-card.json` | resources/read `page/subs` | 2026-10-20T01:40:00Z：读基础页 page/subs |
| `read-page-rules-after-undo-card.json` | resources/read `page/rules` | 2026-10-20T01:40:00Z：读基础页 page/rules |
| `read-page-activity-after-undo-card.json` | resources/read `page/activity` | 2026-10-20T01:40:00Z：读基础页 page/activity |
| `read-page-settings-after-undo-card.json` | resources/read `page/settings` | 2026-10-20T01:40:00Z：读基础页 page/settings |
| `call-mail_do-page.json` | tools/call `mail_do`（带 user-action） | 2026-10-20T01:41:00Z：用户在「处理记录」页上点另一组的「撤销」 |
| `read-page-activity-after-undo-page.json` | resources/read `page/activity` | 2026-10-20T01:41:00Z：点完窗口重新读这一页 |
| `read-outbox-after-undo-page.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:41:00Z：点完再读 outbox（都撤了，这件撤下） |
| `read-summary-after-undo-page.json` | resources/read `summary` | 2026-10-20T01:41:00Z：点完再读概览 |

`call-mail_undo-card.json` 答：「已撤销」（done: false，更新原卡片）

`call-mail_do-page.json` 答：「已撤销」

## `search`：问螺壳搜邮件

收件箱里有几封信，用户在问螺壳里搜「答辩」。

**怎么录的**：LUOKE_MAIL_HOME 指到新建的临时目录；假邮箱是 tests/fakes.py 的 FakeIMAP（gmail），假模型是 FakeChat，分拣和草稿的答复写在 scripts/record_agent_fixtures.py 的 `MAILS` 里；连上了假邮箱 me@example.com（Gmail，处理模式开着）；收件箱里放了：答辩时间确认、项目周会、账户已冻结；2026-10-20T01:30:00Z 同步了一次（App.sync_mailbox：真的读信、分拣、做习惯里的事）。

**邮件助手报的**

- 概览（`summary`）：count 3，「2 封要你处理，1 封可疑」
- 权限（`permissions`）：`mailbox` granted
- outbox：3 件
  - `suspicious/suspicious-000000000001`：`suspicious`、`urgent` → 急事（urgent）
    - 标题：可疑邮件「某银行：账户已冻结」
    - 摘要：某银行：账户已冻结
    - 卡片（surface `suspicious-000000000001`）：
      - **可疑邮件**
      - me@example.com
      - 某银行
      - 账户已冻结
      - 发件域名仿冒银行
      - 按钮：「移到垃圾邮件」→ `mail_apply_plan`、「先不动」→ `mail_apply_plan`、「在邮件助手里看」→ `agent.open_page`
  - `new-mail/new-mail-000000000002`：`new-mail`、`normal` → 要你决定（decide）
    - 标题：1 封邮件要你处理
    - 摘要：王老师：王老师问答辩时间，周五前回。
    - 卡片（surface `new-mail-000000000002`）：
      - **1 封要处理**
      - me@example.com
      - 王老师
      - 答辩时间确认
      - 10-23 截止
      - 王老师问答辩时间，周五前回
      - 选项：王老师：答辩时间确认（draft/flag/archive/keep，默认 draft）
      - 按钮：「写草稿 1」→ `mail_apply_plan`、「查看全部 2 封」→ `agent.open_page`
  - `mailbox/mailbox-000000000003`：`mailbox`、`low` → 顺便看看（later）
    - 标题：1 封待处理
    - 摘要：项目组：项目周会。
    - 卡片（surface `mailbox-000000000003`）：
      - **邮箱**
      - me@example.com
      - 待处理
      - 项目组
      - 项目周会
      - 周四下午三点的周会
      - 选项：项目组：项目周会（flag/archive/keep，默认 flag）
      - 按钮：「10-22 15:00」→ `agent.open_url`
      - 按钮：「加星标 1」→ `mail_apply_plan`、「查看全部 2 封」→ `agent.open_page`
- 基础页 `page/today`：
  - 螺壳今天处理了哪些邮件。不对的改一下，它会记住。
  - 今天
  - 按钮：「昨天」→ `mail_view`
  - 按钮：「10/18」→ `mail_view`
  - 按钮：「10/17」→ `mail_view`
  - 按钮：「10/16」→ `mail_view`
  - 按钮：「10/15」→ `mail_view`
  - 按钮：「10/14」→ `mail_view`
  - 全部 3
  - 按钮：「等你处理 3」→ `mail_view`
  - 等你处理
  - 3
  - 请到「收件箱」处理。
- 基础页 `page/inbox`：
  - 回复 1、加入日程 1、删除 1
  - 按钮：「同步新邮件」→ `mail_do`
  - 快到期
  - 需要你处理
  - 按钮：「可以直接处理」→ `mail_view`
  - 按钮：「判为可疑」→ `mail_view`
  - 按钮：「全部」→ `mail_view`
- 基础页 `page/clean`：
  - 收件箱中的旧邮件
  - 按钮：「已归档的邮件」→ `mail_view`
  - 收件箱里超过 30 天的邮件。加了星标或「收藏」「重要」这类标签的不会动。
  - 还没有读取这些邮件。
  - 按钮：「读取邮件」→ `mail_do`
- 基础页 `page/digest`：
  - 暂时按热度挑选，读得多了会按你的口味挑。在「设置」里添加你的网站或博客。
  - 精选
  - 按钮：「收藏（0）」→ `mail_view`
  - 按钮：「已归档（0）」→ `mail_view`
  - 按钮：「精选来源（0）」→ `mail_view`
  - 还没有精选来源。
  - 在「订阅体检」里把发件人加入精选，或在「清理」的「订阅内容」一栏选择「先读一遍」。
- 基础页 `page/subs`：
  - 选项：体检范围（30/60/90/180/365，默认 60）
  - 按钮：「开始体检」→ `mail_do`
  - 还没体检过。
  - 设好天数，点「开始体检」。
- 基础页 `page/rules`：
  - 告诉螺壳某类邮件以后怎么处理。
  - 规则
  - 还没有规则。在收件箱中更改建议时可以创建规则，也可以在订阅体检中设置。
  - 按习惯自动做的
  - 还没有。你确认过的处理习惯会列在这里。
- 基础页 `page/activity`：
  - 螺壳对邮箱做过的每一步，退订以外都能撤销。
- 基础页 `page/settings`：
  - **邮箱**
  - 推荐给螺壳一个专用邮箱，把订阅、通知转发过去，由它自动整理。
  - 按钮：「添加邮箱…」→ `mail_view`
  - **分析**
  - 同步范围：同步和整理只看这个范围内的邮件
  - 按钮：「保存范围」→ `mail_do`
  - 回复署名：写在回复草稿的末尾
  - 按钮：「保存署名」→ `mail_do`
  - **后台自动整理**
  - 已关闭：开启后定时整理，有要你处理的邮件时发通知。
  - 选项：整理间隔（15/30/60/120，默认 30）
  - 按钮：「开启」→ `mail_do`
  - **已读**
  - 分析过的邮件标为已读（已开启）
  - 按钮：「关闭」→ `mail_do`
  - **往来邮件**
  - 提醒在等对方回复的邮件（已开启）
  - 按钮：「关闭」→ `mail_do`
  - 会读「已发送」里最近一个月的邮件，只在本机分析。
  - **处理模式**
  - 已开启：批量删除、移到垃圾邮件时，每次都需要你确认。
  - 按钮：「关闭」→ `mail_do`
  - **本机模型**
  - test-model · 本机模型服务
  - **功耗**
  - 全速（当前）
  - 按钮：「均衡」→ `mail_do`
  - 按钮：「安静」→ `mail_do`
  - 模型连续运行，速度最快，风扇可能全速转。
  - **记忆**
  - 关于你 0 条 · 关注的话题 0 个
  - 从邮件中了解你
  - 已开启，下次空闲时继续
  - 按钮：「暂停」→ `mail_do`
  - 你的网站和博客
  - 螺壳会读取这些网站上的文章，用来了解你关注的话题。只读取你填写的网址。
  - 按钮：「添加」→ `mail_do`
  - **本机数据**
  - 账号、授权码和分析结果只保存在本机。
  - 注意：在此系统上，授权码以明文保存在本机文件中（仅你的账户可读）。
  - 不勾选，记忆留给其他螺壳工具用。勾选也只删邮件助手自己记下和推测的；你确认过、改过或自己写的会保留。
  - 按钮：「全部清除」→ `mail_do`

**窗口上**

- `mail_search` 答 `hits`（工具协议 §1b），`card` 是空的：窗口的 `HitsList` 做好之前，agent 的 `_search_one` 不画这个来源，见下面「要对齐的」。
- 每条的 `actions` 是 `mail_show`（打开这封），`source` 是邮箱地址。

**文件**（按请求的先后）

| 文件 | 请求 | 什么时候 |
|---|---|---|
| `server-discover.json` | server/discover | 2026-10-20T01:30:00Z：server/discover |
| `tools-list.json` | tools/list | 2026-10-20T01:30:00Z：列工具（agent 认 readOnlyHint） |
| `resources-list.json` | resources/list | 2026-10-20T01:30:00Z：列资源 |
| `read-outbox.json` | resources/read `luoke://luoke-mail/outbox` | 2026-10-20T01:30:00Z：读 outbox（每分钟一次） |
| `read-summary.json` | resources/read `summary` | 2026-10-20T01:30:00Z：读概览（侧栏的计数和一句话） |
| `read-permissions.json` | resources/read `permissions` | 2026-10-20T01:30:00Z：读权限的状态 |
| `read-page-today.json` | resources/read `page/today` | 2026-10-20T01:30:00Z：读基础页 page/today |
| `read-page-inbox.json` | resources/read `page/inbox` | 2026-10-20T01:30:00Z：读基础页 page/inbox |
| `read-page-clean.json` | resources/read `page/clean` | 2026-10-20T01:30:00Z：读基础页 page/clean |
| `read-page-digest.json` | resources/read `page/digest` | 2026-10-20T01:30:00Z：读基础页 page/digest |
| `read-page-subs.json` | resources/read `page/subs` | 2026-10-20T01:30:00Z：读基础页 page/subs |
| `read-page-rules.json` | resources/read `page/rules` | 2026-10-20T01:30:00Z：读基础页 page/rules |
| `read-page-activity.json` | resources/read `page/activity` | 2026-10-20T01:30:00Z：读基础页 page/activity |
| `read-page-settings.json` | resources/read `page/settings` | 2026-10-20T01:30:00Z：读基础页 page/settings |
| `call-mail_search.json` | tools/call `mail_search` | 2026-10-20T01:33:00Z：用户问螺壳「答辩」：搜索框调 mail_search（只读，参数照 agent 的 `search`） |

`call-mail_search.json` 答：「找到 1 封邮件。」
