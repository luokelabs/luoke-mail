# 安全问题

螺壳邮件助手连着你的邮箱、能在里面归档和移到垃圾箱，所以安全问题我们优先处理。下面这些都算：

- 让邮件、授权码、记忆离开本机，或让本机页面的接口被别的网页、别的程序调用；
- 绕过「不可逆的操作要你点了才动」：在你没点的情况下移到垃圾箱、退订、发信、永久删除；
- 邮件里的内容（包括写给 AI 的指令）能让程序执行脚本、连一个新地址，或在你的邮箱里做建议以外的事。

**请不要在公开 issue 里写细节**，用 GitHub 的私密漏洞报告：

1. 打开本仓库的 **Security** 页；
2. 点 **Report a vulnerability**，写清楚版本（页面左上角名字旁，或 `luoke_mail/__init__.py`）、邮箱类型、怎么复现、可能的影响。

本仓库还没公开时，报到顶层仓库 [luokelabs/luoke](https://github.com/luokelabs/luoke/security)。我们会尽快回复，修好以后在发布说明里注明。

请不要在报告里附上真实的邮件、授权码或记忆内容；需要样本时用编造的邮件（可以参照 `luoke_mail/samples.py` 里虚构邮件的写法）。

---

**English.** Please report security issues privately via **Security → Report a vulnerability** on this repository (or on [luokelabs/luoke](https://github.com/luokelabs/luoke/security) while this one is private), not in public issues. In scope: data leaving the device, the local page's API being reachable by other sites or programs, irreversible mailbox actions happening without a click, and mail content steering the program beyond a suggestion. Please don't include real mail, passwords or memory contents; use made-up samples.
