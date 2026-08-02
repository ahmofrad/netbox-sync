# Cisco Catalyst Switch Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Cisco Catalyst (IOS / IOS-XE) switches as a fourth device family — device records, component inventory, per-port interfaces, and CDP/LLDP-derived cables in NetBox.

**Architecture:** New `netbox_sync/collectors/cisco.py` mirroring the Brocade collector (probe → ensure device → collect → sync interfaces/inventory → offline detection), SSH via netmiko, cables reconciled with a `netbox-sync:` ownership marker. Family is opt-in (`CISCO_RANGES` empty by default).

**Tech Stack:** Python 3.8+, netmiko (new dep, pure Python over paramiko), pynetbox, pytest.

**Spec:** `docs/superpowers/specs/2026-07-28-cisco-switches-design.md`

## Global Constraints

- Inventory roles are resolved **by name** via `get_or_create_inventory_role()` — never hardcode role IDs.
- `CISCO_USER`/`CISCO_PASS` are NOT added to `REQUIRED_ENV_VARS`; validated only when `CISCO_RANGES` is set.
- `CISCO_RANGES` default is **empty list** (family disabled unless configured) — backward compatibility is mandatory.
- Cables: only cables whose `description` starts with `"netbox-sync:"` are managed (updated/deleted). Manual cables are never touched. Neighbors that don't resolve to existing NetBox interfaces are skipped with a DEBUG log.
- One failing switch must never abort a run (per-switch try/except, existing pattern).
- Tests use the existing fake-pynetbox harness in `tests/test_netbox_sync.py` and run with `.\.venv\Scripts\python.exe -m pytest tests\ -q`.
- Follow existing code style: module docstring, section banner comments, `log()` from config.

---

### Task 1: Config, models map, dependency, .env.example

**Files:**
- Modify: `netbox_sync/config.py`
- Modify: `netbox_sync/models.py`
- Modify: `requirements.txt`
- Modify: `.env.example`
- Test: `tests/test_netbox_sync.py` (add config tests)

**Interfaces:**
- Produces: `config.CISCO_USER: str|None`, `config.CISCO_PASS: str|None`, `config.CISCO_PORT: int` (default 22), `config.CISCO_RANGES: list[str]` (default `[]`), `config.CISCO_ROLE: str` (default `"Switch"`); `models.CISCO_MODEL_MAP: dict`; `_validate_config()` also errors when `CISCO_RANGES` set but creds missing.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_netbox_sync.py`)

```python
# ── Cisco config ─────────────────────────────────────────────────────────────

def test_cisco_ranges_default_empty_and_parse(monkeypatch):
    import importlib
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    importlib.reload(cfg)
    assert cfg.CISCO_RANGES == []
    monkeypatch.setenv("CISCO_RANGES", "192.0.2.0/29, 198.51.100.0/29")
    importlib.reload(cfg)
    assert cfg.CISCO_RANGES == ["192.0.2.0/29", "198.51.100.0/29"]
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    importlib.reload(cfg)


def test_validate_config_requires_cisco_creds_only_when_ranges_set(monkeypatch):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    monkeypatch.delenv("CISCO_USER", raising=False)
    monkeypatch.delenv("CISCO_PASS", raising=False)
    cfg._validate_config()  # no ranges -> no creds needed

    monkeypatch.setenv("CISCO_RANGES", "192.0.2.0/29")
    with pytest.raises(RuntimeError, match="CISCO_USER"):
        cfg._validate_config()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py -q`
Expected: FAIL — `AttributeError: module 'netbox_sync.config' has no attribute 'CISCO_RANGES'` and the validation test does not raise `RuntimeError` mentioning `CISCO_USER`.

- [ ] **Step 3: Implement config changes** (append in `netbox_sync/config.py` after the `SWITCH_*` credentials block)

```python
CISCO_USER = os.getenv("CISCO_USER")
CISCO_PASS = os.getenv("CISCO_PASS")
```

In `_validate_config()`, extend the body:

```python
def _validate_config():
    """Fail fast at startup if required .env variables are missing.
    Kept out of module scope so the modules stay importable for tests."""
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    # Cisco family is opt-in; its creds are required only when ranges are set.
    if os.getenv("CISCO_RANGES") and (not os.getenv("CISCO_USER")
                                      or not os.getenv("CISCO_PASS")):
        missing.append("CISCO_USER/CISCO_PASS (required when CISCO_RANGES is set)")
    if missing:
        raise RuntimeError(f"Missing required .env variables: {', '.join(missing)}")
```

Append after the `SAN_RANGES` block:

```python
# Cisco family is opt-in: empty default means "disabled".
CISCO_RANGES = []
if os.getenv("CISCO_RANGES"):
    CISCO_RANGES = [r.strip() for r in os.getenv("CISCO_RANGES").split(",") if r.strip()]
```

Append after the role-name constants:

```python
CISCO_PORT    = int(os.getenv("CISCO_PORT", "22"))
CISCO_ROLE    = os.getenv("DEFAULT_CISCO_ROLE", "Switch")
```

- [ ] **Step 4: Add the model map** (append to `netbox_sync/models.py`)

```python
# ── Cisco Catalyst switches ──────────────────────────────────────────────────
# Keys: raw `show version` model string, lowercased. Cisco PIDs are nearly
# canonical already; keep aliases for common reporting variants.
CISCO_MODEL_MAP = {
    "ws-c2960x-48fps-l": "Cisco WS-C2960X-48FPS-L",
    "ws-c2960x-24ps-l":  "Cisco WS-C2960X-24PS-L",
    "c9300-48u":         "Cisco C9300-48U",
    "c9300-48p":         "Cisco C9300-48P",
    "c9300-24t":         "Cisco C9300-24T",
    "c9200l-48p-4g":     "Cisco C9200L-48P-4G",
    "c9200-48p":         "Cisco C9200-48P",
    "c3850-48p":         "Cisco WS-C3850-48P",
    "ws-c3850-48p":      "Cisco WS-C3850-48P",
}
```

Also update the models.py docstring import example line to include `CISCO_MODEL_MAP`.

- [ ] **Step 5: Add dependency + env example**

Append to `requirements.txt`:

```
netmiko>=4.3.0
```

Run: `.\.venv\Scripts\python.exe -m pip install --quiet netmiko`

Append to `.env.example` (after the SAN switch block):

```dotenv
# Cisco Catalyst (IOS/IOS-XE) SSH credentials — required only when CISCO_RANGES is set
CISCO_USER=changeme
CISCO_PASS=changeme
CISCO_PORT=22

# Comma-separated CIDR ranges to scan for Cisco switches (empty = family disabled)
# CISCO_RANGES=192.0.2.64/29
DEFAULT_CISCO_ROLE=Switch
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: 55+ passed (2 new).

- [ ] **Step 7: Commit**

