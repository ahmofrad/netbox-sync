"""run_sync: the main reconciliation job — scan, ensure devices, collect and
sync inventory, sync SAN interfaces, then mark unreachable devices offline."""
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
                                _check_offline,
                                sync_inventory)
from netbox_sync.scanner import scan_all


def run_sync():
    log("INFO", "=" * 60)
    log("INFO", "Unified sync started (servers + storage + SAN switches)")
    log("INFO", "=" * 60)

    found = scan_all()
    api = get_netbox()

    # ── Process servers ───────────────────────────────────────────────────────
    live_server_ips = {h["ip"] for h in found["servers"]}
    for probe in found["servers"]:
        ip = probe["ip"]
        host = probe["host"]
        log("INFO", f"Processing SERVER {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_server_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_server_device failed for {ip}: {e}"); continue

        try:
            data = rf_collect_inventory(host)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  inventory collection failed for {ip}: {e}"); continue

        s   = data["summary"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "bmc_ip":                 ip,
                    "redfish_enabled":        True,
                    "redfish_model":          s.get("model"),
                    "redfish_power_state":    s.get("power_state"),
                    "redfish_bios_version":   s.get("bios_version"),
                    "redfish_cpu_model":      s.get("cpu_model"),
                    "redfish_cpu_sockets":    s.get("cpu_sockets"),
                    "redfish_cpu_cores":      s.get("cpu_cores"),
                    "redfish_cpu_threads":    s.get("cpu_threads"),
                    "redfish_ram_gib":        s.get("ram_gib"),
                    "redfish_disk_total_gib": s.get("disk_total_gib"),
                },
            }
            if s.get("serial"): payload["serial"] = s["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  server update failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] Server {ip} — {len(inv)} items synced")
        except Exception as e:
            log("ERROR", f"  inventory sync failed for {ip}: {e}")

    # ── Process storage ──────────────────────────────────────────────────────
    live_storage_ips = {h["ip"] for h in found["storage"]}
    for probe in found["storage"]:
        ip = probe["ip"]
        log("INFO", f"Processing STORAGE {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_storage_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_storage_device failed for {ip}: {e}"); continue

        try:
            data = storage_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "storage_ip":                 ip,
                    "storage_enabled":            True,
                    "storage_health":             summary.get("health") or probe.get("health"),
                    "storage_firmware":           summary.get("firmware") or probe.get("firmware"),
                    "storage_model":              summary.get("model") or probe.get("model"),
                    "storage_disk_count":         summary.get("disk_count"),
                    "storage_total_capacity_gib": summary.get("disk_total_gib"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  storage update failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] Storage {ip} — {len(inv)} items synced")
        except Exception as e:
            log("ERROR", f"  inventory sync failed for {ip}: {e}")

    # ── Process SAN switches ──────────────────────────────────────────────────
    live_san_ips = {h["ip"] for h in found["san_switches"]}
    for probe in found["san_switches"]:
        ip = probe["ip"]
        log("INFO", f"Processing SAN SWITCH {ip}  ({probe.get('model')} / wwn={probe.get('wwn')})")

        try:
            dev_id = ensure_san_switch_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_san_switch_device failed for {ip}: {e}"); continue

        try:
            data = san_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  SAN inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        ports = data["ports"]
        nameserver = data["nameserver"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "san_switch_ip":        ip,
                    "san_switch_enabled":   True,
                    "san_switch_wwn":       summary.get("wwn") or probe.get("wwn"),
                    "san_switch_firmware":  summary.get("firmware") or probe.get("firmware"),
                    "san_switch_model":     summary.get("model") or probe.get("model"),
                    "san_switch_port_count": summary.get("port_count"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  SAN switch update failed for {ip}: {e}")

        try:
            sync_san_interfaces(dev_id, ports, nameserver)
            log("INFO", f"  [OK] SAN {ip} — {len(ports)} ports, {len(nameserver)} nameserver entries")
        except Exception as e:
            log("ERROR", f"  SAN interface sync failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] SAN {ip} — {len(inv)} inventory items synced")
        except Exception as e:
            log("ERROR", f"  SAN inventory sync failed for {ip}: {e}")

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

    # ── Mark unreachable devices offline ─────────────────────────────────────
    # A device must be missing from OFFLINE_THRESHOLD consecutive scans before
    # being marked offline. This prevents transient iLO slowness under load
    # from causing false offline markings.
    log("INFO", "Checking for unreachable servers (Redfish) ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_redfish_enabled=True)):
            bmc_ip = (dev.custom_fields or {}).get("bmc_ip")
            if not bmc_ip: continue
            ip = bmc_ip.split("/")[0].strip()
            _check_offline(ip, live_server_ips, dev.id, dev.name,
                           mark_server_offline, "Server")
    except Exception as e:
        log("ERROR", f"Server offline check failed: {e}")

    log("INFO", "Checking for unreachable storage ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_storage_enabled=True)):
            storage_ip = (dev.custom_fields or {}).get("storage_ip")
            if not storage_ip: continue
            ip = str(storage_ip).split("/")[0].strip()
            _check_offline(ip, live_storage_ips, dev.id, dev.name,
                           mark_storage_offline, "Storage")
    except Exception as e:
        log("ERROR", f"Storage offline check failed: {e}")

    log("INFO", "Checking for unreachable SAN switches ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_san_switch_enabled=True)):
            san_ip = (dev.custom_fields or {}).get("san_switch_ip")
            if not san_ip: continue
            ip = str(san_ip).split("/")[0].strip()
            _check_offline(ip, live_san_ips, dev.id, dev.name,
                           mark_san_offline, "SAN switch")
    except Exception as e:
        log("ERROR", f"SAN switch offline check failed: {e}")

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

    log("INFO", "Unified sync complete")
    log("INFO", "=" * 60)
