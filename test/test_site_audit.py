#!/usr/bin/env python3
"""Unit tests for site_audit.py's pure logic.

Stdlib only (unittest), matching the tool's no-dependency promise. These cover
the classification and path-resolution functions that the fixture runs in CI
exercise only end-to-end, so a regression in a single branch (a lookalike
domain, a percent-encoded link, an escaping path) is caught in isolation.

Run from the repo root:  python3 -m unittest discover -s test -v
"""
import importlib.util
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "site_audit.py")
_spec = importlib.util.spec_from_file_location("site_audit", _SRC)
sa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sa)


class TestIsInternal(unittest.TestCase):
    def test_non_navigational_schemes_are_none(self):
        for href in ("mailto:contact", "tel:+15551234", "javascript:void(0)",
                     "data:text/plain,hi", "#section"):
            self.assertIsNone(sa.is_internal(href, ""))

    def test_protocol_relative_is_external(self):
        self.assertIs(sa.is_internal("//cdn.example.com/x.js", ""), False)

    def test_absolute_other_host_is_external(self):
        self.assertIs(sa.is_internal("https://other.com/x", "https://example.com"), False)

    def test_absolute_same_host_becomes_path(self):
        self.assertEqual(sa.is_internal("https://example.com/about.html",
                                        "https://example.com"), "/about.html")

    def test_absolute_root_becomes_slash(self):
        self.assertEqual(sa.is_internal("https://example.com", "https://example.com"), "/")

    def test_lookalike_domain_is_not_internal(self):
        # evil-example.com must not be treated as example.com's own path.
        self.assertIs(sa.is_internal("https://example.com.evil.com/x",
                                     "https://example.com"), False)

    def test_relative_href_is_path(self):
        self.assertEqual(sa.is_internal("about.html", "https://example.com"), "about.html")


class TestResolveInternal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = self.tmp.name
        os.makedirs(os.path.join(self.site, "docs"), exist_ok=True)
        for rel in ("index.html", "about.html", "docs/index.html", "my file.html"):
            with open(os.path.join(self.site, rel), "w") as f:
                f.write("<html></html>")

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_file(self):
        self.assertTrue(sa.resolve_internal("about.html", self.site))

    def test_missing_file(self):
        self.assertFalse(sa.resolve_internal("team.html", self.site))

    def test_root_maps_to_index(self):
        self.assertTrue(sa.resolve_internal("/", self.site))

    def test_extensionless_maps_to_html(self):
        self.assertTrue(sa.resolve_internal("about", self.site))

    def test_extensionless_maps_to_dir_index(self):
        self.assertTrue(sa.resolve_internal("docs", self.site))

    def test_query_and_fragment_are_stripped(self):
        self.assertTrue(sa.resolve_internal("about.html?v=2#top", self.site))

    def test_escaping_path_is_broken(self):
        # ../ escapes the site dir; such a file never deploys, so it must be broken.
        outside = os.path.join(self.site, os.pardir, "secret.txt")
        with open(os.path.realpath(outside), "w") as f:
            f.write("x")
        try:
            self.assertFalse(sa.resolve_internal("../secret.txt", self.site))
        finally:
            os.remove(os.path.realpath(outside))

    def test_percent_encoded_link_to_real_file_resolves(self):
        # Regression: href="my%20file.html" points at the real "my file.html".
        # Browsers decode it; the audit must too, or it reports a false broken link.
        self.assertTrue(sa.resolve_internal("my%20file.html", self.site))


class TestUaFor(unittest.TestCase):
    def test_same_host_gets_self_ua(self):
        self.assertEqual(sa.ua_for("https://example.com/x", "https://example.com"), sa.SELF_UA)

    def test_www_prefix_normalized(self):
        self.assertEqual(sa.ua_for("https://example.com/x", "https://www.example.com"), sa.SELF_UA)

    def test_subdomain_gets_self_ua(self):
        self.assertEqual(sa.ua_for("https://blog.example.com/x", "https://example.com"), sa.SELF_UA)

    def test_lookalike_gets_external_ua(self):
        self.assertEqual(sa.ua_for("https://evil-example.com/x", "https://example.com"), sa.UA)

    def test_other_host_gets_external_ua(self):
        self.assertEqual(sa.ua_for("https://github.com/x", "https://example.com"), sa.UA)


class TestPdfPageCount(unittest.TestCase):
    def _write(self, data):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.addCleanup(os.remove, path)
        return path

    def test_counts_pages_excluding_page_tree_root(self):
        # Two /Type /Page objects and one /Type /Pages tree root -> count is 2.
        path = self._write(b"<</Type /Pages>> <</Type /Page>> <</Type /Page>>")
        self.assertEqual(sa.pdf_page_count(path), 2)

    def test_missing_file_is_none(self):
        self.assertIsNone(sa.pdf_page_count("/no/such/file.pdf"))

    def test_no_pages_is_none(self):
        self.assertIsNone(sa.pdf_page_count(self._write(b"not a pdf")))


class TestPageParser(unittest.TestCase):
    def _parse(self, html):
        p = sa.PageParser()
        p.feed(html)
        return p

    def test_nav_items_collected(self):
        p = self._parse("<nav><a href='/'>Home</a><a href='/about'>About</a></nav>")
        self.assertEqual(p.nav_items, ["Home", "About"])

    def test_links_collected_inside_and_outside_nav(self):
        p = self._parse("<nav><a href='/'>Home</a></nav><a href='/x'>X</a>")
        self.assertEqual(p.links, ["/", "/x"])

    def test_unclosed_nav_anchor_is_flushed(self):
        # Browsers imply-close an open <a> when another <a> starts.
        p = self._parse("<nav><a href='/'>Home<a href='/about'>About</a></nav>")
        self.assertEqual(p.nav_items, ["Home", "About"])

    def test_meta_and_og_lowercased(self):
        p = self._parse("<meta property='OG:Title' content='T'>"
                        "<meta name='description' content='D'>")
        self.assertEqual(p.meta.get("og:title"), "T")
        self.assertEqual(p.meta.get("description"), "D")

    def test_title_and_favicon(self):
        p = self._parse("<title>Hello</title><link rel='icon' href='/f.ico'>")
        self.assertEqual(p.title.strip(), "Hello")
        self.assertTrue(p.favicon)

    def test_img_alt_none_vs_present(self):
        p = self._parse("<img src='a.png'><img src='b.png' alt='B'>")
        self.assertEqual(p.images, [("a.png", None), ("b.png", "B")])


if __name__ == "__main__":
    unittest.main()