```bash
git add netbox_sync/config.py netbox_sync/models.py requirements.txt .env.example tests/test_netbox_sync.py
git commit -m "Add Cisco family config, model map, netmiko dependency"
```

---

### Task 2: Cisco CLI parsers + fixtures

**Files:**
- Create: `netbox_sync/collectors/cisco.py` (parsers only in this task)
- Test: `tests/test_cisco_parsers.py` (new)

**Interfaces:**
- Produces: `_parse_show_version(text) -> {"hostname","model","serial","ios_version"}`; `_parse_show_inventory(text) -> [{"name","descr","pid","vid","sn"}]`; `_parse_interfaces_status(text) -> [{"port","name","status","vlan","duplex","speed","type"}]`; `_parse_cdp_detail(text) -> [{"device_id","platform","local_intf","remote_intf","ip"}]`; `_parse_lldp_detail(text) -> same shape`; `_short_intf(name) -> str`; `_eth_interface_type(speed, type_str) -> str`.

- [ ] **Step 1: Write the failing test file** `tests/test_cisco_parsers.py`

```python
"""Tests for the Cisco IOS / IOS-XE CLI output parsers."""
import netbox_sync.collectors.cisco as mod


SHOW_VERSION_IOSXE = """Cisco IOS Software [Fuji], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 16.9.4, RELEASE SOFTWARE (fc2)
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2019 by Cisco Systems, Inc.

SW1 uptime is 12 weeks, 3 days, 4 hours, 22 minutes
System returned to ROM by Reload Command
System image file is "flash:cat9k_iosxe.16.09.04.SPA.bin"

cisco C9300-48U (X86) processor with 1316432K/6147K bytes of memory.
Processor board ID FOC2345X0AB

Model Number                       : C9300-48U
System Serial Number               : FOC2345X0AB
"""

SHOW_VERSION_CLASSIC = """Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(2)E7, RELEASE SOFTWARE (fc3)
Technical Support: http://www.cisco.com/techsupport

SW2 uptime is 30 weeks, 2 days, 1 hour, 5 minutes
System returned to ROM by power-on

cisco WS-C2960X-48FPS-L (APM86XXX) processor (revision B0) with 524288K bytes of memory.
Processor board ID FOC98765432
"""


def test_parse_show_version_iosxe():
    out = mod._parse_show_version(SHOW_VERSION_IOSXE)
    assert out["hostname"] == "SW1"
    assert out["model"] == "C9300-48U"
    assert out["serial"] == "FOC2345X0AB"
    assert out["ios_version"] == "16.9.4"


def test_parse_show_version_classic_ios():
    out = mod._parse_show_version(SHOW_VERSION_CLASSIC)
    assert out["hostname"] == "SW2"
    assert out["model"] == "WS-C2960X-48FPS-L"
    assert out["serial"] == "FOC98765432"
    assert out["ios_version"] == "15.2(2)E7"


SHOW_INVENTORY = """NAME: "Switch 1", DESCR: "C9300-48U"
PID: C9300-48U         , VID: V02  , SN: FOC2345X0AB

NAME: "Power Supply Module 0", DESCR: "350W AC Power Supply"
PID: PWR-C1-350WAC     , VID: V01  , SN: LIT23456789

NAME: "Fan Tray 0", DESCR: "Fan Tray"
PID: C9300-FAN-1       , VID: V01  , SN:

NAME: "GigabitEthernet1/1/1", DESCR: "1000BaseSX SFP"
PID: GLC-SX-MMD        , VID: V01  , SN: FNS12345678
"""


def test_parse_show_inventory():
    rows = mod._parse_show_inventory(SHOW_INVENTORY)
    assert len(rows) == 4
    assert rows[0]["pid"] == "C9300-48U"
    assert rows[0]["sn"] == "FOC2345X0AB"
    assert rows[1]["name"] == "Power Supply Module 0"
    assert rows[2]["pid"] == "C9300-FAN-1"
    assert rows[2]["sn"] is None
    assert rows[3]["descr"] == "1000BaseSX SFP"


INTERFACES_STATUS = """Port      Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1   Uplink to SW2      connected    trunk        full   1000 1000BaseSX SFP
Gi1/0/2   Server-01          connected    100        a-full a-1000 10/100/1000BaseTX
Gi1/0/3                        notconnect   1            auto   auto 10/100/1000BaseTX
Gi1/0/4                        disabled     1            auto   auto 10/100/1000BaseTX
Te1/1/1                        connected    trunk        full    10G SFP-10GBase-SR
"""


def test_parse_interfaces_status():
    ports = mod._parse_interfaces_status(INTERFACES_STATUS)
    assert len(ports) == 5
    p0 = ports[0]
    assert p0["port"] == "Gi1/0/1"
    assert p0["name"] == "Uplink to SW2"
    assert p0["status"] == "connected"
    assert p0["vlan"] == "trunk"
    assert p0["speed"] == "1000"
    assert p0["type"] == "1000BaseSX SFP"
    assert ports[2]["name"] == ""
    assert ports[2]["status"] == "notconnect"
    assert ports[4]["port"] == "Te1/1/1"
    assert ports[4]["speed"] == "10G"


CDP_DETAIL = """-------------------------
Device ID: SW2
Entry address(es):
  IP address: 10.0.0.2
Platform: cisco WS-C2960X-48FPS-L,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/1,  Port ID (outgoing port): GigabitEthernet1/0/24
Holdtime : 157 sec

-------------------------
Device ID: SW3.example.com
Entry address(es):
  IP address: 10.0.0.3
Platform: cisco C9300-48U,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/2,  Port ID (outgoing port): TenGigabitEthernet1/1/1
Holdtime : 164 sec
"""


def test_parse_cdp_detail():
    entries = mod._parse_cdp_detail(CDP_DETAIL)
    assert len(entries) == 2
    e0 = entries[0]
    assert e0["device_id"] == "SW2"
    assert e0["platform"] == "cisco WS-C2960X-48FPS-L"
    assert e0["local_intf"] == "GigabitEthernet1/0/1"
    assert e0["remote_intf"] == "GigabitEthernet1/0/24"
    assert e0["ip"] == "10.0.0.2"
    assert entries[1]["device_id"] == "SW3.example.com"


LLDP_DETAIL = """------------------------------------------------
Local Intf: Gi1/0/1
Chassis id: 001c.73ab.cd00
Port id: Gi1/0/24
Port Description: GigabitEthernet1/0/24
System Name: SW2

System Description:
Cisco IOS Software, C2960X Software
"""


def test_parse_lldp_detail():
    entries = mod._parse_lldp_detail(LLDP_DETAIL)
    assert len(entries) == 1
    assert entries[0]["device_id"] == "SW2"
    assert entries[0]["local_intf"] == "Gi1/0/1"
    assert entries[0]["remote_intf"] == "Gi1/0/24"


def test_short_intf():
    assert mod._short_intf("GigabitEthernet1/0/1") == "Gi1/0/1"
    assert mod._short_intf("TenGigabitEthernet1/1/1") == "Te1/1/1"
    assert mod._short_intf("FastEthernet0/1") == "Fa0/1"
    assert mod._short_intf("Port-channel1") == "Po1"
    assert mod._short_intf("Gi1/0/1") == "Gi1/0/1"


def test_eth_interface_type():
    assert mod._eth_interface_type("100", "10/100BaseTX") == "100base-tx"
    assert mod._eth_interface_type("a-1000", "10/100/1000BaseTX") == "1000base-t"
    assert mod._eth_interface_type("1000", "1000BaseSX SFP") == "1000base-x-sfp"
    assert mod._eth_interface_type("10G", "SFP-10GBase-SR") == "10gbase-x-sfpp"
    assert mod._eth_interface_type("10G", "10GBase-T") == "10gbase-t"
    assert mod._eth_interface_type("auto", "10/100/1000BaseTX") == "other"
    assert mod._eth_interface_type("", "") == "other"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cisco_parsers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'netbox_sync.collectors.cisco'`.

