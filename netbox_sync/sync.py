"""run_sync: the main reconciliation job — scan, ensure devices, collect and
sync inventory, sync SAN interfaces, then mark unreachable devices offline."""
from netbox_sync.collectors.brocade import san_collect_inventory, sync_san_interfaces
from netbox_sync.collectors.cisco import (cisco_collect_inventory,
                                          sync_cisco_interfaces,
                                          ensure_vlan_group,
                                          sync_cisco_vlans,
                                          sync_interface_vlans,
                                          sweep_stale_vlans,
                                          sweep_legacy_site_vlans,
                                          sync_cdp_cables)
from netbox_sync.collectors.fortigate import (fortigate_collect,
                                              sync_fortigate_interfaces)
from netbox_sync.collectors.msa import storage_collect_inventory
from netbox_sync.collectors.redfish import rf_collect_inventory
from netbox_sync.config import (log, BMC_RANGES, STORAGE_RANGES, SAN_RANGES,
                                CISCO_RANGES, FORTIGATE_RANGES)
from netbox_sync.netbox import (get_netbox, ensure_server_device,
                                ensure_storage_device, ensure_san_switch_device,
                                ensure_cisco_device, ensure_fortigate_device,
                                ensure_primary_ip,
                                mark_server_offline, mark_storage_offline,
                                mark_san_offline, mark_cisco_offline,
                                mark_fortigate_offline,
                                _check_offline,
                                sync_inventory)
from netbox_sync.scanner import scan_all


