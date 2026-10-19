#!/usr/bin/env python3
"""site_audit.py: deterministic pre/post-deploy audit for static HTML sites.

Designed to be run by a human or any AI model: all intelligence lives here,
the caller just reads the report. Stdlib only, Python 3.9+.

Usage:
  python3 site_audit.py --repo <repo_root>              # local audit (pre-deploy)
  python3 site_audit.py --repo <repo_root> --live       # audit the deployed site too
  python3 site_audit.py --repo <repo_root> --live --wait 180   # poll until live == local
  python3 site_audit.py --repo <repo_root> --offline    # skip external HTTP checks (fast)
  python3 site_audit.py --repo <repo_root> --init       # generate .site-check.json
  python3 site_audit.py --repo <repo_root> --json       # machine-readable output

Config: <repo_root>/.site-check.json  (see --init). The config is the contract:
expected pages, nav items, required/forbidden strings, OG requirements.

Exit codes: 0 = pass (warnings allowed) · 2 = failures found · 3 = usage/config error
"""
import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from html.parser import HTMLParser

# Two user agents. External link checks use a browser-like UA, so sites that
# screen bots do not block them any harder than they must. Every request to the
# audited site itself carries SELF_UA instead, which contains the word "monitor"
# so the site's own analytics or visitor counter can file audit traffic as a bot.
# Without that split, every --live run would count as one human view per page.
UA = "Mozilla/5.0 (Macintosh) site-audit/1.1 (+https://github.com/elliottstorms/site-audit)"
SELF_UA = "Mozilla/5.0 (compatible; site-audit-monitor/1.1; +https://github.com/elliottstorms/site-audit)"
FAIL, WARN, OK = "FAIL", "WARN", "OK"


class Parser(argparse.ArgumentParser):
    """Usage errors exit 3 (not argparse's default 2, which collides with FAIL)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print("usage error: %s" % message, file=sys.stderr)
        sys.exit(3)


class PageParser(HTMLParser):
    """Collects links, nav anchor labels, meta/OG tags, title, images."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []          # href of <a>
        self.images = []         # (src, alt)
        self.nav_depth = 0
        self.in_nav_a = False
        self._nav_a_text = []
        self.nav_items = []      # visible text of anchors inside <nav>
        self.meta = {}           # property/name -> content
        self.in_title = False
        self.title = ""
        self.favicon = False

    def _flush_nav_a(self):
        text = re.sub(r"\s+", " ", "".join(self._nav_a_text)).strip()
        if text:
            self.nav_items.append(text)
        self.in_nav_a = False
        self._nav_a_text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "nav":
            self.nav_depth += 1
        elif tag == "a":
            if self.in_nav_a:          # unclosed previous <a>: browsers imply-close it
                self._flush_nav_a()
            href = a.get("href")
            if href:
                self.links.append(href)
            if self.nav_depth > 0:
                self.in_nav_a = True
                self._nav_a_text = []
        elif tag == "img":
            self.images.append((a.get("src", ""), a.get("alt")))
        elif tag == "meta":
            key = a.get("property") or a.get("name")
            if key:
                self.meta[key.lower()] = a.get("content", "")
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            if "icon" in rel:
                self.favicon = True
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "nav" and self.nav_depth > 0:
            if self.in_nav_a:
                self._flush_nav_a()
            self.nav_depth -= 1
        elif tag == "a" and self.in_nav_a:
            self._flush_nav_a()
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_nav_a:
            self._nav_a_text.append(data)
        if self.in_title:
            self.title += data


def parse_page(path):
    p = PageParser()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        p.feed(f.read())
    return p


def http_status(url, timeout, ua):
    """Return (status_code_or_None, note). Tries HEAD then GET."""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            if method == "GET":
                return e.code, ""
        except Exception as e:  # noqa: BLE001 - report, don't crash
            if method == "GET":
                return None, str(e)[:120]
    return None, "unreachable"