- [ ] **Step 3: Implement the parsers** — create `netbox_sync/collectors/cisco.py`

```python
"""Cisco Catalyst (IOS / IOS-XE) switches: netmiko SSH session, CLI output
parsers, probing, inventory collection, interface sync and CDP/LLDP cable
reconciliation."""
import re
import time

from netmiko import ConnectHandler

from netbox_sync.config import (CISCO_USER, CISCO_PASS, CISCO_PORT, log)
from netbox_sync.models import CISCO_MODEL_MAP
from netbox_sync.netbox import get_netbox, get_or_create_inventory_role
from netbox_sync.utils import (normalize_model, _invalid_serial,
                               _make_add_item, is_port_open)

# ── CLI output parsers ───────────────────────────────────────────────────────

def _parse_show_version(text):
    """Parse `show version` for both classic IOS and IOS-XE dialects."""
    out = {"hostname": None, "model": None, "serial": None, "ios_version": None}
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^(\S+)\s+uptime is\b', s, re.IGNORECASE)
        if m and not out["hostname"]:
            out["hostname"] = m.group(1)
        m = re.search(r'Version\s+(\S+?)(?:,|\s+RELEASE)', s)
        if m and not out["ios_version"]:
            out["ios_version"] = m.group(1)
        m = re.match(r'^cisco\s+(\S+)\s+\(', s, re.IGNORECASE)
        if m and not out["model"]:
            out["model"] = m.group(1)
        m = re.match(r'^Model\s+(?:number|Number)\s*:\s*(\S+)', s)
        if m and not out["model"]:
            out["model"] = m.group(1)
        m = re.match(r'^Processor board ID\s+(\S+)', s)
        if m and not out["serial"]:
            out["serial"] = m.group(1)
        m = re.match(r'^System Serial Number\s*:\s*(\S+)', s)
        if m and not out["serial"]:
            out["serial"] = m.group(1)
    return out

def _parse_show_inventory(text):
    """Parse `show inventory` NAME/DESCR + PID/VID/SN line pairs."""
    rows = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^NAME:\s*"([^"]*)",\s*DESCR:\s*"([^"]*)"', s, re.IGNORECASE)
        if m:
            if cur: rows.append(cur)
            cur = {"name": m.group(1), "descr": m.group(2),
                   "pid": None, "vid": None, "sn": None}
            continue
        m = re.match(r'^PID:\s*([^,]*),\s*VID:\s*([^,]*),\s*SN:\s*(.*)$', s, re.IGNORECASE)
        if m and cur is not None:
            cur["pid"] = m.group(1).strip() or None
            cur["vid"] = m.group(2).strip() or None
            cur["sn"]  = m.group(3).strip() or None
    if cur: rows.append(cur)
    return rows

_INTF_STATUS_RE = re.compile(
    r'^(\S+)\s+(.*?)\s+'
    r'(connected|notconnect|disabled|err-disabled|inactive|monitoring|suspended)\s+'
    r'(\S+)\s+(\S+)\s+(\S+)\s+(.*)$', re.IGNORECASE)

def _parse_interfaces_status(text):
    """Parse the fixed-width `show interfaces status` table. The Name column
    is optional and free-form, so the row is anchored on the status keyword."""
    ports = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip(): continue
        if re.match(r'^\s*Port\s+Name\s+Status\s+Vlan\s+Duplex\s+Speed\s+Type\s*$',
                    s, re.IGNORECASE):
            continue
        m = _INTF_STATUS_RE.match(s)
        if not m: continue
        ports.append({
            "port":    m.group(1),
            "name":    m.group(2).strip(),
            "status":  m.group(3),
            "vlan":    m.group(4),
            "duplex":  m.group(5),
            "speed":   m.group(6),
            "type":    m.group(7).strip(),
        })
    return ports

_INTF_PREFIXES = (("TwentyFiveGigE", "Twe"), ("FortyGigabitEthernet", "Fo"),
                  ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
                  ("FastEthernet", "Fa"), ("HundredGigE", "Hu"),
                  ("Port-channel", "Po"), ("Ethernet", "Eth"))

def _short_intf(name):
    """GigabitEthernet1/0/1 -> Gi1/0/1 (CDP/LLDP use long names, the
    interfaces-status table uses short ones)."""
    n = (name or "").strip()
    for long, short in _INTF_PREFIXES:
        if n.startswith(long):
            return short + n[len(long):]
    return n

def _normalize_cdp_id(device_id):
    """CDP device IDs may carry a domain suffix (SW2.example.com) — strip it."""
    return (device_id or "").split(".")[0].strip()

def _parse_cdp_detail(text):
    """Parse `show cdp neighbors detail` into per-entry dicts."""
    entries = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("---"):
            if cur and cur.get("local_intf"): entries.append(cur)
            cur = None
            continue
        m = re.match(r'^Device ID:\s*(\S+)', s, re.IGNORECASE)
        if m:
            if cur and cur.get("local_intf"): entries.append(cur)
            cur = {"device_id": m.group(1), "platform": "",
                   "local_intf": None, "remote_intf": None, "ip": None}
            continue
        if cur is None: continue
        m = re.match(r'^IP address:\s*(\S+)', s, re.IGNORECASE)
        if m and not cur["ip"]:
            cur["ip"] = m.group(1); continue
        m = re.match(r'^Platform:\s*([^,]+),', s, re.IGNORECASE)
        if m:
            cur["platform"] = m.group(1).strip(); continue
        m = re.match(r'^Interface:\s*(\S+?),\s*Port ID \(outgoing port\):\s*(\S+)',
                     s, re.IGNORECASE)
        if m:
            cur["local_intf"], cur["remote_intf"] = m.group(1), m.group(2)
            continue
    if cur and cur.get("local_intf"): entries.append(cur)
    return entries

def _parse_lldp_detail(text):
    """Parse `show lldp neighbors detail` into the same shape as CDP.
    Only used as a fallback when CDP yields no entries."""
    entries = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("---"):
            if cur and (cur.get("local_intf") or cur.get("device_id")):
                entries.append(cur)
            cur = None
            continue
        m = re.match(r'^Local Intf:\s*(\S+)', s, re.IGNORECASE)
        if m:
            if cur and (cur.get("local_intf") or cur.get("device_id")):
                entries.append(cur)
            cur = {"device_id": None, "platform": "",
                   "local_intf": m.group(1), "remote_intf": None, "ip": None}
            continue
        if cur is None: continue
        m = re.match(r'^System Name:\s*(.+)$', s, re.IGNORECASE)
        if m:
            cur["device_id"] = m.group(1).strip(); continue
        m = re.match(r'^Port id:\s*(\S+)', s, re.IGNORECASE)
        if m:
            cur["remote_intf"] = m.group(1); continue
    if cur and (cur.get("local_intf") or cur.get("device_id")):
        entries.append(cur)
    return [e for e in entries if e.get("device_id")]

def _eth_interface_type(speed, type_str=None):
    """Map interfaces-status speed/type to a NetBox interface type choice.
    Modular (SFP) ports map to the -x- types; unknown/auto -> 'other'."""
    s = (speed or "").lower().replace("a-", "").strip()
    t = (type_str or "").lower()
    sfpish = any(k in t for k in ("sfp", "gbic", "basesx", "baselx",
                                  "basesr", "baselr", "basezx"))
    if s == "100":   return "100base-tx"
    if s == "1000":  return "1000base-x-sfp" if sfpish else "1000base-t"
    if s in ("10g", "10000"):
        return "10gbase-x-sfpp" if sfpish else "10gbase-t"
    if s == "25g":   return "25gbase-x-sfp28"
    if s == "40g":   return "40gbase-x-qsfpp"
    if s == "100g":  return "100gbase-x-qsfp28"
    return "other"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cisco_parsers.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/collectors/cisco.py tests/test_cisco_parsers.py
git commit -m "Add Cisco IOS/IOS-XE CLI parsers with fixtures"
```

