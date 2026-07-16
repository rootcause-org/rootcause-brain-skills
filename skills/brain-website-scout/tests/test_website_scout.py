from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "website_scout.py"
SPEC = importlib.util.spec_from_file_location("website_scout", SCRIPT)
assert SPEC and SPEC.loader
scout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scout)


class FakeFirecrawlHTTP:
    def __init__(self):
        self.calls = []
        self.polls = 0

    def json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urllib.parse.urlsplit(url).path
        if path == "/v2/map":
            return {"success": True, "links": ["https://example.com/", {"url": "https://example.com/help"}]}
        if method == "POST" and path == "/v2/batch/scrape":
            return {"success": True, "id": "job-1", "invalidURLs": []}
        if urllib.parse.urlsplit(url).query == "skip=1":
            return {"data": [{"markdown": "# Help", "metadata": {"sourceURL": "https://example.com/help"}}]}
        if path == "/v2/batch/scrape/job-1":
            self.polls += 1
            if self.polls == 1:
                return {"status": "scraping", "completed": 0, "total": 2}
            return {"status": "completed", "creditsUsed": 2, "completed": 2, "total": 2,
                    "data": [{"markdown": "# Home", "metadata": {"sourceURL": "https://example.com/"}}],
                    "next": "/v2/batch/scrape/job-1?skip=1"}
        raise AssertionError((method, url, kwargs))


class DiscoveryHTTP:
    def request(self, method, url, **kwargs):
        responses = {
            "https://example.com/robots.txt": b"Sitemap: https://example.com/sitemap.xml\n",
            "https://example.com/sitemap.xml": b"<?xml version='1.0'?><sitemapindex><sitemap><loc>https://example.com/products.xml</loc></sitemap><sitemap><loc>https://example.com/sitemap_agentic_discovery.xml</loc></sitemap></sitemapindex>",
            "https://example.com/products.xml": b"<?xml version='1.0'?><urlset><url><loc>https://example.com/products/tea</loc></url><url><loc>https://example.com/fr/products/tea</loc></url></urlset>",
            "https://example.com/sitemap_agentic_discovery.xml": b"<?xml version='1.0'?><urlset><url><loc>https://example.com/agents.md</loc></url></urlset>",
            "https://example.com/agents.md": b"Shopify read-only catalog: `https://example.com/products/tea.json` and `https://example.com/.well-known/ucp`",
            "https://example.com/.well-known/ucp": b'{"platform":"shopify"}',
            "https://example.com/collections/all/products.json?limit=250": json.dumps({"products": [
                {"id": 1, "title": "Tea", "handle": "tea", "vendor": "Example", "product_type": "Tea",
                 "tags": ["herbal"], "variants": [{"id": 2}]}
            ]}).encode(),
        }
        if url not in responses:
            raise scout.ScoutError(f"HTTP 404 for {url}")
        kind = "application/xml" if url.endswith(".xml") else ("application/json" if url.endswith("/ucp") else "text/plain")
        return responses[url], {"Content-Type": kind}


class EmptyDiscoveryHTTP:
    def request(self, method, url, **kwargs):
        if url == "https://example.com/robots.txt":
            return b"", {"Content-Type": "text/plain"}
        if url == "https://example.com/sitemap.xml":
            return b"<?xml version='1.0'?><urlset/>", {"Content-Type": "application/xml"}
        raise scout.ScoutError(f"HTTP 404 for {url}")


