"""Tests for generic helpers: slugify, model normalization, serial
validation, capacity conversion and range expansion."""
import sync_all_to_netbox as mod
from models import SERVER_MODEL_MAP


def test_slugify():
    assert mod.slugify("HPE DL360 G10") == "hpe-dl360-g10"
    assert mod.slugify("  MSA 2060 SAN  ") == "msa-2060-san"


def test_normalize_model_known_and_unknown():
    assert mod.normalize_model("ProLiant DL360 Gen10", SERVER_MODEL_MAP) == "HPE DL360 G10"
    assert mod.normalize_model("  proliant dl380 gen10 plus ", SERVER_MODEL_MAP) == "HPE DL380 G10+"
    # Unknown models pass through unchanged (stripped)
    assert mod.normalize_model("ProLiant DL999 Gen99", SERVER_MODEL_MAP) == "ProLiant DL999 Gen99"
    assert mod.normalize_model("", SERVER_MODEL_MAP) is None


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