---

### Task 3: Session, probe, and inventory collection

**Files:**
- Modify: `netbox_sync/collectors/cisco.py` (append session + probe + collect)
- Test: `tests/test_netbox_sync.py` (inventory role classification)

**Interfaces:**
- Consumes: Task 2 parsers; `config.CISCO_USER/PASS/PORT`; `netbox.get_or_create_inventory_role`; `utils._make_add_item/_invalid_serial/is_port_open/normalize_model`; `models.CISCO_MODEL_MAP`.
- Produces: `CiscoSwitchSession` (`login()`, `run(cmd) -> str`, `logout()`); `probe_cisco_switch(ip, retries=2, retry_delay=3) -> dict|None` with keys `{ip, host, serial, model, hostname, manufacturer, firmware}`; `cisco_collect_inventory(ip) -> {"summary": {"serial","model","firmware","hostname","port_count"}, "ports": [...], "neighbors": [...], "inventory": {serial: item}}`; `_inventory_item_from_row(row, add_item) -> None`.

- [ ] **Step 1: Write the failing test** — two edits in `tests/test_netbox_sync.py`.

First, replace the existing `_roles_endpoint()` helper (it currently defines ids 42–46) with an extended version that adds the roles Cisco uses:

```python
def _roles_endpoint():
    return FakeEndpoint([
        FakeRecord(42, name="HDD", slug="hdd"),
        FakeRecord(43, name="SSD", slug="ssd"),
        FakeRecord(44, name="PSU", slug="psu"),
        FakeRecord(45, name="Controller", slug="controller"),
        FakeRecord(46, name="SAS Exp", slug="sas-exp"),
        FakeRecord(47, name="SFP", slug="sfp"),
        FakeRecord(48, name="Fan", slug="fan"),
        FakeRecord(49, name="Module", slug="module"),
    ])
```

Then append the new test:

```python
# ── Cisco inventory role classification ──────────────────────────────────────

def test_cisco_inventory_roles_classified(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ep = _roles_endpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))

    inv = {}
    add = utils._make_add_item(inv)
    cisco._inventory_item_from_row(
        {"name": "Power Supply Module 0", "descr": "350W AC Power Supply",
         "pid": "PWR-C1-350WAC", "vid": "V01", "sn": "LIT23456789"}, add)
    cisco._inventory_item_from_row(
        {"name": "Fan Tray 0", "descr": "Fan Tray",
         "pid": "C9300-FAN-1", "vid": "V01", "sn": "FAN123456"}, add)
    cisco._inventory_item_from_row(
        {"name": "GigabitEthernet1/1/1", "descr": "1000BaseSX SFP",
         "pid": "GLC-SX-MMD", "vid": "V01", "sn": "FNS12345678"}, add)
    cisco._inventory_item_from_row(
        {"name": "Switch 1", "descr": "C9300-48U",
         "pid": "C9300-48U", "vid": "V02", "sn": "FOC2345X0AB"}, add)

    assert inv["LIT23456789"]["role"] == 44   # PSU
    assert inv["FAN123456"]["role"] == 48     # Fan
    assert inv["FNS12345678"]["role"] == 47   # SFP
    assert inv["FOC2345X0AB"]["role"] == 49   # Module
    assert inv["LIT23456789"]["part_number"] == "PWR-C1-350WAC"
    assert ep.created == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py::test_cisco_inventory_roles_classified -q`
Expected: FAIL — `AttributeError: module 'netbox_sync.collectors.cisco' has no attribute '_inventory_item_from_row'`.

- [ ] **Step 3: Implement** (append to `netbox_sync/collectors/cisco.py`)

