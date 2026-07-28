"""Unified scanner: probes all configured IP ranges in parallel thread pools
and returns discovered devices grouped by family."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from netbox_sync.collectors.brocade import probe_san_switch
from netbox_sync.collectors.cisco import probe_cisco_switch
from netbox_sync.collectors.msa import probe_storage
from netbox_sync.collectors.redfish import probe_redfish
from netbox_sync.config import (BMC_RANGES, STORAGE_RANGES, SAN_RANGES,
                                CISCO_RANGES, SCAN_WORKERS, log)
from netbox_sync.utils import expand_ranges


def scan_all():
    all_found = {"servers": [], "storage": [], "san_switches": [], "cisco_switches": []}

    bmc_ips = expand_ranges(BMC_RANGES)
    log("INFO", f"Scanning {len(bmc_ips)} IPs across {len(BMC_RANGES)} BMC ranges ...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(probe_redfish, ip): ip for ip in bmc_ips}
        for f in as_completed(futures):
            r = f.result()
            if r:
                log("INFO", f"  + SERVER {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["servers"].append(r)
    log("INFO", f"Server scan done: {len(all_found['servers'])} found.")

    server_ips = {h["ip"] for h in all_found["servers"]}
    all_storage_ips = expand_ranges(STORAGE_RANGES)
    storage_ips = [ip for ip in all_storage_ips if ip not in server_ips]
    skipped = len(all_storage_ips) - len(storage_ips)
    if skipped:
        log("INFO", f"Skipped {skipped} IP(s) in storage ranges already found as servers.")

    if storage_ips:
        log("INFO", f"Scanning {len(storage_ips)} IPs for storage ...")
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            futures = {ex.submit(probe_storage, ip): ip for ip in storage_ips}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    log("INFO", f"  + STORAGE {r['ip']}  {r['model']}  s/n={r['serial']}")
                    all_found["storage"].append(r)
        log("INFO", f"Storage scan done: {len(all_found['storage'])} found.")
    else:
        log("WARN", "No storage ranges to scan (all excluded or none configured).")

    # ── SAN switches (SSH on port 22) ────────────────────────────────────────
    used_ips = server_ips | {h["ip"] for h in all_found["storage"]}
    all_san_ips = expand_ranges(SAN_RANGES)
    san_ips = [ip for ip in all_san_ips if ip not in used_ips]
    skipped_san = len(all_san_ips) - len(san_ips)
    if skipped_san:
        log("INFO", f"Skipped {skipped_san} IP(s) in SAN ranges already found as server/storage.")
    if san_ips:
        log("INFO", f"Scanning {len(san_ips)} IPs for SAN switches (SSH) ...")
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            futures = {ex.submit(probe_san_switch, ip): ip for ip in san_ips}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    log("INFO", f"  + SAN {r['ip']}  {r.get('model')}  wwn={r.get('wwn')}")
                    all_found["san_switches"].append(r)
        log("INFO", f"SAN switch scan done: {len(all_found['san_switches'])} found.")
    else:
        log("WARN", "No SAN switch ranges to scan (all excluded or none configured).")

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

    return all_found
