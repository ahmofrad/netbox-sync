#!/usr/bin/env python3
"""
test_msa_raw_disks.py -- Check if 'show disks' returns disk data even when
the status says "Info: Rates may vary". The MSA 2040 may include the disk
OBJECTs in the XML alongside the rate-limit status message.
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

IP = sys.argv[1] if len(sys.argv) > 1 else "172.31.250.98"


def cred_hash(ht):
    cred = f"{STORAGE_USER}_{STORAGE_PASS}".encode()
    if ht == "md5":
        return hashlib.md5(cred).hexdigest()
    return hashlib.sha256(cred).hexdigest()


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
    return ET.fromstring(r.text)


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


# Login
for ht in (STORAGE_AUTH_HASH, "sha256", "md5"):
    try:
        root = do_request(f"login/{cred_hash(ht)}")
        status = root.find("./OBJECT[@name='status']")
        if status is not None:
            props = {p.get("name"): (p.text or "").strip() for p in status.findall("PROPERTY")}
            if props.get("response-type", "").lower() == "success":
                session_key = props.get("response")
                sess.cookies.set("wbisessionkey", session_key)
                sess.cookies.set("wbiusername", STORAGE_USER)
                print(f"[OK] Logged in (hash={ht})")
                break
    except Exception as e:
        print(f"[WARN] login {ht}: {e}")

if not session_key:
    print("[FAIL] Could not log in")
    sys.exit(1)

# ── Fetch 'show disks' raw XML and parse objects REGARDLESS of status ────────
print(f"\n=== show disks (ignoring rate-limit status) ===")
try:
    root = do_request("show/disks")

    # Show what the status says
    status = root.find("./OBJECT[@name='status']")
    if status is not None:
        props = {p.get("name"): (p.text or "").strip() for p in status.findall("PROPERTY")}
        print(f"Status: response-type={props.get('response-type')}, response={props.get('response', '')[:100]}")

    # Parse ALL objects regardless of status
    all_objects = []
    for obj in root.findall("OBJECT"):
        bt = obj.get("basetype")
        name = obj.get("name")
        props = {"basetype": bt, "name": name, "oid": obj.get("oid")}
        for p in obj.findall("PROPERTY"):
            props[p.get("name")] = (p.text or "").strip()
        all_objects.append(props)

    print(f"\nTotal OBJECTs in XML: {len(all_objects)}")

    # Group by basetype
    by_bt = {}
    for o in all_objects:
        bt = o.get("basetype", "?")
        by_bt.setdefault(bt, []).append(o)

    print(f"Basetypes: { {bt: len(v) for bt, v in by_bt.items()} }")

    # Show full first row of each non-status basetype
    for bt, rows in by_bt.items():
        if bt == "status":
            continue
        print(f"\n--- basetype '{bt}' ({len(rows)} rows) ---")
        if rows:
            print(f"ALL keys: {list(rows[0].keys())}")
            for k, v in rows[0].items():
                if k not in ("basetype", "name", "oid") and v:
                    print(f"  {k} = {v}")

except Exception as e:
    print(f"FAILED: {e}")

# ── Also try disk-statistics for comparison ───────────────────────────────────
print(f"\n=== show disk-statistics (for comparison) ===")
try:
    root = do_request("show/disk-statistics")
    rows = parse_objects(root)
    print(f"Rows: {len(rows)}")
    if rows:
        print(f"Sample keys: {list(rows[0].keys())}")
        for k, v in rows[0].items():
            if k not in ("basetype", "name", "oid") and v:
                print(f"  {k} = {v}")
except Exception as e:
    print(f"FAILED: {e}")

try:
    do_request("exit")
except Exception:
    pass
print("\n[DONE]")