```python
# ── session (netmiko) ────────────────────────────────────────────────────────

class CiscoSwitchSession:
    """Thin netmiko wrapper for Catalyst IOS/IOS-XE. netmiko owns prompt
    detection, paging (`terminal length 0`) and privilege handling."""

    def __init__(self, ip, port=None, timeout=20):
        self.ip = ip
        self.port = port or CISCO_PORT
        self.timeout = timeout
        self.conn = None

    def login(self):
        self.conn = ConnectHandler(
            device_type="cisco_ios",
            host=self.ip, port=self.port,
            username=CISCO_USER, password=CISCO_PASS,
            conn_timeout=self.timeout, auth_timeout=self.timeout,
            banner_timeout=self.timeout,
        )

    def run(self, command):
        if not self.conn:
            raise RuntimeError("SSH session not open")
        return self.conn.send_command(command, read_timeout=self.timeout)

    def logout(self):
        try:
            if self.conn:
                self.conn.disconnect()
        except Exception: pass
        self.conn = None

# ── probe + inventory collection ─────────────────────────────────────────────

def probe_cisco_switch(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, CISCO_PORT):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        sess = CiscoSwitchSession(ip)
        try:
            sess.login()
            try:
                info = _parse_show_version(sess.run("show version"))
                if not (info.get("serial") or info.get("model")):
                    raise RuntimeError("show version yielded no serial/model")
                model = (normalize_model(info.get("model"), CISCO_MODEL_MAP)
                         or info.get("model"))
                return {
                    "ip":           ip,
                    "host":         f"{ip}:{CISCO_PORT}",
                    "serial":       info.get("serial"),
                    "model":        model,
                    "hostname":     (info.get("hostname")
                                     or f"cisco-{ip.replace('.', '-')}"),
                    "manufacturer": "Cisco",
                    "firmware":     info.get("ios_version"),
                }
            finally:
                sess.logout()
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
    return None

def _inventory_item_from_row(row, add_item):
    """Classify a `show inventory` row into PSU/Fan/SFP/Module and add it."""
    serial = (row.get("sn") or "").strip()
    if _invalid_serial(serial):
        return
    label = f"{row.get('name', '')} {row.get('descr', '')}".lower()
    if "power supply" in label:
        role = "PSU"
    elif "fan" in label:
        role = "Fan"
    elif "sfp" in label or "transceiver" in label or "gbic" in label:
        role = "SFP"
    else:
        role = "Module"
    add_item(
        name=str(row.get("descr") or row.get("name") or "Module")[:64],
        manufacturer="Cisco",
        part_number=row.get("pid") or None,
        serial=serial,
        description=f"Name={row.get('name')} Descr={row.get('descr')} VID={row.get('vid')}",
        role_id=get_or_create_inventory_role(role),
    )

def cisco_collect_inventory(ip):
    """Full inventory pull: identity, inventory rows, ports, CDP/LLDP neighbors."""
    sess = CiscoSwitchSession(ip)
    sess.login()
    try:
        ver = _parse_show_version(sess.run("show version"))
        inv_rows = _parse_show_inventory(sess.run("show inventory"))
        ports = _parse_interfaces_status(sess.run("show interfaces status"))
        log("INFO", f"  cisco show: {len(inv_rows)} inventory rows, {len(ports)} ports")

        try:
            neighbors = _parse_cdp_detail(sess.run("show cdp neighbors detail"))
            log("INFO", f"  cdp neighbors: {len(neighbors)}")
        except Exception as exc:
            neighbors = []
            log("WARN", f"  show cdp neighbors detail failed: {exc}")
        if not neighbors:
            try:
                neighbors = _parse_lldp_detail(sess.run("show lldp neighbors detail"))
                log("INFO", f"  lldp neighbors: {len(neighbors)}")
            except Exception as exc:
                log("WARN", f"  show lldp neighbors detail failed: {exc}")

        inventory = {}
        add_item = _make_add_item(inventory)
        for row in inv_rows:
            _inventory_item_from_row(row, add_item)

        summary = {
            "serial":    ver.get("serial"),
            "model":     (normalize_model(ver.get("model"), CISCO_MODEL_MAP)
                          or ver.get("model")),
            "firmware":  ver.get("ios_version"),
            "hostname":  (ver.get("hostname") or "").strip(),
            "port_count": len(ports),
        }
        return {"summary": summary, "ports": ports,
                "neighbors": neighbors, "inventory": inventory}
    finally:
        sess.logout()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass (58+).

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/collectors/cisco.py tests/test_netbox_sync.py
git commit -m "Add Cisco netmiko session, probe and inventory collection"
```

---

### Task 4: NetBox device layer — ensure_cisco_device + mark_cisco_offline

**Files:**
- Modify: `netbox_sync/netbox.py`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Consumes: `config.CISCO_ROLE`, `models.CISCO_MODEL_MAP`, existing CRUD helpers.
- Produces: `ensure_cisco_device(probe: dict) -> int` (NetBox device id); `mark_cisco_offline(dev_id: int, dev_name: str) -> None`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_netbox_sync.py`)

```python
# ── Cisco device ensure ──────────────────────────────────────────────────────

