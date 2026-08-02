# IP-Range-Based Site Assignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Assign NetBox sites from device IP addresses via a longest-prefix-match CIDR map, with hostname-keyword matching as fallback.

**Architecture:** `SITE_IP_MAP` env var parsed in config into prefix-sorted networks; `resolve_site(hostname, ip)` in utils replaces `resolve_site_from_name`; four `ensure_*_device` call sites pass the probe IP. Empty map = today's behavior.

**Tech Stack:** Python 3.9+, stdlib `ipaddress`, pytest (reload-based config tests, monkeypatched map tests).

**Spec:** `docs/superpowers/specs/2026-07-29-ip-range-sites-design.md`

## Global Constraints

- Precedence: IP range match → hostname keyword match → `SITE_UNKNOWN`.
- Longest prefix wins: `SITE_IP_MAP` is stored sorted by `prefixlen` descending (stable sort keeps config order on ties); resolution is a plain first-hit loop.
- Malformed map entries (no colon, bad CIDR): WARN at startup, entry skipped — never crash config load.
- Mixed IPv4/IPv6 containment checks raise `TypeError` → treated as no-match for that entry.
- Tests run with `.\.venv\Scripts\python.exe -m pytest tests\ -q` (76 passing at plan time).

---

### Task 1: `SITE_IP_MAP` parsing in config

**Files:**
- Modify: `netbox_sync/config.py`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `config.SITE_IP_MAP: list[tuple[ipaddress.IPv4Network, str]]` sorted by `prefixlen` descending; helper `_parse_site_ip_map(env_value: str|None) -> list`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_netbox_sync.py`)

```python
def test_site_ip_map_parsing_and_sort(monkeypatch):
    import importlib
    monkeypatch.setenv(
        "SITE_IP_MAP",
        "172.31.0.0/16:HQ,172.31.1.0/24:Branch,bad-entry,10.0.0.0/8:Net")
    importlib.reload(cfg)
    assert [(str(n), s) for n, s in cfg.SITE_IP_MAP] == [
        ("172.31.1.0/24", "Branch"),   # /24 beats /16 beats /8 (longest first)
        ("172.31.0.0/16", "HQ"),
        ("10.0.0.0/8", "Net"),
    ]
    monkeypatch.setenv("SITE_IP_MAP", "not-a-cidr:X")
    importlib.reload(cfg)
    assert cfg.SITE_IP_MAP == []       # invalid CIDR skipped, no crash
    monkeypatch.delenv("SITE_IP_MAP", raising=False)
    importlib.reload(cfg)
    assert cfg.SITE_IP_MAP == []       # unset -> empty (backward compatible)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py::test_site_ip_map_parsing_and_sort -q`
Expected: FAIL — `AttributeError: module 'netbox_sync.config' has no attribute 'SITE_IP_MAP'`.

- [ ] **Step 3: Implement** (in `netbox_sync/config.py`)

Add `import ipaddress` to the stdlib imports at the top, then append after the logging section (so `log()` is available at call time):

```python
# ── site assignment by IP range ──────────────────────────────────────────────
def _parse_site_ip_map(env_value):
    """Parse "cidr:Site,cidr:Site2" into [(IPv4Network, site)] sorted by
    prefix length descending (most specific first; stable on ties).
    Malformed entries are skipped with a WARN."""
    pairs = [p.strip() for p in (env_value or "").split(",") if p.strip()]
    out = []
    for pair in pairs:
        if ":" not in pair:
            log("WARN", f"SITE_IP_MAP entry {pair!r} is not 'cidr:Site' — skipped")
            continue
        cidr, site = pair.split(":", 1)
        try:
            out.append((ipaddress.ip_network(cidr.strip(), strict=False),
                        site.strip()))
        except ValueError as exc:
            log("WARN", f"SITE_IP_MAP entry {pair!r} has invalid CIDR ({exc}) — skipped")
    out.sort(key=lambda t: -t[0].prefixlen)
    return out

SITE_IP_MAP = _parse_site_ip_map(os.getenv("SITE_IP_MAP"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass (77).

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/config.py tests/test_netbox_sync.py
git commit -m "Parse SITE_IP_MAP into prefix-sorted networks"
```

---

### Task 2: `resolve_site(hostname, ip)` + call sites

**Files:**
- Modify: `netbox_sync/utils.py`
- Modify: `netbox_sync/netbox.py` (4 call sites + import)
- Test: `tests/test_helpers.py`

**Interfaces:**
- Consumes: `config.SITE_IP_MAP`, `config.SITE_KEYWORD_MAP`, `config.SITE_UNKNOWN`.
- Produces: `resolve_site(hostname: str|None, ip: str|None) -> str`. Replaces `resolve_site_from_name` (removed; all callers updated in this task).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_helpers.py`)

```python
# ── site resolution ──────────────────────────────────────────────────────────

def _site_map(monkeypatch, entries):
    import ipaddress
    monkeypatch.setattr(
        mod, "SITE_IP_MAP",
        [(ipaddress.ip_network(c), s) for c, s in entries])


def test_resolve_site_ip_match_beats_keyword(monkeypatch):
    _site_map(monkeypatch, [("172.31.0.0/16", "HQ")])
    monkeypatch.setattr(mod, "SITE_KEYWORD_MAP", [("sw", "KeywordSite")])
    assert mod.resolve_site("sw-01", "172.31.5.10") == "HQ"