def run_sync():
    log("INFO", "=" * 60)
    log("INFO", "Unified sync started (servers + storage + SAN + Cisco switches)")
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
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

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
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

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
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

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
    group_vlan_seen = {}
    legacy_sites = set()
    for probe in found["cisco_switches"]:
        ip = probe["ip"]
        log("INFO", f"Processing CISCO {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_cisco_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_cisco_device failed for {ip}: {e}"); continue

        try:
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

        try:
            data = cisco_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  Cisco inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        ports = data["ports"]
        neighbors = data["neighbors"]
        vlans = data["vlans"]
        trunks = data["trunks"]
        vtp = data["vtp"]
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

        site_id = None
        try:
            dev_rec = api.dcim.devices.get(id=dev_id)
            site_id = getattr(getattr(dev_rec, "site", None), "id", None)
        except Exception:
            site_id = None

        vid_map = {}
        if site_id:
            try:
                key = (vtp.get("domain") or probe.get("hostname") or ip)
                group_id = ensure_vlan_group(site_id, key)
                vid_map = sync_cisco_vlans(group_id, probe.get("hostname") or "", vlans)
                group_vlan_seen.setdefault(group_id, set()).update(vid_map.keys())
                legacy_sites.add(site_id)
            except Exception as e:
                log("WARN", f"  VLAN sync failed for {ip}: {e}")
        else:
            log("WARN", f"  no site on device for {ip} — skipping VLAN sync")

        try:
            sync_cisco_interfaces(dev_id, ports)
            log("INFO", f"  [OK] Cisco {ip} — {len(ports)} interfaces synced")
        except Exception as e:
            log("ERROR", f"  Cisco interface sync failed for {ip}: {e}")

        if vid_map:
            try:
                sync_interface_vlans(dev_id, ports, trunks, vid_map)
                log("INFO", f"  [OK] Cisco {ip} — VLAN linkage synced")
            except Exception as e:
                log("ERROR", f"  Cisco VLAN linkage failed for {ip}: {e}")

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

    # ── Process FortiGates ────────────────────────────────────────────────────
    live_fortigate_ips = {h["ip"] for h in found["fortigates"]}
    for probe in found["fortigates"]:
        ip = probe["ip"]
        log("INFO", f"Processing FORTIGATE {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_fortigate_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_fortigate_device failed for {ip}: {e}"); continue

        try:
            ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
        except Exception as e:
            log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")

        try:
            data = fortigate_collect(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  FortiGate inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        ports = data["ports"]
        vlans = data["vlans"]
        neighbors = data["neighbors"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "fortigate_ip":         ip,
                    "fortigate_enabled":    True,
                    "fortigate_firmware":   summary.get("firmware") or probe.get("firmware"),
                    "fortigate_model":      summary.get("model") or probe.get("model"),
                    "fortigate_port_count": summary.get("port_count"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  FortiGate update failed for {ip}: {e}")

        site_id = None
        try:
            dev_rec = api.dcim.devices.get(id=dev_id)
            site_id = getattr(getattr(dev_rec, "site", None), "id", None)
        except Exception:
            site_id = None

        vid_map = {}
        if site_id:
            try:
                group_id = ensure_vlan_group(site_id, probe.get("hostname") or ip)
                vid_map = sync_cisco_vlans(group_id, probe.get("hostname") or "", vlans)
                group_vlan_seen.setdefault(group_id, set()).update(vid_map.keys())
                legacy_sites.add(site_id)
            except Exception as e:
                log("WARN", f"  VLAN sync failed for {ip}: {e}")
        else:
            log("WARN", f"  no site on device for {ip} — skipping VLAN sync")

        try:
            sync_fortigate_interfaces(dev_id, ports, vid_map)
            log("INFO", f"  [OK] FortiGate {ip} — {len(ports)} interfaces synced")
        except Exception as e:
            log("ERROR", f"  FortiGate interface sync failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] FortiGate {ip} — {len(inv)} inventory items synced")
        except Exception as e:
            log("ERROR", f"  FortiGate inventory sync failed for {ip}: {e}")

        try:
            sync_cdp_cables(dev_id, neighbors, protocol="lldp")
            log("INFO", f"  [OK] FortiGate {ip} — {len(neighbors)} neighbors processed")
        except Exception as e:
            log("ERROR", f"  FortiGate cable sync failed for {ip}: {e}")

    # ── Sweep stale marker-owned VLANs per group + legacy site VLANs ─────────
    for group_id, seen in group_vlan_seen.items():
        try:
            sweep_stale_vlans(group_id, seen)
        except Exception as e:
            log("ERROR", f"  VLAN sweep failed for group {group_id}: {e}")
    for site_id in legacy_sites:
        try:
            sweep_legacy_site_vlans(site_id)
        except Exception as e:
            log("ERROR", f"  legacy VLAN sweep failed for site {site_id}: {e}")

    # ── Mark unreachable devices offline ─────────────────────────────────────
    # A device must be missing from OFFLINE_THRESHOLD consecutive scans before
    # being marked offline. This prevents transient iLO slowness under load
    # from causing false offline markings. Families whose ranges are disabled
    # are NOT swept — disabling a family must never affect its devices.
    _offline_sweep(api, bool(BMC_RANGES), "cf_redfish_enabled", "bmc_ip",
                   live_server_ips, mark_server_offline, "servers (Redfish)")
    _offline_sweep(api, bool(STORAGE_RANGES), "cf_storage_enabled", "storage_ip",
                   live_storage_ips, mark_storage_offline, "storage")
    _offline_sweep(api, bool(SAN_RANGES), "cf_san_switch_enabled", "san_switch_ip",
                   live_san_ips, mark_san_offline, "SAN switches")
    _offline_sweep(api, bool(CISCO_RANGES), "cf_cisco_enabled", "cisco_ip",
                   live_cisco_ips, mark_cisco_offline, "Cisco switches")
    _offline_sweep(api, bool(FORTIGATE_RANGES), "cf_fortigate_enabled", "fortigate_ip",
                   live_fortigate_ips, mark_fortigate_offline, "FortiGates")

    log("INFO", "Unified sync complete")
    log("INFO", "=" * 60)


def _offline_sweep(api, enabled, cf_field, ip_field, live_ips, mark_fn, label):
    """One family's offline pass: every enabled device whose stored IP was not
    seen this scan gets a miss via _check_offline. No-op when the family is
    disabled (empty ranges) so it never offlines its existing devices."""
    if not enabled:
        return
    log("INFO", f"Checking for unreachable {label} ...")
    try:
        for dev in list(api.dcim.devices.filter(**{cf_field: True})):
            stored_ip = (dev.custom_fields or {}).get(ip_field)
            if not stored_ip: continue
            ip = str(stored_ip).split("/")[0].strip()
            _check_offline(ip, live_ips, dev.id, dev.name, mark_fn, label)
    except Exception as e:
        log("ERROR", f"{label} offline check failed: {e}")
