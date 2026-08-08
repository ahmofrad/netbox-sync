"""Tests for sync_unifi_wlans: UniFi WLAN aggregation + per-site VLAN
resolution (unique match wins; missing VLANs created in the majority-AP
site; sites without APs are skipped)."""
import netbox_sync.sync as sync_mod


def _fakes(monkeypatch, sites, site_index_by_site, vlan_groups):
    """sites: {name: id}; site_index_by_site: {site_id: {vid: [(gid, vlan_id)]}};
    vlan_groups: ensure_vlan_group returns these ids per site."""
    created_vlans = []
    synced_wlans = {}
    monkeypatch.setattr(sync_mod, "get_or_create_site", lambda name: sites[name])
    monkeypatch.setattr(sync_mod, "_site_vlan_index",
                        lambda site_id: site_index_by_site.get(site_id, {}))
    monkeypatch.setattr(sync_mod, "ensure_vlan_group",
                        lambda site_id, name: vlan_groups[site_id])
    monkeypatch.setattr(sync_mod, "sync_cisco_vlans",
                        lambda gid, host, vlans: (
                            created_vlans.append((gid, vlans)),
                            {v["vid"]: 1000 + v["vid"] for v in vlans})[1])
    monkeypatch.setattr(sync_mod, "sync_wireless_lans",
                        lambda wlc, wlans, vid_map, group_prefix=None: (
                            synced_wlans.update(wlans={w["ssid"]: w for w in wlans},
                                                vid_map=vid_map),
                            {w["ssid"] for w in wlans})[1])
    monkeypatch.setattr(sync_mod, "sweep_wireless_lans", lambda wlc, seen: None)
    return created_vlans, synced_wlans


def _data():
    return {
        "summary": {"reported_ip": "10.0.0.1"},
        "sites": [{"name": "s1", "desc": "HQ"}, {"name": "s2", "desc": "Branch"}],
        "aps": [],
        "wlans": {
            "s1": [{"ssid": "Corp", "networkconf_id": "n1"}],
            "s2": [{"ssid": "Corp", "networkconf_id": "n2"},
                   {"ssid": "Guest", "networkconf_id": "n3"}],
        },
        "networks": {"s1": {"n1": 10}, "s2": {"n2": 20, "n3": 20}},
    }


def test_unifi_wlan_unique_vid_match_and_missing_creation(monkeypatch):
    """Corp's HQ binding matches vid 10 uniquely -> reused; Guest's vid 20
    is missing at Branch -> created in Branch's (majority-AP) group."""
    created_vlans, synced_wlans = _fakes(
        monkeypatch,
        sites={"HQ": 101, "Branch": 102},
        site_index_by_site={101: {10: [(7, 55)]}, 102: {}},
        vlan_groups={102: 900})

    sync_mod.sync_unifi_wlans(_data(), "UDM",
                              {"HQ": {"HQ": 3}, "Branch": {"Branch": 2}},
                              {}, {}, set())

    assert synced_wlans["wlans"]["Corp"]["vlan_id"] == 10
    assert synced_wlans["vid_map"][10] == 55        # HQ's existing VLAN reused
    assert created_vlans == [
        (900, [{"vid": 20, "name": "VLAN0020", "status": "active"}])]
    assert synced_wlans["vid_map"][20] == 1020      # created vid mapped too


def test_unifi_wlan_site_without_aps_falls_back_to_first_binding(monkeypatch):
    """Branch has no AP votes -> its binding is skipped; with HQ's index
    lacking vid 10 the entry falls back to the first binding (HQ, vid 10)."""
    created_vlans, synced_wlans = _fakes(
        monkeypatch,
        sites={"HQ": 101, "Branch": 102},
        site_index_by_site={101: {}, 102: {}},
        vlan_groups={101: 800})

    sync_mod.sync_unifi_wlans(_data(), "UDM",
                              {"HQ": {"HQ": 3}},      # Branch has no APs
                              {}, {}, set())

    assert synced_wlans["wlans"]["Corp"]["vlan_id"] == 10
    # created only at HQ (Branch has no site) — Guest's Branch binding skipped
    assert created_vlans == [
        (800, [{"vid": 10, "name": "VLAN0010", "status": "active"}])]
