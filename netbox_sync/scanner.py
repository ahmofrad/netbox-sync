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


def _drain_pool(ex, futures, on_hit):
    """Collect probe results. On Ctrl+C, cancel pending probes and shut down
    without waiting — the abort stays responsive while in-flight probes
    (up to ~20s of port-timeout retries each) finish in the background."""
    try:
        for f in as_completed(futures):
            r = f.result()
            if r: on_hit(r)
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        ex.shutdown(wait=True)


def scan_all():
    all_found = {"servers": [], "storage": [], "san_switches": [], "cisco_switches": []}

    bmc_ips = expand_ranges(BMC_RANGES)
    if bmc_ips:
        log("INFO", f"Scanning {len(bmc_ips)} IPs across {len(BMC_RANGES)} BMC ranges ...")
        ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
        futures = {ex.submit(probe_redfish, ip): ip for ip in bmc_ips}
        def _on_server(r):
            log("INFO", f"  + SERVER {r['ip']}  {r['model']}  s/n={r['serial']}")
            all_found["servers"].append(r)
        _drain_pool(ex, futures, _on_server)
        log("INFO", f"Server scan done: {len(all_found['servers'])} found.")
    else:
        log("INFO", "BMC ranges empty — skipping server scan.")

    server_ips = {h["ip"] for h in all_found["servers"]}
    all_storage_ips = expand_ranges(STORAGE_RANGES)
    storage_ips = [ip for ip in all_storage_ips if ip not in server_ips]
    skipped = len(all_storage_ips) - len(storage_ips)
    if skipped:
        log("INFO", f"Skipped {skipped} IP(s) in storage ranges already found as servers.")

    if storage_ips:
        log("INFO", f"Scanning {len(storage_ips)} IPs for storage ...")
        ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
        futures = {ex.submit(probe_storage, ip): ip for ip in storage_ips}
        def _on_storage(r):
            log("INFO", f"  + STORAGE {r['ip']}  {r['model']}  s/n={r['serial']}")
            all_found["storage"].append(r)
        _drain_pool(ex, futures, _on_storage)
        log("INFO", f"Storage scan done: {len(all_found['storage'])} found.")
    else:
        log("INFO", "No storage IPs to scan (ranges empty or all excluded).")

    # ── SAN switches (SSH on port 22) ────────────────────────────────────────
    used_ips = server_ips | {h["ip"] for h in all_found["storage"]}
    all_san_ips = expand_ranges(SAN_RANGES)
    san_ips = [ip for ip in all_san_ips if ip not in used_ips]
    skipped_san = len(all_san_ips) - len(san_ips)
    if skipped_san:
        log("INFO", f"Skipped {skipped_san} IP(s) in SAN ranges already found as server/storage.")
    if san_ips:
        log("INFO", f"Scanning {len(san_ips)} IPs for SAN switches (SSH) ...")
        ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
        futures = {ex.submit(probe_san_switch, ip): ip for ip in san_ips}
        def _on_san(r):
            log("INFO", f"  + SAN {r['ip']}  {r.get('model')}  wwn={r.get('wwn')}")
            all_found["san_switches"].append(r)
        _drain_pool(ex, futures, _on_san)
        log("INFO", f"SAN switch scan done: {len(all_found['san_switches'])} found.")
    else:
        log("INFO", "No SAN switch IPs to scan (ranges empty or all excluded).")

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
            ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            futures = {ex.submit(probe_cisco_switch, ip): ip for ip in cisco_ips}
            def _on_cisco(r):
                log("INFO", f"  + CISCO {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["cisco_switches"].append(r)
            _drain_pool(ex, futures, _on_cisco)
            log("INFO", f"Cisco scan done: {len(all_found['cisco_switches'])} found.")
        else:
            log("INFO", "No Cisco IPs to scan (all excluded).")
    else:
        log("INFO", "Cisco ranges not configured — skipping Cisco scan.")

    return all_found
