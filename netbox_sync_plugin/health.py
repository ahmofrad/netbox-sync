"""Current managed-inventory health; no synchronization or writes occur here."""
from collections import Counter
from dataclasses import dataclass, field

from netbox_sync.utils import _invalid_serial


MANAGEMENT_IP_FIELDS = (
    ("redfish_enabled", "bmc_ip"), ("storage_enabled", "storage_ip"),
    ("san_switch_enabled", "san_switch_ip"), ("cisco_enabled", "cisco_ip"),
    ("fortigate_enabled", "fortigate_ip"), ("fortiweb_enabled", "fortiweb_ip"),
    ("ftd_enabled", "ftd_ip"), ("wlc_enabled", "wlc_ip"),
    ("unifi_enabled", "unifi_ip"), ("nvr_enabled", "nvr_ip"),
    ("cam_enabled", "cam_ip"),
)
MANAGED_FLAGS = tuple(flag for flag, _ in MANAGEMENT_IP_FIELDS) + ("wap_enabled",)


def _value(record, key, default=None):
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _status(record):
    status = _value(record, "status")
    return _value(status, "value", status)


def _address(record):
    address = _value(record, "primary_ip4")
    if isinstance(address, dict):
        return str(address.get("address") or "").strip()
    return str(address or "").strip()


@dataclass
class HealthCheck:
    key: str
    label: str
    ids: list = field(default_factory=list)
    url: str = ""
    link_truncated: bool = False

    @property
    def count(self):
        return len(self.ids)


@dataclass
class InventoryHealth:
    available: bool
    total: int | None = None
    offline: int = 0
    attention: int = 0
    healthy: int = 0
    checks: dict = field(default_factory=dict)
    message: str = ""

    @classmethod
    def from_queryset(cls, queryset, max_link_ids=100):
        if queryset is None:
            return cls(False, message="Inventory health unavailable")
        try:
            records = [_value(record, "_state", None) and record or record for record in queryset]
            managed = [record for record in records
                       if any((_value(record, "custom_field_data", {}) or {}).get(flag)
                              for flag in MANAGED_FLAGS)]
            checks = {
                key: HealthCheck(key, label) for key, label in (
                    ("missing_management_ip", "Missing management IP"),
                    ("missing_serial", "Missing or invalid hardware serial"),
                    ("duplicate_serial", "Duplicate hardware serial"),
                    ("duplicate_asset_tag", "Duplicate asset tag"),
                )
            }
            serials = Counter()
            tags = Counter()
            for record in managed:
                cf = _value(record, "custom_field_data", {}) or {}
                serial = str(_value(record, "serial", "") or "").strip()
                asset_tag = str(_value(record, "asset_tag", "") or "").strip()
                if not _invalid_serial(serial):
                    serials[serial] += 1
                if asset_tag:
                    tags[asset_tag] += 1
                if _invalid_serial(serial):
                    checks["missing_serial"].ids.append(_value(record, "id"))
                management_present = bool(_address(record)) or any(
                    cf.get(flag) and str(cf.get(field) or "").strip()
                    for flag, field in MANAGEMENT_IP_FIELDS)
                if not management_present:
                    checks["missing_management_ip"].ids.append(_value(record, "id"))
            duplicate_serials = {value for value, count in serials.items() if count > 1}
            duplicate_tags = {value for value, count in tags.items() if count > 1}
            offline = attention = healthy = 0
            for record in managed:
                serial = str(_value(record, "serial", "") or "").strip()
                asset_tag = str(_value(record, "asset_tag", "") or "").strip()
                record_id = _value(record, "id")
                if serial in duplicate_serials:
                    checks["duplicate_serial"].ids.append(record_id)
                if asset_tag in duplicate_tags:
                    checks["duplicate_asset_tag"].ids.append(record_id)
                if _status(record) == "offline":
                    offline += 1
                elif any(record_id in check.ids for check in checks.values()):
                    attention += 1
                else:
                    healthy += 1
            for check in checks.values():
                check.ids.sort()
                check.link_truncated = len(check.ids) > max_link_ids
                ids = check.ids[:max_link_ids]
                check.url = "/dcim/devices/?" + "&".join(f"id={pk}" for pk in ids)
            return cls(True, len(managed), offline, attention, healthy, checks)
        except Exception:
            return cls(False, message="Inventory health unavailable")

    @property
    def healthy_percent(self):
        return round(self.healthy * 100 / self.total) if self.total else 0

    @property
    def chart(self):
        return {"healthy": self.healthy, "attention": self.attention, "offline": self.offline}