def fetch_bytes(url, timeout, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ua_for(url, base_url):
    """SELF_UA for a request to the audited site itself (its host, www., or any
    subdomain), UA for everything else. Exact or dot-bounded match, so a lookalike
    such as evil-example.com stays external
    when the site is example.com."""
    def host(u):
        return urllib.parse.urlsplit(u if "//" in u else "//" + u).hostname or ""
    site = host(base_url) if base_url else ""
    if site.startswith("www."):
        site = site[4:]
    h = host(url)
    if site and (h == site or h.endswith("." + site)):
        return SELF_UA
    return UA


def is_internal(href, base_url):
    """None = not checkable · False = external · str = internal path."""
    if href.startswith(("mailto:", "tel:", "javascript:", "data:")):
        return None
    if href.startswith("#"):
        return None
    if href.startswith("//"):
        return False  # protocol-relative -> external
    if href.startswith(("http://", "https://")):
        if base_url and href.startswith(base_url):
            rest = href[len(base_url):]
            if rest == "" or rest[0] in "/?#":  # boundary: reject lookalike domains
                return rest or "/"
        return False
    return href


def resolve_internal(target, site_dir):
    """True iff the internal path maps to a real file INSIDE site_dir.

    Paths that escape site_dir (e.g. ../netlify.toml) exist locally but are
    never deployed, so they must count as broken.
    """
    # Split off fragment/query (literal delimiters in the href) BEFORE decoding,
    # then percent-decode: a browser requests "my file.html" for href="my%20file.html"
    # and the server serves the decoded path, so the filesystem lookup must decode too.
    # Without this, every percent-encoded link to a real file is a false "broken link".
    path = urllib.parse.unquote(target.split("#")[0].split("?")[0])
    if path in ("", "/"):
        path = "index.html"
    path = path.lstrip("/")
    root = os.path.realpath(site_dir)
    candidates = [path]
    if not os.path.splitext(path)[1]:
        candidates += [path + ".html", os.path.join(path, "index.html")]
    for c in candidates:
        fp = os.path.realpath(os.path.join(site_dir, c))
        if not fp.startswith(root + os.sep):
            continue
        if os.path.isfile(fp):
            return True
    return False


def find_pages(site_dir):
    """All .html files under site_dir (recursive), as sorted relative paths."""
    out = []
    for dirpath, _dirs, files in os.walk(site_dir):
        for f in files:
            if f.endswith(".html"):
                out.append(os.path.relpath(os.path.join(dirpath, f), site_dir))
    return sorted(out)


def pdf_page_count(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        n = len(re.findall(rb"/Type\s*/Page[^s]", data))
        return n if n > 0 else None
    except Exception:
        return None


def load_config(repo):
    """Return (cfg, error_message). Validates the contract's required shape."""
    cfg_path = os.path.join(repo, ".site-check.json")
    if not os.path.isfile(cfg_path):
        return None, ("No config at %s; this file is the audit contract. If it should "
                      "exist, restore it (e.g. from git); only run --init for a brand-new site." % cfg_path)
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        return None, "Invalid JSON in %s: %s" % (cfg_path, e)
    for key in ("site_dir", "pages"):
        if key not in cfg:
            return None, "Config %s is missing required key: %r" % (cfg_path, key)
    if not isinstance(cfg["pages"], list) or not cfg["pages"]:
        return None, "Config %s: 'pages' must be a non-empty list" % cfg_path
    site_dir = os.path.join(repo, cfg["site_dir"])
    if not os.path.isdir(site_dir):
        return None, "Config site_dir does not exist: %s" % site_dir
    return cfg, None


def cmd_init(repo, args):
    """Generate .site-check.json from the site's current state."""
    site_dir = os.path.join(repo, args.site_dir)
    if not os.path.isdir(site_dir):
        print("Site dir does not exist: %s" % site_dir)
        return 3
    out = os.path.join(repo, ".site-check.json")
    if os.path.isfile(out) and not args.force:
        print("Refusing to overwrite existing %s; it is the audit contract." % out)
        print("If you really want to regenerate it, pass --force.")
        return 3
    pages = find_pages(site_dir)
    if not pages:
        print("No .html files in %s" % site_dir)
        return 3
    ref = "index.html" if "index.html" in pages else pages[0]
    nav_items = parse_page(os.path.join(site_dir, ref)).nav_items
    cfg = {
        "site_dir": args.site_dir,
        "base_url": args.base_url or "",
        "pages": pages,
        "nav": {"expected_items": nav_items, "required_on_all_pages": True, "exempt_pages": []},
        "required_strings": {"*": []},
        "forbidden_strings": {"*": []},
        "og_required": ["og:title", "og:description", "og:image", "og:url"],
        "external_check": {
            "skip_domains": ["linkedin.com", "x.com", "twitter.com", "instagram.com", "facebook.com"],
            "timeout": 10,
        },
        "pdf_max_pages": {},
    }
    with open(out, "w") as f:
        json.dump(cfg, f, indent=2)
    print("Wrote %s" % out)
    print("Pages: %s" % ", ".join(pages))
    print("Nav items (from %s): %s" % (ref, ", ".join(nav_items) or "(none found)"))
    print("Review it: pin required_strings (canonical URLs) and forbidden_strings (known-bad URLs).")
    return 0


def run_audit(repo, cfg, live, wait, as_json, offline=False):
    site_dir = os.path.join(repo, cfg["site_dir"])
    base_url = (cfg.get("base_url") or "").rstrip("/")
    ec = cfg.get("external_check") or {}
    timeout = ec.get("timeout", 10)
    skip_domains = ec.get("skip_domains", [])
    results = []  # (severity, section, message)

    def add(sev, section, msg):
        results.append((sev, section, msg))

    # ---- pages exist ----
    actual = find_pages(site_dir)
    for p in cfg["pages"]:
        if p not in actual:
            add(FAIL, "pages", "expected page missing: %s" % p)
    for p in actual:
        if p not in cfg["pages"]:
            add(WARN, "pages", "page not in config (new? add it): %s" % p)

    parsed = {}
    for p in cfg["pages"]:
        fp = os.path.join(site_dir, p)
        if os.path.isfile(fp):
            parsed[p] = parse_page(fp)

    # ---- nav parity ----
    nav_cfg = cfg.get("nav") or {}
    expected_nav = nav_cfg.get("expected_items", [])
    nav_exempt = set(nav_cfg.get("exempt_pages", []))
    if expected_nav:
        for p, pp in parsed.items():
            if p in nav_exempt:
                continue
            missing = [i for i in expected_nav if i not in pp.nav_items]
            extra = [i for i in pp.nav_items if i not in expected_nav]
            if missing:
                add(FAIL, "nav", "%s: nav missing item(s): %s" % (p, ", ".join(missing)))
            if extra:
                add(WARN, "nav", "%s: nav has unexpected item(s): %s (update config if intentional)" % (p, ", ".join(extra)))

    # ---- links ----
    externals = {}
    seen_broken = set()
    for p, pp in parsed.items():
        for href in pp.links:
            kind = is_internal(href, base_url)
            if kind is None:
                continue
            if kind is False:
                externals.setdefault(href, []).append(p)
            elif not resolve_internal(kind, site_dir) and (p, href) not in seen_broken:
                seen_broken.add((p, href))
                add(FAIL, "links", "%s: broken internal link: %s" % (p, href))
        for src, _alt in pp.images:
            if not src:
                continue
            kind = is_internal(src, base_url)
            if kind is None:
                continue
            if kind is False:
                externals.setdefault(src, []).append(p)
            elif not resolve_internal(kind, site_dir) and (p, src) not in seen_broken:
                seen_broken.add((p, src))
                add(FAIL, "links", "%s: broken image src: %s" % (p, src))

    checked, skipped = {}, []
    def check_one(url):
        return url, http_status(url, timeout, ua_for(url, base_url))
    to_check = []
    for url in sorted(externals):
        u = url if not url.startswith("//") else "https:" + url
        host = re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
        if any(host == d or host.endswith("." + d) for d in skip_domains):
            skipped.append(url)
        else:
            to_check.append(u)
    if to_check and not offline:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for url, (status, note) in ex.map(check_one, to_check):
                checked[url] = (status, note)
    elif to_check and offline:
        add(WARN, "links", "offline mode: %d external URL(s) not HTTP-checked" % len(to_check))
    for url, (status, note) in sorted(checked.items()):
        pages_str = ",".join(sorted(set(externals.get(url, externals.get(url.replace("https:", "", 1), [])))))
        if status is None:
            add(WARN, "links", "external unreachable (%s): %s [on %s]" % (note, url, pages_str))
        elif status >= 400:
            add(FAIL, "links", "external returns %d: %s [on %s]" % (status, url, pages_str))
    for url in skipped:
        add(WARN, "links", "bot-blocked domain, verify by exact string match instead: %s" % url)

    # ---- OG + head hygiene ----
    for p, pp in parsed.items():
        for tag in cfg.get("og_required", []):
            if not pp.meta.get(tag):
                add(FAIL, "og", "%s: missing %s" % (p, tag))
        og_img = pp.meta.get("og:image", "")
        kind = is_internal(og_img, base_url) if og_img else None
        if kind not in (None, False) and not resolve_internal(kind, site_dir):
            add(FAIL, "og", "%s: og:image points to missing file: %s" % (p, og_img))
        if not pp.title.strip():
            add(FAIL, "head", "%s: empty <title>" % p)
        elif len(pp.title.strip()) > 65:
            add(WARN, "head", "%s: <title> is %d chars (>65 may truncate in search)" % (p, len(pp.title.strip())))
        if not pp.meta.get("description"):
            add(WARN, "head", "%s: missing meta description" % p)
        if not pp.favicon:
            add(WARN, "head", "%s: no favicon <link>" % p)
        for src, alt in pp.images:
            if alt is None:
                add(WARN, "a11y", "%s: <img> missing alt attribute: %s" % (p, src or "(no src)"))

    # ---- required / forbidden strings ----
    def targets_for(key):
        return cfg["pages"] if key == "*" else [key]
    for key, needles in (cfg.get("required_strings") or {}).items():
        for page in targets_for(key):
            fp = os.path.join(site_dir, page)
            if not os.path.isfile(fp):
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                content = f.read()
            for n in needles:
                if n not in content:
                    add(FAIL, "strings", "%s: required string absent: %r" % (page, n))
    # Case-INSENSITIVE, unlike required_strings above. A forbidden string is usually a
    # name or a term that must never ship, and a lowercase spelling sliding past a check
    # that only knows the capitalized one is a silent hole in the one guard whose whole
    # job is catching it. required_strings stay case-sensitive: they are canonical URLs
    # and exact copy, where casing is part of the value.
    # Also unlike required_strings: "*" sweeps EVERY .html under site_dir (the find_pages
    # walk in `actual`, which recurses into subdirs like drafts/), not just registered
    # pages. An unregistered file must not slip past the tripwire by not being in the
    # config yet; registration only gets a WARN elsewhere, and a WARN exits 0.
    for key, needles in (cfg.get("forbidden_strings") or {}).items():
        for page in (actual if key == "*" else [key]):
            fp = os.path.join(site_dir, page)
            if not os.path.isfile(fp):
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                content = f.read().lower()
            for n in needles:
                if n.lower() in content:
                    add(FAIL, "strings", "%s: FORBIDDEN string present: %r" % (page, n))

    # ---- sitemap / robots ----
    sm = os.path.join(site_dir, "sitemap.xml")
    if os.path.isfile(sm):
        with open(sm, encoding="utf-8", errors="replace") as f:
            sm_text = f.read()
        locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", sm_text)
        loc_paths = set()
        for loc in locs:
            path = re.sub(r"^https?://[^/]+", "", loc).strip("/")
            loc_paths.add(path or "index.html")
        for p in cfg["pages"]:
            stem = p[:-5] if p.endswith(".html") else p
            if p not in loc_paths and stem not in loc_paths and not (p == "index.html" and "" in {x.strip("/") for x in loc_paths}):
                add(WARN, "sitemap", "sitemap.xml does not list %s" % p)
    else:
        add(WARN, "sitemap", "no sitemap.xml in site dir")
    if not os.path.isfile(os.path.join(site_dir, "robots.txt")):
        add(WARN, "sitemap", "no robots.txt in site dir")

    # ---- PDFs ----
    for rel, maxp in (cfg.get("pdf_max_pages") or {}).items():
        fp = os.path.join(site_dir, rel)
        if not os.path.isfile(fp):
            add(FAIL, "pdf", "expected PDF missing: %s" % rel)
            continue
        n = pdf_page_count(fp)
        if n is None:
            add(WARN, "pdf", "%s: could not count pages (check manually)" % rel)
        elif n > maxp:
            add(FAIL, "pdf", "%s: %d pages (max %d)" % (rel, n, maxp))

    # ---- live parity ----
    live_status = None
    if live:
        if not base_url:
            add(FAIL, "live", "no base_url in config; cannot run live checks")
        else:
            deadline = time.time() + (wait or 0)
            compared = 0
            while True:
                mismatched = []
                compared = 0
                for p in cfg["pages"]:
                    fp = os.path.join(site_dir, p)
                    if not os.path.isfile(fp):
                        continue
                    url = base_url + "/" + ("" if p == "index.html" else p)
                    try:
                        remote = fetch_bytes(url, timeout, SELF_UA)
                    except Exception as e:  # noqa: BLE001
                        mismatched.append((p, "fetch failed: %s" % str(e)[:100]))
                        continue
                    compared += 1
                    with open(fp, "rb") as f:
                        local = f.read()
                    if remote != local:
                        mismatched.append((p, "live bytes differ from local (%d vs %d bytes)" % (len(remote), len(local))))
                if not mismatched or time.time() >= deadline:
                    break
                time.sleep(10)
            if mismatched:
                for p, why in mismatched:
                    add(FAIL, "live", "%s: %s" % (p, why))
                live_status = "MISMATCH"
            elif compared == 0:
                add(FAIL, "live", "no pages could be compared (all missing locally?)")
                live_status = "NOTHING COMPARED"
            else:
                live_status = "IN SYNC"
                add(OK, "live", "%d page(s) byte-identical to the live site" % compared)

    # ---- report ----
    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    if as_json:
        print(json.dumps({
            "fails": [{"section": s, "msg": m} for _, s, m in fails],
            "warnings": [{"section": s, "msg": m} for _, s, m in warns],
            "live": live_status,
            "pass": not fails,
        }, indent=2))
    else:
        print("=" * 62)
        print("SITE AUDIT: %s  (%d pages)" % (cfg["site_dir"], len(cfg["pages"])))
        print("=" * 62)
        for sev, section, msg in results:
            icon = {"FAIL": "❌", "WARN": "⚠️ ", "OK": "✅"}[sev]
            print("%s [%s] %s" % (icon, section, msg))
        if not results:
            print("✅ no findings at all")
        print("-" * 62)
        print("RESULT: %s  (%d fail, %d warn)%s" % (
            "❌ FAIL" if fails else "✅ PASS", len(fails), len(warns),
            "  · live: %s" % live_status if live_status else ""))
    return 2 if fails else 0


def main():
    ap = Parser()
    ap.add_argument("--repo", required=True, help="repo root containing .site-check.json")
    ap.add_argument("--live", action="store_true", help="also verify the deployed site matches local")
    ap.add_argument("--wait", type=int, default=0, help="with --live: seconds to poll for parity")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true", help="skip external HTTP checks (fast, for git hooks)")
    ap.add_argument("--init", action="store_true", help="generate .site-check.json (refuses to overwrite without --force)")
    ap.add_argument("--force", action="store_true", help="(--init only) allow overwriting an existing config")
    ap.add_argument("--site-dir", default="site", help="(--init only) site dir relative to repo")
    ap.add_argument("--base-url", default="", help="(--init only) production base URL")
    args = ap.parse_args()

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(repo):
        print("Repo path does not exist: %s" % repo)
        sys.exit(3)
    if args.init:
        sys.exit(cmd_init(repo, args))
    cfg, err = load_config(repo)
    if cfg is None:
        print(err)
        sys.exit(3)
    sys.exit(run_audit(repo, cfg, args.live, args.wait, args.json, args.offline))


if __name__ == "__main__":
    main()
