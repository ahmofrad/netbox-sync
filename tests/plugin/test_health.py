"""Current managed-inventory health evaluation, independent of run history."""
from types import SimpleNamespace

from netbox_sync_plugin.health import InventoryHealth


def device(pk, *, flags=None, cf=None, serial="VALID-SERIAL", asset_tag=None,
           status="active", primary_ip4="192.0.2.1/32"):
    if flags is None:
        flags = {"redfish_enabled": True}
    return SimpleNamespace(
        id=pk, custom_field_data={**flags, **(cf or {})},
        serial=serial, asset_tag=asset_tag, status=status, primary_ip4=primary_ip4)


def test_scope_and_offline_precedence_with_overlapping_details():
    records = [
        device(1, status="offline", serial="", primary_ip4=None, cf={"bmc_ip": ""}),
        device(2, primary_ip4=None, cf={"bmc_ip": "192.0.2.2"}),
        device(3, serial="", primary_ip4=None),
        device(4, flags={"wap_enabled": True}, primary_ip4=None),
        device(5, flags={}, serial=""),
    ]
    result = InventoryHealth.from_queryset(records)
    assert (result.total, result.offline, result.attention, result.healthy) == (4, 1, 3, 0)
    assert result.offline + result.attention + result.healthy == result.total
    assert result.checks["missing_management_ip"].ids == [1, 3, 4]
    assert result.checks["missing_serial"].ids == [1, 3]


def test_all_management_ip_fields_and_primary_override():
    fields = (
        ("storage_enabled", "storage_ip"), ("san_switch_enabled", "san_switch_ip"),
        ("cisco_enabled", "cisco_ip"), ("fortigate_enabled", "fortigate_ip"),
        ("fortiweb_enabled", "fortiweb_ip"), ("ftd_enabled", "ftd_ip"),
        ("wlc_enabled", "wlc_ip"), ("unifi_enabled", "unifi_ip"),
        ("nvr_enabled", "nvr_ip"), ("cam_enabled", "cam_ip"),
    )
    records = [device(index + 1, flags={flag: True, field: "192.0.2.10"},
                      serial=f"UNIQUE-{index}", primary_ip4=None)
               for index, (flag, field) in enumerate(fields)]
    result = InventoryHealth.from_queryset(records)
    assert result.healthy == len(fields)
    assert not result.checks["missing_management_ip"].ids


def test_duplicate_serial_and_tag_use_valid_nonblank_values_only():
    records = [
        device(1, serial="DUP-SERIAL", asset_tag="TAG-SHARED"),
        device(2, serial="DUP-SERIAL", asset_tag="TAG-SHARED"),
        device(3, serial="UNKNOWN", asset_tag=""),
        device(4, serial="UNKNOWN", asset_tag=""),
    ]
    result = InventoryHealth.from_queryset(records)
    assert result.checks["duplicate_serial"].ids == [1, 2]
    assert result.checks["duplicate_asset_tag"].ids == [1, 2]
    assert result.checks["missing_serial"].ids == [3, 4]


def test_links_use_ids_not_serials_and_are_bounded():
    records = [device(index, serial="SHARED", primary_ip4=None) for index in range(1, 6)]
    result = InventoryHealth.from_queryset(records, max_link_ids=3)
    link = result.checks["duplicate_serial"].url
    assert "SHARED" not in link
    assert "id=1&id=2&id=3" in link
    assert result.checks["duplicate_serial"].link_truncated is True


def test_unavailable_does_not_look_like_empty_healthy_inventory():
    class Broken:
        def __iter__(self):
            raise RuntimeError("query failed token=fake-health-token")
    result = InventoryHealth.from_queryset(Broken())
    assert result.available is False
    assert result.total is None
    assert "fake-health-token" not in result.message
    assert result.message == "Inventory health unavailable"


def test_explicit_unavailable_and_unpermitted_querysets():
    result = InventoryHealth.from_queryset(None)
    assert result.available is False
    assert result.checks == {}
