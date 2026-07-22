#!/usr/bin/env python3
"""
test_msa_disks2.py -- Deeper investigation of MSA 2040 disk detail endpoints.

Tries:
1. show disks with multiple retries + longer waits (rate-limit may clear)
2. Per-disk queries: show disks/disk_01.01, show disk-statistics/disk_01.01
3. show disks/<location>, show disks/<serial>
4. show disk/[serial] variations
5. show drives/<id>
6. show maps
7. show enclosure-slot / show component-location
"""
import sys
import os
import hashlib
import time
from xml.etree import ElementTree as ET

import requests
import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

STORAGE_USER = os.getenv("STORAGE_USER")
STORAGE_PASS = os.getenv("STORAGE_PASS")
STORAGE_PORT = int(os.getenv("STORAGE_PORT", "443"))
STORAGE_AUTH_HASH = os.getenv("STORAGE_AUTH_HASH", "sha256").lower()

if not STORAGE_USER or not STORAGE_PASS:
    print("ERROR: STORAGE_USER/STORAGE_PASS missing in .env")
    sys.exit(1)
if len(sys.argv) < 2:
    print("Usage: python test_msa_disks2.py <storage_ip>")
    sys.exit(1)

IP = sys.argv[1]


def cred_hash(ht):
    cred = f"{STORAGE_USER}_{STORAGE_PASS}".encode()
    if ht == "md5":
        return hashlib.md5(cred).hexdigest()
    return hashlib.sha256(cred).hexdigest()


def parse_objects(root):
    objs = []
    for obj in root.findall("OBJECT"):
        bt = obj.get("basetype")
        if not bt or bt == "status":
            continue
        props = {"basetype": bt, "name": obj.get("name"), "oid": obj.get("oid")}
        for p in obj.findall("PROPERTY"):
            props[p.get("name")] = (p.text or "").strip()
        objs.append(props)
    return objs


def response_status(root):
    status = root.find("./OBJECT[@name='status']")
    if status is None:
        return {"response-type": "unknown"}
    return {p.get("name"): (p.text or "").strip() for p in status.findall("PROPERTY")}


base = f"https://{IP}:{STORAGE_PORT}"
sess = requests.Session()
sess.verify = False
session_key = None


def do_request(path):
    url = f"{base}/api/{path.lstrip('/')}"
    headers = {"dataType": "api"}
    if session_key:
        headers["sessionKey"] = session_key
    r = sess.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    if r.text.strip().startswith("*"):
        raise RuntimeError(f"RATE_LIMIT: {r.text.strip()}")
    return ET.fromstring(r.text)


def login():
    global session_key
    for ht in (STORAGE_AUTH_HASH, "sha256", "md5"):
        try:
            root = do_request(f"login/{cred_hash(ht)}")
            st = response_status(root)
            if st.get("response-type", "").lower() == "success":
                session_key = st.get("response")
                sess.cookies.set("wbisessionkey", session_key)
                sess.cookies.set("wbiusername", STORAGE_USER)
                print(f"[OK] Logged in (hash={ht})")
                return True
        except Exception as e:
            print(f"[WARN] login with {ht} failed: {e}")
    return False


def try_cmd(cmd, label=None):
    label = label or cmd
    try:
        root = do_request(f"show/{cmd}")
        st = response_status(root)
        if st.get("response-type", "").lower() != "success":
            print(f"  [{label}] response-type={st.get('response-type')}: {st.get('response', '')[:100]}")
            return None
        rows = parse_objects(root)
        bts = {}
        for r in rows:
            bts[r.get("basetype", "?")] = bts.get(r.get("basetype", "?"), 0) + 1
        print(f"  [{label}] {len(rows)} rows, basetypes={bts}")
        return rows
    except Exception as e:
        print(f"  [{label}] FAILED: {e}")
        return None


def dump_first(rows, label):
    if not rows:
        return
    seen = set()
    for r in rows:
        bt = r.get("basetype", "?")
        if bt in seen:
            continue
        seen.add(bt)
        print(f"    FULL sample [{bt}] from {label}:")
        for k, v in r.items():
            if k not in ("basetype", "name", "oid") and v:
                print(f"      {k} = {v}")


print(f"Connecting to {base} ...")
if not login():
    sys.exit(1)

# ── 1. Retry show disks with longer waits ────────────────────────────────────
print(f"\n=== show disks RETRIES (longer waits) ===")
for attempt in range(1, 5):
    print(f"\n-- attempt {attempt} --")
    rows = try_cmd("disks")
    if rows:
        print(f"  [SUCCESS on attempt {attempt}]")
        dump_first(rows, "disks")
        break
    # Wait progressively longer before retrying
    if attempt < 4:
        wait = 15 * attempt
        print(f"  waiting {wait}s before retry ...")
        time.sleep(wait)

# ── 2. Per-disk queries ─────────────────────────────────────────────────────
print(f"\n=== PER-DISK QUERIES (sample disk_01.01) ===")
per_disk_cmds = [
    "disks/disk_01.01",
    "disk-statistics/disk_01.01",
    "disks/1.1",
    "disks/57X0A06UF7CD1721",
    "disk/disk_01.01",
    "disk/1.1",
    "disk/57X0A06UF7CD1721",
    "drive/disk_01.01",
    "drive/1.1",
    "drives/disk_01.01",
]
for cmd in per_disk_cmds:
    rows = try_cmd(cmd)
    if rows:
        dump_first(rows, cmd)

# ── 3. Other potential disk-detail endpoints ────────────────────────────────
print(f"\n=== OTHER DISK-DETAIL ENDPOINTS ===")
other_cmds = [
    "disk-parameters/disk_01.01",
    "disk-groups",            # has array-drive-type per group
    "disk-groups/all",
    "maps",
    "maps/disks",
    "vdisks",
    "vdisk",
    "storage",
    "enclosures",
    "enclosure-disk",
    "component-location",
    "disk-info",
    "disk",
    "drive-info",
]
for cmd in other_cmds:
    rows = try_cmd(cmd)
    if rows:
        dump_first(rows, cmd)
    time.sleep(2)

# ── 4. Check disk-groups per-disk membership (to infer type per disk) ───────
print(f"\n=== DISK GROUP MEMBERS (type inference) ===")
dg_rows = try_cmd("disk-groups")
if dg_rows:
    # Each disk-group has a serial; query its members
    for dg in dg_rows[:3]:
        dg_serial = dg.get("serial-number")
        dg_name = dg.get("name")
        if dg_serial:
            print(f"\n  Disk-group '{dg_name}' (serial={dg_serial}):")
            for sub in ("disks", "drives", "members"):
                rows = try_cmd(f"disk-groups/{dg_serial}/{sub}", f"disk-groups/{sub}")
                if rows:
                    dump_first(rows, f"dg/{sub}")
                    break

# Logout
try:
    do_request("exit")
except Exception:
    pass
print("\n[DONE]")
