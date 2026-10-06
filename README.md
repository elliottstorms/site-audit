# site-audit

A pre-deploy and post-deploy audit for static HTML sites, in one Python file with no dependencies.

You write down what the site is supposed to look like in a small JSON file (which pages exist, what the nav says, which strings must and must never appear), and `site_audit.py` checks the built site against it. Run it before you push to catch broken links and nav drift, and run it after a deploy to prove the live site is byte-identical to what you built.

```
==============================================================
SITE AUDIT: site  (2 pages)
==============================================================
❌ [nav] about.html: nav missing item(s): Home
❌ [links] about.html: broken internal link: team.html
❌ [og] about.html: missing og:image
❌ [strings] about.html: FORBIDDEN string present: 'lorem ipsum'
--------------------------------------------------------------
RESULT: ❌ FAIL  (4 fail, 0 warn)
```

That is the real output for [`examples/broken`](examples/broken), a copy of the clean fixture with four mistakes planted in one page.

## What it checks

| Section | Fails on | Warns on |
|---|---|---|
| `pages` | a page listed in the config is missing | an `.html` file exists that the config does not list |
| `nav` | a page's `<nav>` is missing an expected item | a page's `<nav>` has an item the config does not expect |
| `links` | an internal link or `<img src>` points at a file that does not exist (including paths that escape the site folder, since those never deploy); an external URL returns 4xx or 5xx | an external URL is unreachable; a link goes to a bot-blocking domain you listed in `skip_domains` |
| `og` | a required Open Graph tag is missing; `og:image` points at a missing local file | |
| `head` | empty `<title>` | `<title>` over 65 characters, no meta description, no favicon link |
| `a11y` | | `<img>` with no `alt` attribute |
| `strings` | a required string is absent; a forbidden string is present | |
| `sitemap` | | `sitemap.xml` or `robots.txt` missing; a page not listed in the sitemap |
| `pdf` | a listed PDF is missing or has more pages than allowed | page count could not be read |
| `live` | (with `--live`) a page's live bytes differ from the local file, or could not be fetched | |

Warnings never change the exit code. Only failures do.

## Install

It needs Python 3.9 or newer and nothing else.

```bash
curl -O https://raw.githubusercontent.com/elliottstorms/site-audit/main/site_audit.py
```

Or clone the repo and run it from there.

## Usage

```bash
python3 site_audit.py --repo .                      # audit the local build
python3 site_audit.py --repo . --offline            # skip external HTTP checks (fast, good for git hooks)
python3 site_audit.py --repo . --live               # also compare every page to the deployed site
python3 site_audit.py --repo . --live --wait 180    # poll up to 180 seconds for the deploy to land
python3 site_audit.py --repo . --json               # machine-readable report
python3 site_audit.py --repo . --init --site-dir public --base-url https://example.com
```

`--repo` is the folder that holds `.site-check.json`. The site itself lives in the subfolder the config names.

Exit codes: `0` pass (warnings allowed), `2` failures found, `3` usage or config error.

### Try it on the fixtures

```bash
python3 site_audit.py --repo examples/basic --offline     # passes, exit 0
python3 site_audit.py --repo examples/broken --offline    # fails, exit 2
```

## The config: `.site-check.json`

The config is the contract. `--init` writes a starting one from the site's current state, and refuses to overwrite an existing config unless you pass `--force`, because regenerating the contract from a broken site would bless the breakage.

This is the config for [`examples/basic`](examples/basic):

```json
{
  "site_dir": "site",
  "base_url": "https://example.com",
  "pages": ["index.html", "about.html"],
  "nav": {
    "expected_items": ["Home", "About"],
    "required_on_all_pages": true,
    "exempt_pages": []
  },
  "required_strings": {
    "*": ["https://example.com/"],
    "about.html": ["<h1>About</h1>"]
  },
  "forbidden_strings": {
    "*": ["lorem ipsum", "staging.example.com"]
  },
  "og_required": ["og:title", "og:description", "og:image", "og:url"],
  "external_check": {
    "skip_domains": ["linkedin.com", "x.com", "twitter.com", "instagram.com", "facebook.com"],
    "timeout": 10
  },
  "pdf_max_pages": {}
}
```

| Key | Meaning |
|---|---|
| `site_dir` | **Required.** The built site, relative to `--repo`. |
| `pages` | **Required.** Every page you expect, as paths relative to `site_dir`. |
| `base_url` | The production URL. Absolute links that start with it are treated as internal, and `--live` fetches from it. |
| `nav.expected_items` | The visible text of every link inside `<nav>`, checked on every page. |
| `nav.exempt_pages` | Pages that skip the nav check (a 404 page, a landing page with its own nav). |
| `nav.required_on_all_pages` | Written by `--init` for readability. The check runs on every page not in `exempt_pages`. |
| `required_strings` | `{"page.html": [...]}` or `{"*": [...]}` for all listed pages. Case-sensitive, because canonical URLs and exact copy are usually case-sensitive too. |
| `forbidden_strings` | Same shape, but **case-insensitive**, and `"*"` sweeps every `.html` file under `site_dir`, including ones you forgot to list in `pages`. A draft page cannot slip past the guard by not being registered yet. |
| `og_required` | Meta tags every page must carry. |
| `external_check.skip_domains` | Domains that block automated checks. They are reported as warnings so you can verify them by exact string match in `required_strings` instead. |
| `external_check.timeout` | Seconds per HTTP request. |
| `pdf_max_pages` | `{"resume.pdf": 1}` fails the audit if that PDF grows past the limit. |

## Design notes

- **Deterministic.** All the judgment lives in the script and the config. A person, a CI job, or an AI agent can run it and act on the exit code without interpreting anything.
- **Polite to your analytics.** Requests to your own site (every `--live` fetch, plus any external link that lands on your domain or a subdomain) send a user agent containing `site-audit-monitor`, so a visitor counter that filters on "monitor" files audit runs as bot traffic instead of inflating your page views. Requests to other sites use a browser-like user agent.
- **Lookalike domains stay external.** If `base_url` is `https://example.com`, a link to `https://example.com.evil.net` is checked as an external URL, not resolved as a local file.
- **Byte-level live parity.** `--live` compares raw bytes, not rendered text, so a CDN that rewrites or caches a stale copy shows up as a mismatch.

## Use it as a git hook

```bash
# .git/hooks/pre-push (make it executable)
#!/bin/sh
exec python3 site_audit.py --repo . --offline
```

## License

MIT. See [LICENSE](LICENSE).
