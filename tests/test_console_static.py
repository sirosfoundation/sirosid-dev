"""The console's static files: what is served, how, and that the pages hold no inline code."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import cbor2, httpx, starlette, webauthn  # noqa: F401,E401
    HAVE = True
except ImportError:
    HAVE = False
if HAVE:
    from starlette.testclient import TestClient
    from sirosid_service.api import PAGE_CSP, ApiConfig, create_app
    from sirosid_service.auth import AuthConfig, AuthService
    from test_service import make

CONSOLE = ROOT / "console"
ORIGIN = "https://console.sirosid.dev"


@unittest.skipUnless(HAVE, "needs starlette, httpx, webauthn, cbor2")
class Static(unittest.TestCase):
    def setUp(self):
        cp, _, _ = make()
        app = create_app(cp, AuthService(cp, AuthConfig(origins=(ORIGIN,))), ApiConfig(origins=(ORIGIN,), console_dir=str(CONSOLE)))
        self.http = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)

    def test_pages_and_scripts_are_served_with_the_page_csp(self):
        for path, ctype in (("/", "text/html"), ("/index.html", "text/html"), ("/js/app.js", "text/javascript"),
                            ("/js/container.js", "text/javascript"), ("/css/console.css", "text/css")):
            r = self.http.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertTrue(r.headers["content-type"].startswith(ctype), path)
            self.assertEqual(r.headers["content-security-policy"], PAGE_CSP)
            self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.http.get("/js/app.js").content, (CONSOLE / "js" / "app.js").read_bytes())

    def test_csp_allows_only_self_and_no_inline(self):
        self.assertIn("script-src 'self'", PAGE_CSP)
        self.assertIn("frame-ancestors 'none'", PAGE_CSP)
        self.assertNotIn("unsafe", PAGE_CSP)
        self.assertNotIn("*", PAGE_CSP)

    def test_only_the_console_is_served(self):
        for path in ("/test/container.test.mjs", "/test/xcheck.mjs", "/js/../test/container.test.mjs", "/../CLAUDE.md",
                     "/%2e%2e/CLAUDE.md", "/js/", "/js", "/js/nothing.js", "/index.htm", "/sirosid_service/service.py"):
            r = self.http.get(path)
            self.assertEqual(r.status_code, 404, path)
            self.assertNotIn(b"describe", r.content)

    def test_the_console_never_publishes_a_webauthn_related_origins_file(self):
        """Chrome honours /.well-known/webauthn: a host that serves it lets the listed origins use its host as
        their RP ID. The console is the RP; it must never delegate, and nothing under /.well-known is served."""
        for path in ("/.well-known/webauthn", "/.well-known/", "/.well-known/security.txt"):
            self.assertEqual(self.http.get(path).status_code, 404, path)

    def test_static_is_read_only_and_head_has_no_body(self):
        self.assertEqual(self.http.post("/", headers={"Origin": ORIGIN}).status_code, 405)
        r = self.http.head("/js/app.js")
        self.assertEqual((r.status_code, r.content), (200, b""))

    def test_api_responses_keep_the_locked_down_csp(self):
        r = self.http.get("/healthz")
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])
        self.assertNotIn("script-src", r.headers["content-security-policy"])

    def test_without_a_console_dir_nothing_is_served(self):
        cp, _, _ = make()
        app = create_app(cp, AuthService(cp, AuthConfig(origins=(ORIGIN,))), ApiConfig(origins=(ORIGIN,)))
        self.assertEqual(TestClient(app, base_url=ORIGIN).get("/").status_code, 404)

    def test_a_console_dir_without_index_is_refused(self):
        cp, _, _ = make()
        with self.assertRaises(ValueError):
            create_app(cp, AuthService(cp, AuthConfig(origins=(ORIGIN,))), ApiConfig(origins=(ORIGIN,), console_dir=str(ROOT / "tests")))


class NoInlineCode(unittest.TestCase):
    """Static rules that keep the CSP honest (they need no dependencies)."""

    def test_index_html_has_no_inline_script_style_or_handlers(self):
        html = (CONSOLE / "index.html").read_text()
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S):
            self.assertIn("src=", m.group(1))
            self.assertEqual(m.group(2).strip(), "")
        self.assertNotRegex(html, r"<style\b")
        self.assertNotRegex(html, r"\son[a-z]+\s*=")
        self.assertNotRegex(html, r"\sstyle\s*=")
        self.assertNotRegex(html, r"https?://")

    def test_scripts_never_parse_server_text_as_markup_or_run_it(self):
        for f in (CONSOLE / "js").glob("*.js"):
            src = f.read_text()
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute(\"style\""):
                self.assertNotIn(bad, src, f"{f.name}: {bad}")
            self.assertNotRegex(src, r"""\bstyle\s*:\s*["']""", f.name)             # inline style attributes are blocked by the CSP
            self.assertNotRegex(src, r"https?://[a-z]", f.name) if f.name != "container.js" else None

    def test_polling_cannot_leak_timers_and_null_is_never_rendered(self):
        """Regressions found by the browser run: setInterval re-armed from its own tick made an
        exponential request flood, and a null child rendered as the text 'null'."""
        src = (CONSOLE / "js" / "app.js").read_text()
        self.assertNotIn("setInterval", src)
        self.assertIn("setTimeout", src)
        self.assertRegex(src, r"replaceChildren\(\.\.\.kids\.flat\(\)\.filter")

    def test_the_prf_output_is_never_sent(self):
        src = (CONSOLE / "js" / "app.js").read_text()
        self.assertNotRegex(src, r"credentialToJSON\([^)]*prf", "the PRF output must not be passed into a request")
        self.assertNotIn("localStorage", src)
        self.assertNotIn("sessionStorage", src)


if __name__ == "__main__":
    unittest.main()