def public_resolver(host, port, **kwargs):
    return [(scout.socket.AF_INET, scout.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class WebsiteScoutTests(unittest.TestCase):
    def test_pinned_connections_use_validated_ip_without_second_dns_and_preserve_tls_sni(self):
        answers = iter([
            [(scout.socket.AF_INET, scout.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            [(scout.socket.AF_INET, scout.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ])
        resolver = lambda *args, **kwargs: next(answers)
        validator = scout.public_url_validator("example.com", True, resolver)
        pinned = validator("https://example.com/help")
        raw_socket = mock.Mock()
        tls_socket = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = tls_socket
        connection = scout.PinnedHTTPSConnection(
            "example.com", pinned_ips=pinned, context=context, timeout=3)
        with mock.patch.object(scout.socket, "create_connection", return_value=raw_socket) as connect:
            connection.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), 3, None)
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.com")
        self.assertIs(connection.sock, tls_socket)
        with self.assertRaisesRegex(scout.ScoutError, "non-public address"):
            validator("https://example.com/help")

    def test_pinned_https_handler_uses_its_verified_context_on_python_312(self):
        validator = lambda _url: ("93.184.216.34",)
        handler = scout.PinnedHTTPSHandler(validator)
        request = scout.urllib.request.Request("https://example.com/help")
        with mock.patch.object(handler, "do_open", return_value=object()) as do_open:
            handler.https_open(request)
        connection_factory, passed_request = do_open.call_args.args
        self.assertIs(passed_request, request)
        self.assertIs(do_open.call_args.kwargs["context"], handler._context)
        self.assertEqual(connection_factory("example.com").pinned_ips,
                         ("93.184.216.34",))

    def test_redirected_request_gets_its_own_newly_pinned_connection(self):
        returned = iter([("93.184.216.34",), ("93.184.216.35",), ("93.184.216.36",)])
        validated_urls = []
        def validator(url):
            validated_urls.append(url)
            return next(returned)
        handler = scout.PinnedHTTPHandler(validator)
        connections = []
        def fake_do_open(factory, request, **kwargs):
            connection = factory(request.host)
            connections.append(connection)
            return object()
        handler.do_open = fake_do_open
        initial = scout.urllib.request.Request("http://example.com/start")
        handler.http_open(initial)
        redirect_handler = scout.ValidatingRedirectHandler(validator)
        redirected = redirect_handler.redirect_request(
            initial, None, 302, "Found", {}, "http://www.example.com/final")
        handler.http_open(redirected)
        self.assertEqual(connections[0].pinned_ips, ("93.184.216.34",))
        self.assertEqual(connections[1].pinned_ips, ("93.184.216.36",))
        self.assertEqual(validated_urls, ["http://example.com/start", "http://www.example.com/final",
                                          "http://www.example.com/final"])

    def test_site_and_redirect_validation_reject_private_and_cross_site_targets(self):
        private = lambda host, port, **kwargs: [
            (scout.socket.AF_INET, scout.socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        ]
        with self.assertRaisesRegex(scout.ScoutError, "non-public address"):
            scout.normalize_site("127.0.0.1", private)
        calls = []
        def counting_resolver(host, port, **kwargs):
            calls.append(host)
            return public_resolver(host, port, **kwargs)
        validator = scout.public_url_validator("example.com", True, counting_resolver)
        handler = scout.ValidatingRedirectHandler(validator)
        request = scout.urllib.request.Request("https://example.com/start")
        handler.redirect_request(request, None, 302, "Found", {}, "https://www.example.com/one")
        handler.redirect_request(request, None, 302, "Found", {}, "https://www.example.com/two")
        self.assertEqual(calls, ["www.example.com", "www.example.com"],
                         "every redirect hop must re-resolve its target")
        with self.assertRaisesRegex(scout.ScoutError, "outside configured site scope"):
            handler.redirect_request(request, None, 302, "Found", {}, "https://metadata.google.internal/")

    def test_http_retries_rate_limit_with_retry_after(self):
        class Response:
            headers = {"Content-Type": "application/json"}
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self, _): return b'{"success":true}'

        rate_limit = urllib.error.HTTPError("https://example.com", 429, "slow down",
                                            {"Retry-After": "0"}, io.BytesIO(b"slow down"))
        effects = [rate_limit, Response()]
        delays = []
        with mock.patch.object(scout.urllib.request, "urlopen", side_effect=effects):
            raw, _ = scout.HTTP(retries=1, sleep=delays.append).request("GET", "https://example.com")
        self.assertEqual(raw, b'{"success":true}')
        self.assertEqual(delays, [0.0])

    def test_firecrawl_map_batch_poll_and_pagination(self):
        http = FakeFirecrawlHTTP()
        fc = scout.Firecrawl("test-key", http, "https://firecrawl.test")
        links = fc.map("https://example.com", 1000, True)
        self.assertEqual([x["url"] for x in links], ["https://example.com/", "https://example.com/help"])
        pages, summary = fc.batch_scrape([x["url"] for x in links], 0, 2, 4)
        self.assertEqual(len(pages), 2)
        self.assertEqual(summary["credits_used"], 2)
        map_body = http.calls[0][2]["payload"]
        self.assertEqual(map_body["sitemap"], "include")
        self.assertTrue(map_body["ignoreQueryParameters"])

    def test_discovery_recurses_sitemaps_preserves_artifacts_and_compacts_shopify(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            links, manifest = scout.discover("https://example.com", "example.com", out, DiscoveryHTTP(), True,
                                             validator=lambda _url: None)
            urls = {x["url"] for x in links}
            self.assertIn("https://example.com/products/tea", urls)
            self.assertIn("https://example.com/agents.md", urls)
            self.assertFalse(any("`" in url for url in urls))
            self.assertTrue(manifest["shopify_detected"])
            self.assertEqual(json.loads((out / "catalog.json").read_text())["product_count"], 1)
            self.assertGreaterEqual(len(list((out / "discovery").iterdir())), 5)

    def test_discovery_rerun_prunes_stale_files_and_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            scout.discover("https://example.com", "example.com", out, DiscoveryHTTP(), True,
                           validator=lambda _url: None)
            (out / "discovery" / "stale.txt").write_text("stale")
            self.assertTrue((out / "catalog.json").exists())
            scout.discover("https://example.com", "example.com", out, EmptyDiscoveryHTTP(), True,
                           validator=lambda _url: None)
            self.assertFalse((out / "discovery" / "stale.txt").exists())
            self.assertFalse((out / "catalog.json").exists())

    def test_selection_dedupes_locales_but_keeps_required_and_manual(self):
        links = [
            {"url": "https://example.com/products/tea"},
            {"url": "https://example.com/fr/products/tea"},
            {"url": "https://example.com/privacy"},
            {"url": "https://example.com/help"},
            {"url": "https://example.com/blog/story"},
        ]
        manual = {"https://example.com/fr/products/tea"}
        inventory = scout.build_inventory(links, "example.com", True, "", manual, ["*/blog/*"])
        selected = scout.select_pages(inventory, 10)
        urls = {x["url"] for x in selected}
        self.assertIn("https://example.com/privacy", urls)
        self.assertIn("https://example.com/help", urls)
        self.assertIn("https://example.com/fr/products/tea", urls)
        self.assertNotIn("https://example.com/blog/story", urls)
        duplicate = next(x for x in inventory if x["url"] == "https://example.com/fr/products/tea")
        self.assertIsNone(duplicate["locale_duplicate_of"], "manual include must bypass locale dedupe")

    def test_deep_selection_fills_past_family_caps_and_prefers_unique_localized_pages(self):
        links = ([{"url": f"https://example.com/products/item-{i}"} for i in range(60)] +
                 [{"url": f"https://example.com/nl/articles/story-{i}"} for i in range(50)])
        inventory = scout.build_inventory(links, "example.com", True, "", set(), [])
        selected = scout.select_pages(inventory, 100)
        self.assertEqual(len(selected), 100)
        self.assertEqual(sum(x["locale"] == "nl" for x in selected), 50)

    def test_normalize_url_strips_markdown_backticks(self):
        self.assertEqual(scout.normalize_url("`https://example.com/.well-known/ucp`"),
                         "https://example.com/.well-known/ucp")

    def test_apex_and_www_are_one_locale_dedupe_group(self):
        inventory = scout.build_inventory([
            {"url": "https://example.com/help"}, {"url": "https://www.example.com/help"},
        ], "example.com", True, "", set(), [])
        selected = scout.select_pages(inventory, 10)
        self.assertEqual([x["url"] for x in selected], ["https://example.com/help"])

    def test_capture_validates_source_final_status_warnings_content_and_title(self):
        selected = [
            {"url": "https://example.com/help", "family": "support", "title": "Help"},
            {"url": "https://example.com/privacy", "family": "policy_legal"},
            {"url": "https://example.com/terms", "family": "policy_legal"},
            {"url": "https://example.com/contact", "family": "company_contact"},
            {"url": "https://example.com/shipping", "family": "shipping_returns"},
        ]
        pages = [
            {"markdown": "x" * 350, "metadata": {"sourceURL": "https://example.com/help",
             "url": "https://www.example.com/help", "statusCode": 200,
             "title": "Help\nInjected [label]"}},
            {"markdown": "x" * 350, "warning": "blocked", "metadata": {
             "sourceURL": "https://example.com/privacy", "statusCode": 200}},
            {"markdown": "thin", "metadata": {"sourceURL": "https://example.com/terms",
             "statusCode": 200}},
            {"markdown": "x" * 350, "metadata": {"sourceURL": "https://example.com/contact"}},
            {"markdown": "x" * 350, "metadata": {"sourceURL": "https://example.com/shipping",
             "url": "https://evil.example.net/shipping", "statusCode": 200}},
            {"markdown": "x" * 350, "metadata": {"sourceURL": "https://evil.example.net/help",
             "url": "https://example.com/help", "statusCode": 200}},
            {"markdown": "x" * 350, "metadata": {"sourceURL": "https://attacker@example.com/help",
             "url": "https://example.com/help", "statusCode": 200}},
            {"markdown": "x" * 350, "metadata": {"sourceURL": "https://example.com/unrequested",
             "url": "https://example.com/help", "statusCode": 200}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            validator = scout.public_url_validator("example.com", True, public_resolver)
            written, failures, accounting = scout.process_scraped_pages(
                pages, selected, "example.com", True, validator, Path(tmp), 300)
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0]["requested_url"], "https://example.com/help")
            self.assertEqual(written[0]["final_url"], "https://www.example.com/help")
            self.assertEqual(written[0]["title"], "Help Injected [label]")
            self.assertNotIn("\n", written[0]["title"])
            self.assertEqual(scout.markdown_label(written[0]["title"]),
                             "Help Injected \\[label\\]")
            self.assertEqual({x["status"] for x in accounting}, {"written", "rejected"})
            errors = "\n".join(x["error"] for x in failures)
            for expected in ("warning", "content characters", "HTTP status", "unsafe final URL",
                             "not in the requested set", "unsafe reported sourceURL"):
                self.assertIn(expected, errors)

    def test_out_of_scope_manual_and_edited_selection_fail_before_credits(self):
        class NeverHTTP:
            def __init__(self): self.calls = 0
            def json(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("Firecrawl must not be called")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(scout, "check_safe_output"):
            http = NeverHTTP()
            plan_args = SimpleNamespace(
                out=tmp, site="https://example.com", include_url=["https://evil.example.net/private"],
                include_file=[], exclude_url=[], exclude_file=[], include_subdomains=True,
                retries=0, backoff=0, http_timeout=1, map_limit=10, max_pages=10,
                preferred_locale="", api_base="https://firecrawl.test",
            )
            with self.assertRaisesRegex(scout.ScoutError, "outside configured site scope"):
                scout.plan(plan_args, http=http, resolver=public_resolver)
            self.assertEqual(http.calls, 0)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(scout, "check_safe_output"):
            out = Path(tmp)
            (out / "selection.json").write_text(json.dumps({
                "config": {"site": "https://example.com", "host": "example.com",
                           "include_subdomains": True},
                "selected": [{"url": "https://evil.example.net/private", "selected": True}],
            }))
            http = NeverHTTP()
            with self.assertRaisesRegex(scout.ScoutError, "outside configured site scope"):
                scout.scrape(SimpleNamespace(out=tmp), http=http, resolver=public_resolver)
            self.assertEqual(http.calls, 0)

    def test_scrape_clears_stale_capture_before_failed_new_batch(self):
        class FailingHTTP:
            def json(self, *args, **kwargs):
                raise scout.ScoutError("new batch failed")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(scout, "check_safe_output"), \
                mock.patch.object(scout, "firecrawl_key", return_value="test-key"):
            out = Path(tmp)
            (out / "pages").mkdir()
            (out / "pages" / "stale.md").write_text("stale")
            (out / "capture.json").write_text("{}")
            (out / "INDEX.md").write_text("stale")
            (out / "selection.json").write_text(json.dumps({
                "config": {"site": "https://example.com", "host": "example.com",
                           "include_subdomains": True},
                "selected": [{"url": "https://example.com/help", "selected": True}],
            }))
            args = SimpleNamespace(out=tmp, retries=0, backoff=0, http_timeout=1,
                                   api_base="https://firecrawl.test", poll_interval=0,
                                   poll_timeout=1, max_concurrency=1, min_content_chars=300)
            with self.assertRaisesRegex(scout.ScoutError, "new batch failed"):
                scout.scrape(args, http=FailingHTTP(), resolver=public_resolver)
            self.assertFalse((out / "pages").exists())
            self.assertFalse((out / "capture.json").exists())
            self.assertFalse((out / "INDEX.md").exists())


if __name__ == "__main__":
    unittest.main()