def test_ensure_cisco_device_creates_with_custom_fields(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device", lambda *a, **k: None)

    dev_id = nbx.ensure_cisco_device({
        "ip": "192.0.2.65", "serial": "FOC2345X0AB", "model": "C9300-48U",
        "hostname": "SW1", "manufacturer": "Cisco", "firmware": "16.9.4",
    })
    assert len(devices_ep.created) == 1
    payload = devices_ep.created[0]
    assert payload["serial"] == "FOC2345X0AB"
    assert payload["status"] == "active"
    assert payload["custom_fields"]["cisco_ip"] == "192.0.2.65"
    assert payload["custom_fields"]["cisco_enabled"] is True
    assert payload["custom_fields"]["cisco_model"] == "C9300-48U"
    assert dev_id is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py::test_ensure_cisco_device_creates_with_custom_fields -q`
Expected: FAIL — `AttributeError: module 'netbox_sync.netbox' has no attribute 'ensure_cisco_device'`.

- [ ] **Step 3: Implement** (in `netbox_sync/netbox.py`)

Update the imports at the top:

```python
from netbox_sync.config import (NETBOX_URL, NETBOX_TOKEN, _env_bool,
                                SERVER_ROLE, STORAGE_ROLE, SWITCH_ROLE,
                                CISCO_ROLE,
                                DEFAULT_MFR, OFFLINE_THRESHOLD, log)
from netbox_sync.models import (SERVER_MODEL_MAP, STORAGE_MODEL_MAP,
                                SWITCH_MODEL_MAP, CISCO_MODEL_MAP)
```

Append after `ensure_san_switch_device` / before `mark_server_offline`:

```python
def ensure_cisco_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "Cisco")
    role_id = get_or_create_role(CISCO_ROLE, "009688")
    site_name = resolve_site_from_name(probe.get("hostname") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, CISCO_MODEL_MAP)
    name = _device_name(probe, prefix="cisco")
    api = get_netbox()
    dev = find_device(serial, role_name=CISCO_ROLE)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = cands[0] if cands else None
        if dev: log("INFO", f"  Found cisco switch by name+site: {name} (id={dev.id})")
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id, "role": role_id,
        "custom_fields": {
            "cisco_ip":       probe["ip"],
            "cisco_enabled":  True,
            "cisco_firmware": probe.get("firmware"),
            "cisco_model":    probe.get("model"),
        },
        **({"serial": serial} if not _invalid_serial(serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  Cisco switch updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  Cisco switch created: {name} (id={new.id})")
    return new.id
```

Append after `mark_san_offline`:

```python
def mark_cisco_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"cisco_enabled": False},
        }])
        log("WARN", f"  Cisco switch marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark Cisco switch offline {dev_name}: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/netbox.py tests/test_netbox_sync.py
git commit -m "Add ensure_cisco_device and mark_cisco_offline"
```

---

### Task 5: Interface sync

**Files:**
- Modify: `netbox_sync/collectors/cisco.py` (append `sync_cisco_interfaces`)
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Consumes: Task 2 `_eth_interface_type`; `netbox.get_netbox`.
- Produces: `sync_cisco_interfaces(dev_id: int, ports: list[dict]) -> None` — update existing by name, create missing, delete stale (mirrors `sync_san_interfaces`).

- [ ] **Step 1: Write the failing test** (append to `tests/test_netbox_sync.py`)

```python
# ── Cisco interface sync ─────────────────────────────────────────────────────

def test_sync_cisco_interfaces_update_create_delete(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="Gi1/0/1", device_id=7),
        FakeRecord(2, name="Gi1/0/9", device_id=7),   # stale -> deleted
    ])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))

    ports = [
        {"port": "Gi1/0/1", "name": "Uplink", "status": "connected",
         "vlan": "trunk", "duplex": "full", "speed": "1000",
         "type": "1000BaseSX SFP"},
        {"port": "Gi1/0/2", "name": "", "status": "notconnect",
         "vlan": "1", "duplex": "auto", "speed": "auto",
         "type": "10/100/1000BaseTX"},
    ]
    cisco.sync_cisco_interfaces(7, ports)

    assert {u["id"] for u in ifaces_ep.updated} == {1}
    assert ifaces_ep.updated[0]["type"] == "1000base-x-sfp"
    assert ifaces_ep.updated[0]["enabled"] is True
    assert len(ifaces_ep.created) == 1
    assert ifaces_ep.created[0]["name"] == "Gi1/0/2"
    assert ifaces_ep.created[0]["type"] == "other"
    assert ifaces_ep.created[0]["enabled"] is False
    assert ifaces_ep.deleted_ids == [2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py::test_sync_cisco_interfaces_update_create_delete -q`
Expected: FAIL — `AttributeError: ... no attribute 'sync_cisco_interfaces'`.

- [ ] **Step 3: Implement** (append to `netbox_sync/collectors/cisco.py`)

```python
# ── interfaces ───────────────────────────────────────────────────────────────

def sync_cisco_interfaces(dev_id, ports):
    """Create/update NetBox interfaces per switchport; delete stale ones.
    Description carries status/vlan/duplex/speed + the port's description."""
    api = get_netbox()
    existing = {}
    for iface in list(api.dcim.interfaces.filter(device_id=dev_id)):
        existing[str(iface.name)] = iface

    seen = set()
    for p in ports:
        name = p["port"]
        seen.add(name)
        desc_parts = [f"status={p.get('status')}", f"vlan={p.get('vlan')}",
                      f"duplex={p.get('duplex')}", f"speed={p.get('speed')}"]
        if p.get("name"):
            desc_parts.append(p["name"])
        payload = {
            "device":     dev_id,
            "name":       name,
            "type":       _eth_interface_type(p.get("speed"), p.get("type")),
            "enabled":    p.get("status", "").lower() == "connected",
            "description": " | ".join(desc_parts)[:200],
            "mgmt_only":  False,
        }
        if name in existing:
            api.dcim.interfaces.update([{"id": existing[name].id, **payload}])
        else:
            try:
                api.dcim.interfaces.create(payload)
            except Exception as e:
                log("WARN", f"  Could not create interface {name}: {e}")

    for name, iface in existing.items():
        if name not in seen:
            try: iface.delete()
            except Exception: pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/collectors/cisco.py tests/test_netbox_sync.py
git commit -m "Add sync_cisco_interfaces"
```

---

### Task 6: CDP/LLDP cable sync

**Files:**
- Modify: `netbox_sync/collectors/cisco.py` (append cable sync)
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Consumes: `_normalize_cdp_id`, `_short_intf` (Task 2); `netbox.get_netbox`.
- Produces: `CABLE_MARKER = "netbox-sync:"`; `sync_cdp_cables(dev_id: int, neighbors: list[dict]) -> None`. Cable create payload: `{"a_terminations": [{"object_type": "dcim.interface", "object_id": int}], "b_terminations": [...], "description": str}`.

Behavior contract:
1. Both ends must resolve (local iface on `dev_id`, peer device by normalized name, peer iface by short name) else skip + DEBUG.
2. Marked cable already on either interface → refresh description (no duplicate).
3. **Unmarked** cable on either interface → leave untouched, DEBUG conflict note.
4. Otherwise create with marker description.
5. After processing: marked cables on this device not seen this run → delete.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_netbox_sync.py`)

```python
# ── Cisco CDP cable sync ─────────────────────────────────────────────────────

def _cisco_cable_api(local_ifaces, peer_dev, peer_ifaces, cables):
    return _fake_api(
        devices=FakeEndpoint([peer_dev] if peer_dev else []),
        interfaces=FakeEndpoint(local_ifaces + peer_ifaces),
        cables=FakeEndpoint(cables),
    )

_PEER = FakeRecord(5, name="SW2")
_LOCAL_IFACE = FakeRecord(11, name="Gi1/0/1", device_id=7)
_PEER_IFACE = FakeRecord(55, name="Gi1/0/24", device_id=5)
_NEIGHBORS = [{"device_id": "SW2", "platform": "", "ip": None,
               "local_intf": "GigabitEthernet1/0/1",
               "remote_intf": "GigabitEthernet1/0/24"}]


def test_cdp_cable_created_when_both_ends_resolve(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert len(api.dcim.cables.created) == 1
    payload = api.dcim.cables.created[0]
    assert payload["a_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 11}]
    assert payload["b_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 55}]
    assert payload["description"].startswith(cisco.CABLE_MARKER)


def test_cdp_cable_dedupes_existing_marked(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=7, description="netbox-sync: cdp old",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert api.dcim.cables.created == []          # no duplicate
    assert {u["id"] for u in api.dcim.cables.updated} == {9}
    assert api.dcim.cables.deleted_ids == []      # seen -> kept


def test_cdp_cable_skips_unresolvable_neighbor(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], None, [], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, [{"device_id": "UNKNOWN", "platform": "",
                               "ip": None, "local_intf": "GigabitEthernet1/0/1",
                               "remote_intf": "Gi0/1"}])
    assert api.dcim.cables.created == []


def test_cdp_cable_preserves_unmarked_and_conflicts(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    manual = FakeRecord(8, device_id=7, description="manual doc",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert api.dcim.cables.created == []        # conflict -> no create
    assert api.dcim.cables.deleted_ids == []    # manual cable preserved


def test_cdp_cable_deletes_stale_marked(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    stale = FakeRecord(9, device_id=7, description="netbox-sync: cdp old",
                       a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                       b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [stale])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, [])   # nothing seen this run

    assert api.dcim.cables.deleted_ids == [9]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py -k cdp -q`
