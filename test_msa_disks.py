#!/usr/bin/env python3
"""
test_msa_disks.py -- Standalone debug script for HPE MSA 2040 disk discovery.

Run from the same directory as your .env file:
    python test_msa_disks.py <storage_ip>

It logs in to the storage array, tries every known disk-related show command,
and dumps the basetype, row count, and first row's fields for each. This lets
us identify which command + fields your MSA 2040 firmware uses so the main
sync script can be fixed correctly.
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
    print("Usage: python test_msa_disks.py <storage_ip>")
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


print(f"Connecting to {base} ...")

# Login
for ht in (STORAGE_AUTH_HASH, "sha256", "md5"):
    try:
        root = do_request(f"login/{cred_hash(ht)}")
        st = response_status(root)
        if st.get("response-type", "").lower() == "success":
            session_key = st.get("response")
            sess.cookies.set("wbisessionkey", session_key)
            sess.cookies.set("wbiusername", STORAGE_USER)
            print(f"[OK] Logged in (hash={ht})")
            break
    except Exception as e:
        print(f"[WARN] login with {ht} failed: {e}")
else:
    print("[FAIL] Could not log in")
    sys.exit(1)

# Identify the system
try:
    rows = parse_objects(do_request("show/system"))
    if rows:
        print(f"\n=== SYSTEM ===")
        for k, v in rows[0].items():
            if k not in ("basetype", "name", "oid") and v:
                print(f"  {k} = {v}")
except Exception as e:
    print(f"[WARN] show/system failed: {e}")

# Try every known disk command
disk_cmds = [
    "disks",
    "disk-statistics",   # MSA 2040 - has 68 disk rows with serial + location
    "disk-parameters",
    "disks-all",
    "maps-disks",
    "drives",
    "disk-groups",
    "vdisk-info",
    "storage-system",
]

print(f"\n=== DISK COMMANDS ===")
for cmd in disk_cmds:
    try:
        root = do_request(f"show/{cmd}")
        st = response_status(root)
        if st.get("response-type", "").lower() != "success":
            print(f"[{cmd}] response-type={st.get('response-type')}: {st.get('response', '')[:80]}")
            continue
        rows = parse_objects(root)
        bts = {}
        for r in rows:
            bts[r.get("basetype", "?")] = bts.get(r.get("basetype", "?"), 0) + 1
        print(f"\n[{cmd}] {len(rows)} rows, basetypes={bts}")
        # Show ALL fields of the first row of each basetype
        seen_bt = set()
        for r in rows:
            bt = r.get("basetype", "?")
            if bt in seen_bt:
                continue
            seen_bt.add(bt)
            print(f"  FULL sample [{bt}]:")
            for k, v in r.items():
                if k not in ("basetype", "name", "oid") and v:
                    print(f"    {k} = {v}")
    except Exception as e:
        print(f"[{cmd}] FAILED: {e}")
    time.sleep(2)  # avoid rate-limit

# Try enclosure/disk-combination commands
print(f"\n=== ENCLOSURE / FRU COMMANDS ===")
for cmd in ("enclosures", "enclosure-fru", "controllers", "controller-statistics"):
    try:
        root = do_request(f"show/{cmd}")
        st = response_status(root)
        if st.get("response-type", "").lower() != "success":
            continue
        rows = parse_objects(root)
        bts = {}
        for r in rows:
            bts[r.get("basetype", "?")] = bts.get(r.get("basetype", "?"), 0) + 1
        print(f"\n[{cmd}] {len(rows)} rows, basetypes={bts}")
    except Exception as e:
        print(f"[{cmd}] failed: {e}")
    time.sleep(1)

# Logout
try:
    do_request("exit")
except Exception:
    pass
print("\n[DONE]")
