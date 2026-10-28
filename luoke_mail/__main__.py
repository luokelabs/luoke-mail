"""python3 -m luoke_mail [--port 8765] [--no-browser]

Also used by the background service and the launcher app: --service, --open, --take-notification; and by
螺壳邮件助手.app (packaging/): --app.
"""
import argparse
import os
import sys

if sys.version_info < (3, 9):
    sys.exit("luoke-mail 需要 Python 3.9 或更新版本。")


def main() -> None:
    ap = argparse.ArgumentParser(prog="luoke-mail", description="螺壳邮件助手：用本机模型整理收件箱")
    ap.add_argument("--port", type=int, default=None, help="端口（默认 8765，被占用时用下一个空闲的；指定了就只用这一个）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    ap.add_argument("--open", action="store_true", help="打开正在运行的螺壳邮件助手页面")
    ap.add_argument("--app", action="store_true", help=argparse.SUPPRESS)                # 螺壳邮件助手.app's entry
    ap.add_argument("--reopen", action="store_true", help=argparse.SUPPRESS)             # the app opened again
    ap.add_argument("--resident", action="store_true", help=argparse.SUPPRESS)           # started by 螺壳主控 at login
    ap.add_argument("--service", action="store_true", help=argparse.SUPPRESS)            # run by the login item
    ap.add_argument("--take-notification", action="store_true", help=argparse.SUPPRESS)  # run by the launcher app
    ap.add_argument("--forget-secrets", action="store_true", help=argparse.SUPPRESS)     # 螺壳主控, before uninstalling
    ap.add_argument("--job", nargs="?", const="", default=None, help=argparse.SUPPRESS)   # 螺壳主控's idle job (name)
    a = ap.parse_args()
    if a.port is None and os.environ.get("LUOKE_MAIL_PORT"):     # for an app started by macOS, which passes no --port
        a.port = int(os.environ["LUOKE_MAIL_PORT"])
    if a.take_notification:            # keep this path light: no server imports
        from .desktop import take_notification
        print(take_notification())
        return
    if a.job is not None:              # 螺壳主控's idle job: some of the idle work, in a process of its own, then exit
        from .server import run_idle_job
        sys.exit(run_idle_job(name=a.job or None))
    if a.forget_secrets:               # the mailboxes' codes leave the keychain before the app and its data go
        from .server import forget_secrets
        print(f"luoke-mail: {forget_secrets()} keychain item(s) removed")
        return
    from .server import open_or_start, open_page, serve
    if os.environ.get("LUOKE_MAIL_STAMP"):    # started detached, stderr in app.log (desktop.start_detached)
        from . import desktop
        desktop.stamp_stderr()
    if a.app and a.resident:           # background sorting, kept running by 螺壳主控: the instance itself
        from . import __version__, config, desktop
        desktop.log_to_file(os.path.join(config.CONFIG_DIR, "service.log"))      # stderr went nowhere (2026-10-20)
        print(f"luoke-mail {__version__} resident", file=sys.stderr)             # the log puts the time on it
        sys.exit(serve(port=a.port or 8765, open_browser=False, service=True, exact=a.port is not None))
    if a.app:                          # opened (again) by the user, or a notification clicked: the page
        sys.exit(open_or_start(open_browser=not a.no_browser, port=a.port))
    if a.open:
        sys.exit(open_page())
    sys.exit(serve(port=a.port or 8765, open_browser=not (a.no_browser or a.service), service=a.service, exact=a.port is not None))


if __name__ == "__main__":
    main()