Expected: FAIL — `AttributeError: ... no attribute 'sync_cdp_cables'`.

- [ ] **Step 3: Implement** (append to `netbox_sync/collectors/cisco.py`)

```python
# ── CDP/LLDP cable reconciliation ────────────────────────────────────────────

# Ownership marker: only cables whose description starts with this prefix are
# managed (refreshed/deleted) by the sync. Manual cabling is never touched.
CABLE_MARKER = "netbox-sync:"

def _cable_iface_ids(cable):
    for t in (getattr(cable, "a_terminations", None) or []) + \
             (getattr(cable, "b_terminations", None) or []):
        oid = t.get("object_id") if isinstance(t, dict) else None
        if oid is not None:
            yield oid

def sync_cdp_cables(dev_id, neighbors):
    """Reconcile NetBox cables for one switch from CDP/LLDP neighbor data.

    Both ends must resolve to existing NetBox interfaces; anything else is
    skipped (DEBUG). Only marker-owned cables are managed."""
    api = get_netbox()
    local_ifaces = {str(i.name): i
                    for i in api.dcim.interfaces.filter(device_id=dev_id)}
    existing_cables = list(api.dcim.cables.filter(device_id=dev_id))
    marked = [c for c in existing_cables
              if (c.description or "").startswith(CABLE_MARKER)]
    unmarked = [c for c in existing_cables
                if not (c.description or "").startswith(CABLE_MARKER)]

    marked_by_iface = {}
    for c in marked:
        for oid in _cable_iface_ids(c):
            marked_by_iface.setdefault(oid, c)

    peer_dev_cache = {}
    seen_cable_ids = set()

    for n in neighbors:
        local = local_ifaces.get(_short_intf(n.get("local_intf")))
        if not local:
            log("DEBUG", f"  cdp: local iface {n.get('local_intf')} not found, skipping")
            continue
        peer_name = _normalize_cdp_id(n.get("device_id"))
        if not peer_name:
            continue
        if peer_name not in peer_dev_cache:
            try:
                peer_dev_cache[peer_name] = api.dcim.devices.get(name=peer_name)
            except Exception:
                peer_dev_cache[peer_name] = None
        peer_dev = peer_dev_cache[peer_name]
        if not peer_dev:
            log("DEBUG", f"  cdp: neighbor {peer_name} not in NetBox, skipping")
            continue
        peer_iface = api.dcim.interfaces.get(
            device_id=peer_dev.id, name=_short_intf(n.get("remote_intf")))
        if not peer_iface:
            log("DEBUG", f"  cdp: iface {n.get('remote_intf')} not found on "
                        f"{peer_name}, skipping")
            continue

        desc = (f"{CABLE_MARKER} cdp {local.name} <-> "
                f"{peer_name} {peer_iface.name}")
        existing = marked_by_iface.get(local.id) or marked_by_iface.get(peer_iface.id)
        if existing:
            seen_cable_ids.add(existing.id)
            api.dcim.cables.update([{"id": existing.id, "description": desc}])
            continue
        if any(local.id in _cable_iface_ids(c) or peer_iface.id in _cable_iface_ids(c)
               for c in unmarked):
            log("DEBUG", f"  cdp: manual cable exists on {local.name} or "
                        f"{peer_iface.name}, leaving untouched")
            continue
        try:
            cable = api.dcim.cables.create({
                "a_terminations": [{"object_type": "dcim.interface",
                                    "object_id": local.id}],
                "b_terminations": [{"object_type": "dcim.interface",
                                    "object_id": peer_iface.id}],
                "description": desc,
            })
            seen_cable_ids.add(cable.id)
            log("INFO", f"  cdp: cabled {local.name} <-> {peer_name} {peer_iface.name}")
        except Exception as exc:
            log("WARN", f"  cdp: could not create cable {local.name} "
                        f"<-> {peer_name} {peer_iface.name}: {exc}")

    for c in marked:
        if c.id not in seen_cable_ids:
            try:
                c.delete()
                log("INFO", f"  cdp: removed stale cable id={c.id}")
            except Exception: pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass (64+).

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/collectors/cisco.py tests/test_netbox_sync.py
git commit -m "Add CDP/LLDP cable reconciliation with ownership marker"
```

---

### Task 7: Scanner + orchestrator wiring

**Files:**
- Modify: `netbox_sync/scanner.py`
- Modify: `netbox_sync/sync.py`

**Interfaces:**
- Consumes: `probe_cisco_switch` (Task 3), `cisco_collect_inventory` (Task 3), `ensure_cisco_device` / `mark_cisco_offline` (Task 4), `sync_cisco_interfaces` (Task 5), `sync_cdp_cables` (Task 6), `config.CISCO_RANGES`.
- Produces: `scan_all()["cisco_switches"]` key; Cisco processing + offline blocks in `run_sync()`.

- [ ] **Step 1: Wire the scanner** (in `netbox_sync/scanner.py`)

Update imports:

```python
from netbox_sync.collectors.brocade import probe_san_switch
from netbox_sync.collectors.cisco import probe_cisco_switch
from netbox_sync.collectors.msa import probe_storage
from netbox_sync.collectors.redfish import probe_redfish
from netbox_sync.config import (BMC_RANGES, STORAGE_RANGES, SAN_RANGES,
                                CISCO_RANGES, SCAN_WORKERS, log)
```

Update `scan_all()` — initial dict becomes:

```python
    all_found = {"servers": [], "storage": [], "san_switches": [], "cisco_switches": []}
```

Append before `return all_found`:

```python
    # ── Cisco switches (SSH, opt-in family) ─────────────────────────────────
    if CISCO_RANGES:
        used_ips = used_ips | {h["ip"] for h in all_found["san_switches"]}
        all_cisco_ips = expand_ranges(CISCO_RANGES)
        cisco_ips = [ip for ip in all_cisco_ips if ip not in used_ips]
        skipped_cisco = len(all_cisco_ips) - len(cisco_ips)
        if skipped_cisco:
            log("INFO", f"Skipped {skipped_cisco} IP(s) in Cisco ranges already found.")
        if cisco_ips:
            log("INFO", f"Scanning {len(cisco_ips)} IPs for Cisco switches (SSH) ...")
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futures = {ex.submit(probe_cisco_switch, ip): ip for ip in cisco_ips}
                for f in as_completed(futures):
                    r = f.result()
                    if r:
                        log("INFO", f"  + CISCO {r['ip']}  {r['model']}  s/n={r['serial']}")
                        all_found["cisco_switches"].append(r)
            log("INFO", f"Cisco scan done: {len(all_found['cisco_switches'])} found.")
        else:
            log("WARN", "No Cisco IPs to scan (all excluded).")
    else:
        log("INFO", "Cisco ranges not configured — skipping Cisco scan.")
```