def test_resolve_site_longest_prefix_wins(monkeypatch):
    # list arrives pre-sorted from config (most specific first)
    _site_map(monkeypatch, [("172.31.1.0/24", "Branch"),
                            ("172.31.0.0/16", "HQ")])
    assert mod.resolve_site("x", "172.31.1.55") == "Branch"
    assert mod.resolve_site("x", "172.31.9.55") == "HQ"


def test_resolve_site_falls_back_to_keyword_then_unknown(monkeypatch):
    _site_map(monkeypatch, [("10.0.0.0/8", "Other")])
    monkeypatch.setattr(mod, "SITE_KEYWORD_MAP", [("dc1", "Datacenter1")])
    monkeypatch.setattr(mod, "SITE_UNKNOWN", "Default")
    assert mod.resolve_site("srv-dc1-01", "172.31.1.55") == "Datacenter1"
    assert mod.resolve_site("srv-01", "172.31.1.55") == "Default"


def test_resolve_site_tolerates_bad_ip(monkeypatch):
    _site_map(monkeypatch, [("172.31.0.0/16", "HQ")])
    monkeypatch.setattr(mod, "SITE_UNKNOWN", "Default")
    assert mod.resolve_site("x", "not-an-ip") == "Default"
    assert mod.resolve_site("x", None) == "Default"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_helpers.py -q`
Expected: FAIL — `AttributeError: module 'netbox_sync.utils' has no attribute 'resolve_site'`.

- [ ] **Step 3: Implement**

In `netbox_sync/utils.py`: add `import ipaddress` to imports, extend the config import to include `SITE_IP_MAP`, and replace `resolve_site_from_name` with:

```python
def resolve_site(hostname, ip):
    """Site resolution: IP-range map first (longest-prefix-match — the list
    is pre-sorted most-specific-first), then hostname keyword, then
    SITE_UNKNOWN."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except (ValueError, AttributeError):
        addr = None
    if addr is not None:
        for net, site in SITE_IP_MAP:
            try:
                if addr in net:
                    return site
            except TypeError:
                continue   # mixed IPv4/IPv6 entry — not a match
    name_lower = (hostname or "").lower()
    for keyword, site in SITE_KEYWORD_MAP:
        if keyword in name_lower:
            return site
    return SITE_UNKNOWN
```

In `netbox_sync/netbox.py`: change the utils import to

```python
from netbox_sync.utils import (slugify, normalize_model, resolve_site,
                               _invalid_serial)
```

and replace all four occurrences of

```python
    site_name = resolve_site_from_name(probe.get("hostname") or "")
```

with

```python
    site_name = resolve_site(probe.get("hostname") or "", probe["ip"])
```

(occurs in `ensure_server_device`, `ensure_storage_device`,
`ensure_san_switch_device`, `ensure_cisco_device`.)

- [ ] **Step 4: Run tests to verify they pass + no stale references**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
rg "resolve_site_from_name" netbox_sync tests
```
Expected: all pass (81); grep returns nothing.

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/utils.py netbox_sync/netbox.py tests/test_helpers.py
git commit -m "Resolve sites by IP range with hostname keyword fallback"
```

---

### Task 3: Docs (.env.example + README EN/FA)

**Files:**
- Modify: `.env.example`, `README.md`

- [ ] **Step 1: .env.example** — after the `SITE_KEYWORD_MAP` line add:

```dotenv
# Optional: comma-separated "cidr:SiteName" pairs, checked BEFORE SITE_KEYWORD_MAP.
# Longest prefix wins. e.g. "172.31.0.0/16:HQ,172.31.1.0/24:Branch-F10"
# SITE_IP_MAP=192.0.2.0/27:Site1,198.51.100.0/27:Site2
```

- [ ] **Step 2: README env table (EN)** — after the `SITE_KEYWORD_MAP` row add:

```markdown
| `SITE_IP_MAP` | ❌ | — | Comma-separated `cidr:SiteName` pairs. A device whose IP falls inside the CIDR is assigned that site; **longest prefix wins**. Checked **before** `SITE_KEYWORD_MAP`. e.g. `172.31.0.0/16:HQ,172.31.1.0/24:Branch`. |
```

Also change the `SITE_KEYWORD_MAP` description's first sentence to: "Comma-separated `keyword:SiteName` pairs — used as fallback when no `SITE_IP_MAP` range matches."

- [ ] **Step 3: README env table (FA)** — mirror:

```markdown
| `SITE_IP_MAP` | ❌ | — | جفت‌های `cidr:SiteName` جداشده با کاما. دستگاهی که IP آن داخل CIDR باشد به آن سایت اختصاص می‌یابد؛ **طولانی‌ترین پیشوند برنده است**. **قبل از** `SITE_KEYWORD_MAP` بررسی می‌شود. مثال: `172.31.0.0/16:HQ,172.31.1.0/24:Branch`. |
```

And prepend to the FA `SITE_KEYWORD_MAP` description: «جفت‌های `keyword:SiteName` جداشده با کاما — وقتی استفاده می‌شود که هیچ بازه‌ای در `SITE_IP_MAP` مطابقت نداشته باشد.»

- [ ] **Step 4: Commit**

```bash
git add .env.example README.md
git commit -m "Document SITE_IP_MAP (EN+FA)"
```

---

### Task 4: Verify + push

- [ ] **Step 1: Full verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile netbox_sync\config.py netbox_sync\utils.py netbox_sync\netbox.py
git status --short
```
Expected: 81 tests pass, compile OK, clean tree.

- [ ] **Step 2: Push**

```bash
git push origin main
```
