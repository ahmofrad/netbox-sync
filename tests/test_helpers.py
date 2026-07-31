"""Tests for generic helpers: slugify, model normalization, serial
validation, capacity conversion and range expansion."""
import netbox_sync.utils as mod
from netbox_sync.models import SERVER_MODEL_MAP


def test_slugify():
    assert mod.slugify("HPE DL360 G10") == "hpe-dl360-g10"
    assert mod.slugify("  MSA 2060 SAN  ") == "msa-2060-san"


def test_normalize_model_known_and_unknown():
    assert mod.normalize_model("ProLiant DL360 Gen10", SERVER_MODEL_MAP) == "HPE DL360 G10"
    assert mod.normalize_model("  proliant dl380 gen10 plus ", SERVER_MODEL_MAP) == "HPE DL380 G10+"
    # Unknown models pass through unchanged (stripped)
    assert mod.normalize_model("ProLiant DL999 Gen99", SERVER_MODEL_MAP) == "ProLiant DL999 Gen99"
    assert mod.normalize_model("", SERVER_MODEL_MAP) is None
    # No map at all -> stripped passthrough
    assert mod.normalize_model(" ZD1200 ", None) == "ZD1200"


def test_invalid_serial():
    assert mod._invalid_serial("N/A") is True
    assert mod._invalid_serial("") is True
    assert mod._invalid_serial(None) is True
    assert mod._invalid_serial("unknown") is True
    assert mod._invalid_serial(" ABC123 ") is False


def test_capacity_to_bytes():
    assert mod._capacity_to_bytes({"CapacityBytes": "960"}) == 960
    assert mod._capacity_to_bytes({"CapacityMiB": 2}) == 2 * 1024 ** 2
    assert mod._capacity_to_bytes({"CapacityGiB": 1}) == 1024 ** 3
    assert mod._capacity_to_bytes({"CapacityGB": 1}) == 1000 ** 3
    assert mod._capacity_to_bytes({}) is None
    assert mod._capacity_to_bytes(None) is None


def test_gib_from_bytes():
    assert mod.gib_from_bytes(3 * 1024 ** 3) == 3
    assert mod.gib_from_bytes(None) is None


def test_parse_storage_size_bytes():
    assert mod.parse_storage_size_bytes("600GB") == 600 * 1024 ** 3
    assert mod.parse_storage_size_bytes("1.8TB") == int(1.8 * 1024 ** 4)
    assert mod.parse_storage_size_bytes(None, 1000) == 1000 * 1024 * 1024
    assert mod.parse_storage_size_bytes(None, None) is None


def test_bytes_to_human_snaps_to_standard_sizes():
    assert mod._bytes_to_human(480032981402) == "480GB"      # ~447 GiB -> 480GB
    assert mod._bytes_to_human(1200000000000) == "1.2TB"
    assert mod._bytes_to_human(8000000000) == "8GB"


def test_expand_ranges():
    assert mod.expand_ranges(["192.0.2.5/32"]) == ["192.0.2.5"]
    assert mod.expand_ranges(["192.0.2.0/30"]) == ["192.0.2.1", "192.0.2.2"]


def test_add_inventory_item_skips_invalid_and_duplicate_serials():
    inv = {}
    mod._add_inventory_item(inv, "CPU", "Intel", "PN", "S1", "desc", 3)
    mod._add_inventory_item(inv, "CPU", "Intel", "PN", "N/A", "desc", 3)
    mod._add_inventory_item(inv, "CPU 2", "Intel", "PN", "S1", "desc", 3)
    assert list(inv.keys()) == ["S1"]
    assert inv["S1"]["name"] == "CPU"
    assert inv["S1"]["role"] == 3


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
