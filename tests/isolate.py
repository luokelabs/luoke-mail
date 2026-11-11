"""Imported first by every test module, before anything imports luoke_mail: tests never reach the user's real data.

luoke_mail reads its data folder (LUOKE_MAIL_HOME; the memory and the page token follow it) when it is first
imported. Each module used to set it itself, so a run that loaded a module without it first (test_hub before
test_server) used ~/.luoke-mail, and a test wrote mailboxes and a model into the real config (2026-10-15).
The place is set here, once; luoke_mail/config.py refuses the real folder under unittest as a backstop.
"""
import os
import sys
import tempfile

if "luoke_mail.config" in sys.modules and not os.environ.get("LUOKE_MAIL_HOME"):
    raise RuntimeError("luoke_mail was imported before tests/isolate.py: import isolate first")
os.environ.setdefault("LUOKE_MAIL_HOME", tempfile.mkdtemp(prefix="luoke-test-"))
os.environ.setdefault("LUOKE_HUB_SOCKET", "/nonexistent/luoke-hub-for-tests.sock")   # never the 螺壳主控 in daily use
os.environ.setdefault("LUOKE_HUB_APPS", "/nonexistent/luoke-hub-apps-for-tests")      # nor its app (memory.hub_roots)
os.environ.setdefault("LUOKE_DOWNLOADS", os.path.join(os.environ["LUOKE_MAIL_HOME"], "Downloads"))  # nor ~/Downloads