- [ ] **Step 2: Wire the orchestrator** (in `netbox_sync/sync.py`)

Update imports:

```python
from netbox_sync.collectors.brocade import san_collect_inventory, sync_san_interfaces
from netbox_sync.collectors.cisco import (cisco_collect_inventory,
                                          sync_cisco_interfaces,
                                          sync_cdp_cables)
from netbox_sync.collectors.msa import storage_collect_inventory
from netbox_sync.collectors.redfish import rf_collect_inventory
from netbox_sync.config import log
from netbox_sync.netbox import (get_netbox, ensure_server_device,
                                ensure_storage_device, ensure_san_switch_device,
                                ensure_cisco_device,
                                mark_server_offline, mark_storage_offline,
                                mark_san_offline, mark_cisco_offline,
                                _check_offline, sync_inventory)
```

Append the processing block after the SAN switches block (before the offline section):

```python
    # ── Process Cisco switches ────────────────────────────────────────────────
    live_cisco_ips = {h["ip"] for h in found["cisco_switches"]}
    for probe in found["cisco_switches"]:
        ip = probe["ip"]
        log("INFO", f"Processing CISCO {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_cisco_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_cisco_device failed for {ip}: {e}"); continue

        try:
            data = cisco_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  Cisco inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        ports = data["ports"]
        neighbors = data["neighbors"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "cisco_ip":         ip,
                    "cisco_enabled":    True,
                    "cisco_firmware":   summary.get("firmware") or probe.get("firmware"),
                    "cisco_model":      summary.get("model") or probe.get("model"),
                    "cisco_port_count": summary.get("port_count"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  Cisco switch update failed for {ip}: {e}")

        try:
            sync_cisco_interfaces(dev_id, ports)
            log("INFO", f"  [OK] Cisco {ip} — {len(ports)} interfaces synced")
        except Exception as e:
            log("ERROR", f"  Cisco interface sync failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] Cisco {ip} — {len(inv)} inventory items synced")
        except Exception as e:
            log("ERROR", f"  Cisco inventory sync failed for {ip}: {e}")

        try:
            sync_cdp_cables(dev_id, neighbors)
            log("INFO", f"  [OK] Cisco {ip} — {len(neighbors)} neighbors processed")
        except Exception as e:
            log("ERROR", f"  Cisco cable sync failed for {ip}: {e}")
```

Append the offline check after the SAN offline check:

```python
    log("INFO", "Checking for unreachable Cisco switches ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_cisco_enabled=True)):
            cisco_ip = (dev.custom_fields or {}).get("cisco_ip")
            if not cisco_ip: continue
            ip = str(cisco_ip).split("/")[0].strip()
            _check_offline(ip, live_cisco_ips, dev.id, dev.name,
                           mark_cisco_offline, "Cisco switch")
    except Exception as e:
        log("ERROR", f"Cisco offline check failed: {e}")
```

- [ ] **Step 3: Verify — full suite + compile + import check**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile netbox_sync\scanner.py netbox_sync\sync.py netbox_sync\collectors\cisco.py
.\.venv\Scripts\python.exe -c "from netbox_sync.sync import run_sync; print('WIRING OK')"
```
Expected: all tests pass, `WIRING OK`.

- [ ] **Step 4: Commit**

```bash
git add netbox_sync/scanner.py netbox_sync/sync.py
git commit -m "Wire Cisco family into scanner and run_sync"
```

---

### Task 8: Documentation (README EN+FA)

**Files:**
- Modify: `README.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: English updates**

1. Opening paragraph: append Cisco switches to the device list — change `**Brocade / HPE B-Series SAN switches** (via SSH CLI)` phrase list to include `**Cisco Catalyst switches** (via SSH CLI, with CDP/LLDP cabling)`.
2. Repository files table: add row `| `netbox_sync/collectors/cisco.py` | Cisco Catalyst collector — netmiko SSH, CLI parsers, CDP/LLDP cable reconciliation. |` after the `netbox_sync/` row.
3. Env table: add rows after `DEFAULT_SWITCH_ROLE`:

```markdown
| `CISCO_USER` | ❌* | — | SSH username for Cisco switches (required only when `CISCO_RANGES` is set). |
| `CISCO_PASS` | ❌* | — | SSH password for Cisco switches. |
| `CISCO_PORT` | ❌ | `22` | SSH port for Cisco switches. |
| `CISCO_RANGES` | ❌ | *(empty)* | Comma-separated CIDR ranges for Cisco switches. Empty = family disabled. |
| `DEFAULT_CISCO_ROLE` | ❌ | `Switch` | NetBox device role for Cisco switches. |
```

4. Custom fields section: add a **For Cisco switches:** block:

```markdown
| `cisco_ip` | Text | Cisco switch IP |
| `cisco_enabled` | Boolean | Cisco switch enabled |
| `cisco_firmware` | Text | IOS version |
| `cisco_model` | Text | Model |
| `cisco_port_count` | Integer | Port count |
```

5. Offline detection paragraph: extend the flag list with `cisco_enabled=True` (Cisco switches).
6. Supported hardware: add a `**LAN switches (Cisco IOS / IOS-XE, SSH via netmiko):**` bullet — Catalyst 2960X/3650/3850/9200/9300, commands `show version`, `show inventory`, `show interfaces status`, `show cdp neighbors detail`, `show lldp neighbors detail` (fallback).
7. New subsection **CDP/LLDP cabling**: cables are created between Cisco switches (and any neighbor whose device + interface exist in NetBox) from neighbor data; only cables whose description starts with `netbox-sync:` are managed — manually documented cables are never modified or deleted; neighbors that can't be resolved (e.g. servers, whose NICs are inventory items rather than interfaces) are skipped.

- [ ] **Step 2: Persian updates** — mirror items 1, 3, 4, 5, 6, 7 in the فارسی section (same tables/rows, Persian descriptions).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Document Cisco Catalyst support (EN+FA)"
```

---

### Task 9: Final verification + release

**Files:** none (verification only).

- [ ] **Step 1: Full verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile sync_all_to_netbox.py (Get-ChildItem -Recurse netbox_sync -Filter *.py | ForEach-Object FullName)
git status --short
```
Expected: 64+ tests pass, compile OK, clean tree.

- [ ] **Step 2: Push**

```bash
git push origin main
```

- [ ] **Step 3: First real run (user action)**

User: add `CISCO_RANGES` + `CISCO_USER`/`CISCO_PASS` to the production `.env`, create the five `cisco_*` custom fields in NetBox, run once, and check the DEBUG/INFO output. If a switch's CLI format differs from the fixtures, paste sanitized output and the parsers get adjusted (same workflow as Brocade).
