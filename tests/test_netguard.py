"""Addresses someone else wrote (a mail's one-click unsubscribe, a feed link) are fetched only on the public internet,
and redirects are checked hop by hop. Servers here listen on 127.0.0.1 only; names are resolved by a fake."""
import http.server
import ipaddress
import os
import socket
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import actions, netguard, profile, subscriptions  # noqa: E402
from luoke_mail.store import Store  # noqa: E402

FEED = b'<?xml version="1.0"?><rss><channel><item><title>A post title</title><link>/p/1</link></item></channel></rss>'


def fake_dns(table):
    """socket.getaddrinfo answering from `table` ({name: [address, ...]}); unknown names don't resolve."""
    def resolve(host, port, *a, **k):
        if host not in table:
            raise socket.gaierror(8, "nodename nor servname provided")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]
    return unittest.mock.patch.object(netguard.socket, "getaddrinfo", side_effect=resolve)


class Loopback:
    """A small server on 127.0.0.1: `routes` maps a path to (status, headers, body); every path asked is recorded."""

    def __init__(self, test, routes):
        self.asked = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                outer.asked.append(self.path)
                status, headers, body = routes.get(self.path, (404, {}, b"no"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v.replace("{port}", str(outer.port)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        test.addCleanup(self.srv.server_close)
        test.addCleanup(self.srv.shutdown)


class PublicAddressTests(unittest.TestCase):
    def test_which_addresses_are_public(self):
        cgnat = str(ipaddress.IPv4Address((100 << 24) | (64 << 16) | 1))          # the first address of 100.64/10
        for addr in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808", "::808:808"):
            self.assertTrue(netguard.public_ip(addr), addr)
        for addr in ("127.0.0.1", "127.8.9.10", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", cgnat,
                     "::ffff:6440:1", "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1",
                     "fd12::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:192.168.1.1", "64:ff9b::7f00:1", "64:ff9b::a00:1",
                     "fe80::1%en0", "localhost", "", "not an address"):
            self.assertFalse(netguard.public_ip(addr), addr)

    def test_ipv4_compatible_ipv6_counts_as_its_ipv4_address(self):
        for addr in ("::7f00:1", "::127.0.0.1", "::c0a8:101", "::192.168.1.1", "::a00:1", "::a9fe:a9fe"):
            self.assertFalse(netguard.public_ip(addr), addr)
        self.assertTrue(netguard.public_ip("::8.8.8.8"))
        for url in ("https://[::7f00:1]/u", "https://[::127.0.0.1]/u"):
            self.assertFalse(netguard.public_url(url), url)

    def test_fake_ip_range_counts_as_public(self):
        """Clash / Surge in fake-ip mode answer every name with 198.18.x.x: still public, or no address would do."""
        for addr in ("198.18.0.1", "198.18.0.5", "198.19.255.254", "::ffff:198.18.0.5"):
            self.assertTrue(netguard.public_ip(addr), addr)
        for addr in ("198.17.255.255", "198.20.0.1"):                  # just outside: ordinary public addresses
            self.assertTrue(netguard.public_ip(addr), addr)
        with fake_dns({"example.com": ["198.18.0.5"]}):
            self.assertEqual(netguard.url_kind("https://example.com/u"), netguard.PUBLIC)

    def test_a_name_counts_by_every_address_it_resolves_to(self):
        with fake_dns({"news.example": ["93.184.216.34"], "both.example": ["93.184.216.34", "127.0.0.1"],
                       "lan.example": ["192.168.1.20"], "v6.example": ["::1"]}):
            self.assertTrue(netguard.public_host("news.example", 443))
            for host in ("both.example", "lan.example", "v6.example", "gone.example", "", "[::1]", "127.0.0.1"):
                self.assertFalse(netguard.public_host(host, 443), host)
            self.assertTrue(netguard.public_url("https://news.example/u?x=1"))
            self.assertTrue(netguard.public_url("https://8.8.8.8/u"))
            for url in ("https://lan.example/u", "ftp://news.example/u", "https://127.0.0.1:8765/api/state",
                        "https://[::1]/u", "https://news.example:notaport/", "https:///u", "javascript:alert(1)"):
                self.assertFalse(netguard.public_url(url), url)
            self.assertFalse(netguard.public_url("http://news.example/u", schemes=("https",)))
            self.assertEqual(netguard.url_kind("https://gone.example/u"), netguard.UNRESOLVED)
            self.assertEqual(netguard.url_kind("https://lan.example/u"), netguard.NOT_PUBLIC)
            self.assertEqual(netguard.url_kind("ftp://news.example/u"), netguard.NOT_PUBLIC)


class OneClickTests(unittest.TestCase):
    def test_one_click_refuses_this_mac_and_the_local_network_before_connecting(self):
        with fake_dns({"unsub.example": ["192.168.1.1"]}), \
                unittest.mock.patch.object(netguard, "urlopen") as opened:
            for url in ("https://127.0.0.1:8765/api/state", "https://[::1]/u", "https://169.254.169.254/latest",
                        "https://[::7f00:1]/u", "https://unsub.example/u/1"):
                self.assertEqual(subscriptions.one_click(url), (False, subscriptions.NOT_PUBLIC), url)
            # a name that doesn't resolve now (offline): nothing sent, and said like a server that can't be reached
            ok, msg = subscriptions.one_click("https://nowhere.example/u")
            self.assertFalse(ok)
            self.assertTrue(msg.startswith("连不上对方服务器"), msg)
            opened.assert_not_called()

    def test_refused_for_not_public_says_so_and_opens_nothing(self):
        st = Store(os.path.join(tempfile.mkdtemp(), "u.db"))
        self.addCleanup(st.close)
        a = actions.Actions(st, "me@example.com", "gmail", None)
        sub = {"key": "from:news@shop.example", "name": "Shop", "from_addr": "news@shop.example", "method": "one_click",
               "unsub_http": "https://unsub.example/u/1"}
        with fake_dns({"unsub.example": ["10.0.0.7"]}), unittest.mock.patch.object(netguard, "urlopen") as opened:
            with self.assertRaises(actions.ActionError) as e:
                a.unsubscribe(sub)
            opened.assert_not_called()
        self.assertIn("不是公网上的地址", str(e.exception))
        self.assertNotIn("不支持一键退订", str(e.exception))
        self.assertIsNone(st.sender("me@example.com", "from:news@shop.example"))      # not marked as opened
        # offline: as before, the unsubscribe page is opened for the user to finish there
        with fake_dns({}), unittest.mock.patch.object(netguard, "urlopen") as opened:
            r = a.unsubscribe(sub)
            opened.assert_not_called()
        self.assertEqual((r["status"], r["open_url"]), ("unsubscribe_opened", "https://unsub.example/u/1"))

    def test_one_click_works_behind_a_fake_ip_proxy(self):
        sent = []

        class Answer:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout, allow=None):
            sent.append(req.full_url)
            return Answer()
        with fake_dns({"example.com": ["198.18.0.5"]}), unittest.mock.patch.object(netguard, "urlopen", urlopen):
            self.assertEqual(subscriptions.one_click("https://example.com/u"), (True, "对方服务器返回 HTTP 200"))
        self.assertEqual(sent, ["https://example.com/u"])

    def test_one_click_posts_to_a_public_address_and_follows_no_redirect(self):
        sent = []

        class Answer:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout, allow=None):
            sent.append((req.full_url, req.get_method(), req.data, allow))
            return Answer()
        with fake_dns({"unsub.example": ["93.184.216.34"]}), unittest.mock.patch.object(netguard, "urlopen", urlopen):
            self.assertEqual(subscriptions.one_click("https://unsub.example/u/1"), (True, "对方服务器返回 HTTP 200"))
        self.assertEqual(sent, [("https://unsub.example/u/1", "POST", b"List-Unsubscribe=One-Click", None)])


@unittest.mock.patch.dict(os.environ, {"no_proxy": "*"})       # straight to 127.0.0.1, whatever the system proxy says
class RedirectTests(unittest.TestCase):
    def test_redirects_follow_only_where_allowed(self):
        target = Loopback(self, {"/secret": (200, {}, b"secret")})
        hop = Loopback(self, {"/go": (302, {"Location": f"http://127.0.0.1:{target.port}/secret"}, b"")})
        url = f"http://127.0.0.1:{hop.port}/go"
        for allow in (None, netguard.public_url):
            with self.assertRaises(urllib.error.HTTPError) as e:
                netguard.urlopen(urllib.request.Request(url), timeout=5, allow=allow)
            e.exception.close()
            self.assertEqual(e.exception.code, 302)
        self.assertEqual(target.asked, [])
        with netguard.urlopen(urllib.request.Request(url), timeout=5, allow=lambda u: True) as r:
            self.assertEqual(r.read(), b"secret")

    def test_a_feed_that_redirects_off_the_users_site_is_not_followed(self):
        other = Loopback(self, {"/feed.xml": (200, {}, FEED)})
        page = b'<html><head><title>My site</title><link rel="alternate" type="application/rss+xml" href="/feed"></head></html>'
        site = Loopback(self, {"/": (200, {"Content-Type": "text/html; charset=utf-8"}, page),
                               # the user's site at 127.0.0.1: same host is fine, "localhost" is another host on this Mac
                               "/feed": (301, {"Location": "/feed.xml"}, b""),
                               "/feed.xml": (302, {"Location": f"http://localhost:{other.port}/feed.xml"}, b"")})
        info = profile.fetch_site(f"http://127.0.0.1:{site.port}/")
        self.assertEqual(info["title"], "My site")
        self.assertEqual(info["posts"], [])
        self.assertEqual(site.asked, ["/", "/feed", "/feed.xml"])
        self.assertEqual(other.asked, [])

    def test_a_feed_name_that_resolves_to_the_local_network_is_not_read(self):
        with fake_dns({"feeds.example": ["10.1.2.3"], "cdn.example": ["93.184.216.34"]}):
            self.assertFalse(profile._may_read("https://feeds.example/rss", "https://lab.example/"))
            self.assertTrue(profile._may_read("https://cdn.example/rss", "https://lab.example/"))
        self.assertTrue(profile._may_read("http://localhost:4000/feed.xml", "http://localhost:4000/"))     # the user's own


if __name__ == "__main__":
    unittest.main()
