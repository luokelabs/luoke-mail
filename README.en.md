# Luoke Mail

[简体中文](README.md) | English

Sort your whole mailbox with a model running on your own Mac. Every message is read by the model, which tells you what needs a reply, what belongs on your calendar, and what can be archived or moved to the trash; nothing happens until you confirm it, and every step can be undone. Your mail is read and analysed on your computer only and never passes through a cloud service.

Luoke Mail (螺壳邮件助手, `luoke-mail`) is part of [Luoke](https://github.com/luokelabs/luoke) by Luoke Labs. **The app's interface is in Chinese for now**; an English interface is on the roadmap. Interface labels below are given in Chinese with a translation.

## What it is and where it sits in Luoke

Luoke is a family of local AI tools made by Luoke Labs (螺壳实验室); Luoke Mail is its first official app. It handles mail itself: reading messages, making suggestions, and carrying them out (and undoing them) in your mailbox. Model inference is left to Luoke ([luoke-core](https://github.com/luokelabs/luoke-core)), which picks a model for the Mac's memory and schedules background work for when you are not using the computer. What it learns about you lives in Luoke Memory ([luoke-mem](https://github.com/luokelabs/luoke-mem)), shared by all Luoke apps: Luoke Mail only records what happened, and the memory draws the conclusions. For how the pieces fit together see the top-level repository [luokelabs/luoke](https://github.com/luokelabs/luoke) and the [Luoke Mail page of the system docs](https://luokelabs.github.io/luoke-atlas/#mail).

**Inside Luoke's window** (from 0.19.45): 今日处理 (today), the inbox, 清理 (clean-up), 精选 (digest), 订阅体检 (subscription check-up), rules, the record of what was done, and settings are pages of the mail assistant in Luoke's window; no separate web page. Adding a mailbox or changing its app password is done in the window's settings too (the password is kept only in this Mac's keychain), and exporting Gmail filters asks where to save the file. With an older Luoke, these two still happen in the browser. Run from source, the page opens in the browser as before.

## Status

- **In closed beta** (beta1, Luoke Mail 0.18.11; the development version is in [`luoke_mail/__init__.py`](luoke_mail/__init__.py)). During the closed beta we fix problems and add no features.
- **System**: Apple silicon (M1 or later), macOS 26.2 or later, 16 GB of memory or more, 32 GB or more recommended. These are Luoke's requirements; Luoke Mail adds none of its own.
- **Measured per memory tier** (through Luoke, on the evaluation sets; source: [docs/community/luoke-mail.md](docs/community/luoke-mail.md), 「各档的真实体验」):

| | 16 GB | 32 GB | 128 GB |
|---|---|---|---|
| Base model | Qwen3.5-9B 4-bit, loaded only while you are away | Qwen3.5-9B 4-bit, resident | gemma-4-26B-A4B 8-bit, resident |
| Fully correct, of 45 sorting questions | 39, about 22 s per message | 39, about 11 s per message | 44, about 2 s per message |
| Legitimate mail flagged as suspicious | 0 | 0 | 0 |

- **Mailboxes**: Gmail, QQ Mail, NetEase 163 / 126, and other IMAP mailboxes.
- **Interface**: Chinese; English is on the roadmap.

## Getting it

Download Luoke from [luoke.ai](https://luoke.ai), turn on Luoke Mail under 「应用」 (Apps) in Luoke, open it and connect your mailbox as the page guides you. Luoke prepares the model; there is nothing to install yourself. Gmail, QQ Mail and NetEase need an app password or authorisation code generated in the mailbox's own settings; step-by-step instructions (in Chinese) are in [docs/community/授权码引导.md](docs/community/授权码引导.md).

Before connecting a mailbox you can try every feature on the built-in sample inbox (44 fictional messages), which touches no mailbox at all.

## What it does

- **Sorting**: mail from the last 30 days (adjustable), read or unread, each gets one suggestion — reply, add to calendar, star, archive, delete — plus a one-line summary, the reason, a deadline (only when the mail states one) and points for a reply. Mail impersonating a bank, asking for a verification code or trying to instruct the AI is flagged as 「可疑」 (suspicious); when the provider has verified the sender's domain (Gmail, QQ, NetEase), that verdict is passed to the model too. A code sent to you, or a sign-in notice, is not a request for one. When a mail's amounts are labelled only in pictures, the card gives no amount or date and asks you to check the original. Mail you archived on your phone after reading (last 7 days, Gmail) is listed separately if it still holds an appointment or a question for you.
- **Every message judged by the model**: a local model costs nothing to run, so there are no shortcuts — bulk mail is judged message by message too (a bank's statement and its promotion are told apart); messages from the same source with the same suggestion are grouped on the page and can be handled together.
- **One-click handling**: everything that can be handled directly (archive / delete / move to junk) in one go, with the effect listed first.
- **Order**: the list is ordered by suggestion, what needs you first; switch to by time for newest first.
- **Per message**:
  - Reply: the model drafts, you edit on the page, and it goes into the mailbox's Drafts for you to send yourself;
  - Calendar: Gmail opens a pre-filled Google Calendar event; other mailboxes get a calendar file; a mail with several appointments (a return flight, a multi-leg trip) lists them apart, added one by one in Gmail, all together in the calendar file;
  - Star: starred in your mailbox, archived when done.
- **Undo any time**: every action is kept in 「操作记录」 (activity log), and whatever can be undone can be undone there.
- **Subscription check-up** (订阅体检): per sender, how many messages arrived lately, how many you read, how much space they take; senders you never read get an unsubscribe suggestion — after the model has looked at the sender's recent subjects, so bills and sign-in alerts you rarely open but need are never suggested. One-click unsubscribe (RFC 8058), unsubscribe pages and unsubscribe e-mails are supported; when the headers offer no way out, the body is searched for an unsubscribe link or a "reply TD" instruction. Suspicious senders are never unsubscribed from.
- **Senders you cannot unsubscribe from**:
  - send their future mail straight to the trash (a rule, undoable);
  - Gmail: export the rules as a filter file and import it in Gmail's settings, so Gmail applies them even when the app is closed;
  - QQ Mail, NetEase: block them in the mailbox, so the server filters them (step-by-step per mailbox, address copied in one click);
  - 「清理历史邮件」 (clear past mail): move their earlier mail (archived mail included) to the trash, which the mailbox empties on its own schedule (Gmail and QQ 30 days, NetEase 7 days); until then it can be undone. Archiving saves no space; this step does.
- **Today's handling** (今日处理, top of the sidebar): what Luoke did with each message today — archived or labelled by your habits, marked read and left in the inbox, waiting for your reply or calendar entry, suspicious — each with one-click undo, mark as unread or change of suggestion, which Luoke remembers. Mail left for you is listed too: this page is your daily review. Long-unread mail from before the sorting range is analysed in idle time, marked read and listed in its own section, with anything still waiting for you marked as past due or not.
- **Luoke's own mailbox** (设置 → 邮箱, Settings → Mailboxes): we recommend giving Luoke a mailbox of its own (a new Gmail, say) and forwarding subscriptions and notifications there. In a mailbox set as Luoke's own, Luoke archives and moves to trash by itself, every message listed in 今日处理 and undoable; your own mailbox stays your decision.
- **Rules**: when changing a suggestion, tick 「以后……的邮件都这样处理」 (handle such mail this way from now on), or click 「以后都删除」 (always delete) in the check-up; similar mail then gets that suggestion from the model, which proposes otherwise — marked 「与规则不同」 (differs from rule) — only when a message clearly needs you (a bill about to fall due, a security warning, someone asking you directly) or is suspicious. Each inbox has its own rules; rules tried on the sample inbox never touch your mailbox.
- **Export to Gmail**: rules apply when the app sorts your mail; Gmail users can export them as a filter file (`mailFilters.xml`) and use 「导入过滤器」 (Import filters) in Gmail's settings, so Gmail applies them itself. No Google authorisation, no server in between.
- **Background sorting** (后台自动整理): started by Luoke at login and always running (from source, only while the program is open); every 15 minutes to 2 hours it reads new mail and sorts it, and sends a notification when something needs a reply, a calendar entry or a star — clicking it opens the page. Analysed mail is marked read and archived or labelled by habits you confirmed; at 20:00 each evening it reminds you to look at 今日处理. It never moves mail to the trash, unsubscribes or sends. If the model service is not running, new mail waits until it is; problems like an unreachable mailbox or a stopped model are reported once. Open Luoke Mail from Luoke's 「应用」 or from Launchpad to return to the page.
- **Clean-up** (清理, two tabs):
  - **Old mail in the inbox**: mail older than the sorting range, read by headers only (a thousand-odd in seconds), read and judged one by one by the local model in idle time (pausable; once judged, a suggestion appears at the top of the inbox, and a notification when all are done). Acting on it still needs your confirmation: ads and one-off notifications are suggested for the trash, mail that may still be open (an unpaid bill, an unanswered question) is listed as 「待确认」 (to check), the rest is suggested for archiving; mail not yet judged starts unticked and is archived only if you tick it. Starred mail and mail labelled 「收藏」 / 「重要」 (saved / important) is left alone; it runs in batches, each verified, and can be stopped midway; it can be undone (even after the process quit midway), read and unread state restored; what you left unticked stays unticked next time.
  - **Archived mail**: after 「开始分析」 (start analysis) the local model judges each message (a sender's mail is asked about in one batch, so a bank's statement and promotion are judged apart), into 「建议删除」 (suggest deleting: ads, subscriptions, one-off notices such as codes and deliveries) and 「建议保留」 (suggest keeping: bills and receipts, contracts, travel, account security, personal and work correspondence); your labels are shown and given to the model. Trash suggestions start ticked (Gmail empties its trash after 30 days); batches are verified and undoable, labels restored on undo; starred mail is left alone. The analysis runs in the background and is saved, giving way to syncing and background sorting: 13,000 messages take about an hour and a half on an M5 Max; a 16 GB machine can run it overnight.
- **Subscription digest** (订阅精选): the local model reads your newsletters (dailies, weeklies, pushes) for you, item by item when a message holds several, then compares them like an editor: about 20 picks for the last week and about 10 per earlier month, each with a summary and why it is worth reading; the same story is listed once. Two sources:
  - when clearing a subscription backlog, choose 「先挑出值得看的…」 (pick what is worth reading first): messages with picks are kept and starred, the rest go to the trash as usual;
  - senders added to the digest (in clean-up or the check-up): their new mail is gathered into the weekly digest, with a weekly reminder from background sorting.
  Every item can be 「收藏」 (saved, kept under Saved), 「归档」 (archived: read, out of the list, findable under Archived) or 「没兴趣」 (not interested); saves and not-interested shape later picks. You do not write your interests yourself: Luoke Memory infers them from your website or blog (entered on the memory page), your mailbox labels, the newsletters you open and your feedback; wrong ones get 「不对」 (not right). Reading one message takes ten-odd seconds (Qwen3.8 Flash-Next), so a backlog of a thousand can run overnight; reading and picking happen only in idle time.
- **Memory**: Luoke remembers things about you — about you, your website and blog, handling habits, topics you follow, things you are not interested in — to judge priority, draft replies and pick the digest. Each entry shows its evidence and can be edited or deleted. The memory is shared by Luoke's apps ([Luoke Memory](https://github.com/luokelabs/luoke-mem)) and stays on the device.
- **Learning about you from mail** (从邮件中了解你, on by default, pausable on the memory page): nothing to fill in. In idle time Luoke reads your mail, including what you sent: facts consistent across several messages (title, organisation, signature, colleagues and clients you write to often) are recorded directly, while forwarded and quoted words of others do not count; correspondence from organisations (banks, landlords, schools…) is used according to how much evidence there is; what your favourite newsletters are about and how you handle each kind of mail, Luoke Memory learns from the text itself. What only you can settle is asked under 「想问你的」 (questions for you) on the memory page, with the count beside 「记忆」 in the sidebar. Passwords, verification codes, full card and ID numbers are never stored; only that string is masked.
- **Power** (in Settings): full, balanced and quiet modes limit how long the local model runs, for less heat and fan noise; background work slows down accordingly.
- **Incremental**: only mail not seen before is downloaded and analysed; nothing is analysed twice.

## Promises

- **What can be taken back is done first, for you to review**: analysed mail is marked read; handling habits you confirmed (say, "archive this shop's promotions" — only that sender's that kind of mail, never its bills or security alerts; what you do yourself in Gmail or on your phone is learned too) are applied automatically: archive, mark read or label. Each message is listed in 今日处理 with one-click undo, and one undo records that the habit may be wrong.
- **What cannot be taken back waits for your click**: in your own mailbox, moving to trash, marking as junk and unsubscribing are never automatic, only suggested; moving many to the trash or clearing a sender's past mail first needs 「处理模式」 (handling mode) turned on, then a confirmation each time.
- **Never**: no sending, no permanent deletion, no forwarding; instructions to the AI inside mail are treated as data, never followed.
- **No Google authorisation**: Gmail is connected with an app password you generate yourself, kept in your Mac's keychain; Gmail filters come from a file you import yourself. Luoke has registered no app that can access your mailbox and runs no server.
- **Sample mode**: a built-in sample inbox of 44 messages lets you try everything before connecting a mailbox, touching none (it is hidden once a mailbox is connected); it has a memory of its own that never affects yours. The sample mail is fictional; company names in it only make the inbox look real (including phishing mails impersonating a bank and Apple).
- **Your data stays on your device**: the model runs only on your Mac, and mail and memory are processed there, never uploaded. On your device Luoke reads whatever it needs to understand you: the memory is kept in `~/.luoke/mem.db`, readable only by your account, every entry with its evidence and open to editing and deletion; passwords, verification codes, full card and ID numbers are never stored.

## Supported mailboxes

| Mailbox | What you need | Notes |
|---|---|---|
| Gmail | Two-step verification on, an app password | IMAP is on by default. Company Google Workspace accounts may have it disabled by the admin |
| QQ Mail | Turn on IMAP under Settings → Account and security on the web, generate a 16-character authorisation code | Your QQ password will not work |
| NetEase 163 / 126 | Turn on IMAP under Settings → POP3/SMTP/IMAP on the web, note the authorisation code | Your login password will not work |
| Others | Enter the IMAP server and port by hand | Advanced |

The differences between providers (NetEase requires an ID command first, QQ hangs on commands it does not know, Chinese folder names are encoded, Gmail archives with labels, some servers such as 139 Mail offer only RSA key exchange) are handled in the code; see [docs/DESIGN.md](docs/DESIGN.md) (in Chinese).

## Data and privacy

**On the device only**: the model runs on your Mac; mail, results and memory are processed and kept there, never uploaded. Luoke has no server and no registered app that can access your mailbox.

**When it goes online** (checked against the code line by line in 2026-10; the Luoke Mail section of Luoke's network inventory):

- **Your own mail server (IMAP)**: when syncing and acting, and on a timer for background sorting and idle work. It connects to imap.gmail.com, imap.qq.com, imap.163.com, imap.126.com or the server you entered. Besides reading, it archives, marks read, labels and saves drafts in your mailbox; moving to the trash needs your click.
- **One-click unsubscribe**: when you click unsubscribe in the check-up, one request goes to the unsubscribe address the sender put in the headers (RFC 8058). Senders of suspicious mail are never unsubscribed from.
- **Your website or blog**: read once when you add it on the memory page, then again every 30 days in the background.
- **Links you open**: Open in Gmail, add to Google Calendar (the URL carries the event title, time, place and a summary of the mail), unsubscribe pages and digest articles are all opened by your browser.
- **Model service**: the packaged app talks only to Luoke on the same Mac; from source, to the address you entered.

Model output never makes the program connect to a new address: every address comes from your settings, the mail headers or what you entered.

**Where data lives**:

- Settings: `~/.luoke-mail/config.json` (relocatable with `LUOKE_MAIL_HOME`)
- Sorting results, rules, activity log: `~/.luoke-mail/luoke.db` (SQLite)
- Passwords: the macOS keychain (service `luoke-mail:imap`, one entry per mailbox; with a direct model connection its API key is under `luoke-mail:engine`); on other systems, for now, `~/.luoke-mail/credentials.json` (mode 600), which the page points out is plain text
- The page's access token: `~/.luoke-mail/token` (mode 600); all of `~/.luoke-mail` is readable only by your account (700)
- Background log: `~/.luoke-mail/service.log` (never the token, and no mail text, senders or label names)
- Memory: Luoke Memory, `~/.luoke/mem.db`, shared by Luoke's apps; every entry's evidence can be seen, edited and deleted on the memory page. Passwords, verification codes, full card and ID numbers are never stored; only that string is masked

「设置 → 数据 → 全部清除」 (Settings → Data → Clear all) deletes all of the above; mail in your mailbox is untouched. The memory is kept by default; only when ticked is what Luoke Mail recorded deleted.

## Security design

- The page is served on `127.0.0.1` only. A random token is generated on first start (kept in a file only you can read, so open pages survive a restart of the background service); every API call must carry it, and the Host header is checked, so other websites you visit cannot call the local API (CSRF / DNS rebinding). Only one Luoke Mail runs per data folder; starting it again opens the one already running.
- Notifications show sender and subject, on your computer only; their text is handed to the system as data, never run as a script.
- Mail bodies are always rendered as plain text on the page; HTML and scripts in mail never run.
- Model output is constrained to JSON with fixed fields and fixed choices; the model only suggests and never triggers a mailbox operation directly.
- Writes only move and flag; where the server has no MOVE, it falls back to copy + flag as deleted and expunges only the messages it flagged, never a whole folder. Undo finds mail by Message-ID.

## Running from source and development

For developers and anyone experimenting with their own model service; everyone else, please use Luoke above.

You need macOS (Windows / Linux are being ported), Python 3.9 or later (macOS's own `/usr/bin/python3` will do; standard library only, no third-party dependencies), and an OpenAI-compatible local model service (oMLX, LM Studio, Ollama, llama.cpp). Luoke Memory goes next to this repository:

```bash
git clone https://github.com/luokelabs/luoke-mail.git
git clone https://github.com/luokelabs/luoke-mem.git     # next to luoke-mail
cd luoke-mail
python3 -m luoke_mail                                    # port 8765 by default, opens the browser; --port, --no-browser
```

A page only this computer can reach opens in the browser; the wizard connects your mailbox and a local model (from source you can connect directly to any OpenAI-compatible service; the packaged .app goes through Luoke only). From source, background sorting runs only while the program is open. The data folder can be moved with the `LUOKE_MAIL_HOME` environment variable, which is advisable for experiments.

Tests need no real mailbox or model: they bring a fake IMAP server in Gmail, QQ and NetEase flavours, with writes and undo, plus a fake model service and a fake Luoke:

```bash
python3 -m unittest discover -s tests
(cd ../luoke-mem && python3 -m unittest discover -s tests)
```

The front end is plain ES modules with no build step. A page preview that touches no real data: `python3 tests/ui_harness.py` (fake Gmail, fake model, temporary data folder, port 8799).

### Testing your own model on the same questions

```bash
python3 bench/run_bench.py --base-url http://127.0.0.1:1234/v1 --model <model name>
python3 bench/summarize.py        # summarise into a table
```

There are 48 simulated messages built in (Chinese and English, with phishing and prompt injection; 20 of them hard; added on 2026-10-26: one with a deadline after a long run of tracking links; on 2026-10-10: five with hidden injected instructions; on 2026-10-15: two carrying the provider's sender-domain verdict — a bank's genuine approval notice from its official domain and a lookalike domain that passed verification — all for evaluation only, not in the sample inbox). Each message has its acceptable answers fixed when it was written, and none contains real data. Any OpenAI-compatible local service can be tested.

Scores cover the suggested action, suspicious detection, false alarms, dates (deadlines and event dates), spurious deadlines (a deadline set on mail that needs nothing), priority, the share of valid JSON on the first try, and seconds per message. You are welcome to share results with the Luoke community.

Besides sorting, the clean-up judgement, the check-up's sender judgement and learning about you each have a set of their own (fictional mail as well, each with hard requirements: mail to keep is never suggested for the trash, bank statements and sign-in alerts are never suggested for unsubscribing, someone else's words are never taken as yours, passwords and ID numbers are never recorded):

```bash
python3 bench/run_tasks.py --base-url http://127.0.0.1:1234/v1 --model <model name>
```

### Choosing a model for a direct connection

When running from source against your own model service, these results can guide you (the packaged app's model is chosen by Luoke per memory tier; see Status above).

| Your memory | Recommended model | Fully correct | Per message |
|---|---|---|---|
| 96 GB or more | Qwen3.8 Flash-Next | 45/45 | about 3 s (M5 Max) |
| 48 GB or more | gemma-4-26B-A4B (or Qwen3.6-35B-A3B) | 44/45 (Qwen3.6 older set 38/40) | about 2.5 s (about 2 s) |
| 32 GB | Qwen3.8-27B 4-bit | 40/40, older set | about 8 s (M5 Max) |
| 16 GB, accurate | Ternary Bonsai 2 27B | 39/40, older set | about 29 s (M1 Mac mini) |
| 16 GB, fast | Qwen3.5-9B 4-bit | 39/45, suspicious 12/13 | about 16–22 s (M1 Mac mini) |
| under 16 GB | not recommended | small models that fit (Qwen3.5-4B, gemma-4-E4B) miss too much suspicious mail to meet the hard requirements | |

"Older set" means the 40 questions before the five injection questions were added on 2026-10-10; these models have not been rerun on the new ones.

Notes:

- Sorting runs in the background in batches, so slowness matters little: 20 messages on a 16 GB M1 take about 10 minutes with Bonsai and about 5 with the 9B.
- The 9B's mistakes are mostly taking "when would suit you?" mail for a calendar entry, and missing an instruction injected into a meeting invitation; smaller models miss more scams and injections, so they are no longer recommended (they can still be picked by hand).
- Every action waits for your click, so a wrong judgement never touches your mailbox by itself.
- **This set still saturates on large models** (Flash-Next gets all 45 right; two more models got 40/40 on the older set), so it only separates large, medium and small; and the prompts were improved after seeing these questions' failures, so scores may be optimistic. The five new injection questions widened the gap at 16 GB / 32 GB. Harder questions closer to real inboxes are welcome in `luoke_mail/samples.py`.
- The tool turns off the model's "thinking" mode, so small models do not think forever.

Full results (generated by `python3 bench/summarize.py`):

| Model (engine) | Machine | Hard requirements | Fully correct | Hard questions | Action | Suspicious | False alarms | Dates | Spurious deadlines | Per message |
|---|---|---|---|---|---|---|---|---|---|---|
| gemma-4-26B-A4B 8bit (oMLX) (older set) | M5 Max 128 GB | not run in full | 40/40 | 12/12 | 40/40 | 8/8 | 0/32 | 15/15 | 0/22 | 2.5 s |
| Qwen3.8-Flash-Next-oQ4e (oMLX, M5 Max 128GB, with the scorer resident) | M5 Max 128 GB | pass | 45/45 | 17/17 | 45/45 | 13/13 | 0/32 | 15/15 | 0/27 | 3.0 s |
| Qwen3.8-Flash-Next-oQ4e (oMLX) | M5 Max 128 GB | pass | 45/45 | 17/17 | 45/45 | 13/13 | 0/32 | 15/15 | 0/27 | 3.0 s |
| Qwen3.8-27B-oQ4e (oMLX) (older set) | M5 Max 128 GB | not run in full | 40/40 | 12/12 | 40/40 | 8/8 | 0/32 | 15/15 | 0/22 | 8.5 s |
| gemma-4-26B-A4B 8bit (oMLX, M5 Max 128GB, with the scorer resident) | M5 Max 128 GB | pass | 44/45 | 16/17 | 44/45 | 12/13 | 0/32 | 15/15 | 0/27 | 2.5 s |
| Ternary-Bonsai-2-27B (oMLX) (older set) | M5 Max 128 GB | not run in full | 39/40 | 11/12 | 40/40 | 8/8 | 0/32 | 14/15 | 0/22 | 6.3 s |
| Ternary-Bonsai-2-27B PQ2_0 (llama.cpp, M1 16GB) (older set) | M1 16 GB | not run in full | 39/40 | 11/12 | 40/40 | 8/8 | 0/32 | 14/15 | 0/22 | 28.8 s |
| Qwen3.6-35B-A3B oQ6 (oMLX) (older set) | M5 Max 128 GB | not run in full | 38/40 | 11/12 | 40/40 | 8/8 | 0/32 | 13/15 | 0/22 | 1.9 s |
| Qwen3.5-9B Q4_K_M (llama.cpp) (older set) | M5 Max 128 GB | not run in full | 37/40 | 11/12 | 38/40 | 8/8 | 0/32 | 14/15 | 0/22 | 2.4 s |
| Qwen3.5-9B Q4_K_M (llama.cpp, M1 16GB) (older set) | M1 16 GB | not run in full | 37/40 | 11/12 | 38/40 | 8/8 | 0/32 | 14/15 | 0/22 | 16.2 s |
| Qwen3.5-9B 4bit (oMLX, M4 32GB, with the scorer resident) | M4 32 GB | pass | 39/45 | 13/17 | 40/45 | 12/13 | 0/32 | 14/15 | 3/27 | 10.7 s |
| Qwen3.5-9B 4bit (oMLX, M1 16GB, with the scorer resident) | M1 16 GB | pass | 39/45 | 13/17 | 40/45 | 12/13 | 0/32 | 14/15 | 3/27 | 21.7 s |
| Qwen3.5-9B 8bit (oMLX, M4 32GB, with the scorer resident) | M4 32 GB | fail | 38/45 | 12/17 | 39/45 | 11/13 | 0/32 | 14/15 | 2/27 | 15.8 s |
| gemma-4-12B 4bit (oMLX, M4 32GB, with the scorer resident) | M4 32 GB | fail | 36/45 | 10/17 | 37/45 | 9/13 | 0/32 | 13/15 | 0/27 | 14.2 s |
| gemma-4-E4B 4bit (oMLX) (older set) | M5 Max 128 GB | not run in full | 31/40 | 7/12 | 34/40 | 7/8 | 0/32 | 11/15 | 3/22 | 1.7 s |
| Qwen3.5-4B 4bit (oMLX) (older set) | M5 Max 128 GB | not run in full | 30/40 | 6/12 | 33/40 | 7/8 | 0/32 | 12/15 | 2/22 | 1.7 s |
| gemma-4-E4B 4bit (oMLX, M1 16GB, with the scorer resident) | M1 16 GB | fail | 31/45 | 7/17 | 35/45 | 7/13 | 0/32 | 11/15 | 3/27 | 12.5 s |

45 simulated messages (17 hard), temperature 0; "fully correct" = action, suspicious verdict and dates all right. "Hard requirements" = no legitimate mail flagged as suspicious and at most one suspicious message missed, judged only on a full run. "Older set" results were run before questions were added and are scored on the questions they ran.

Rows marked "with the scorer resident" were measured by luoke-core (2026-10-10, `luoke-core/docs/experiments/tier-bases.md`): the input method's scoring model was resident on the same machine and the model service was capped at 10 GB / 20 GB, so they are slower than a model running alone. Of the five new injection questions, none of these models except Flash-Next caught the one hidden in a meeting invitation (injection-invite); the 16 GB and 32 GB tiers cannot yet catch every suspicious message. The hard requirements (set 2026-10-11) are no false alarms on legitimate mail and at most one suspicious message missed: false alarms make people stop trusting the warnings, while a missed one is still only a suggestion and is never acted on automatically.

## Roadmap

- Reading Luoke's calendar, so the same appointment is not suggested again
- Native password storage on Windows (Credential Manager) and Linux (Secret Service), and background running at login there (today background sorting runs only while the program is open)
- Outlook / Microsoft 365 (needs OAuth)
- English interface

## Documentation

- [docs/DESIGN.md](docs/DESIGN.md): design and rationale (provider differences, undo, clean-up, digest, memory, running through Luoke), in Chinese
- [docs/community/](docs/community/): notes for users and the website (connecting a mailbox, closed-beta notes, features and figures), in Chinese
- [bench/](bench/): the evaluation sets and how to run them (`run_bench.py`, `run_tasks.py`, `summarize.py`)
- How Luoke is divided and decisions across components: `ARCHITECTURE.md` and `docs/adr/` in the top-level repository [luokelabs/luoke](https://github.com/luokelabs/luoke)

## License, security, contributing

- License: Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright © Luoke Labs (螺壳实验室). The 螺壳 / Luoke name, icons and brand assets are not licensed with the code. Versions up to 0.19.8 were released under MIT and remain so.
- Please report security issues privately, see [SECURITY.md](SECURITY.md).
- Issues are welcome; please open one to discuss a change first, see [CONTRIBUTING.md](CONTRIBUTING.md).
