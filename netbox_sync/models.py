"""Device model normalization maps for the NetBox sync tool.

HPE ProLiant server, HPE MSA storage and HPE B-Series (Brocade OEM) SAN
switch model-name aliases live here so the sync collectors can stay
product-agnostic. Import via:

    from netbox_sync.models import (SERVER_MODEL_MAP, STORAGE_MODEL_MAP,
                                    SWITCH_MODEL_MAP, CISCO_MODEL_MAP)

Keys are the raw vendor strings (lowercased) as returned by Redfish
(servers), the MSA XML API (storage) or the Brocade CLI `switchshow`
output (SAN switches); values are the canonical NetBox device-type
model names.
"""

# ── HPE ProLiant servers ─────────────────────────────────────────────────────
# Keys: raw Redfish "Model" string, lowercased.
SERVER_MODEL_MAP = {
    "proliant dl360 gen8":       "HPE DL360 G8",
    "proliant dl360p gen8":      "HPE DL360 G8",
    "proliant dl380 gen8":       "HPE DL380 G8",
    "proliant dl380p gen8":      "HPE DL380 G8",
    "proliant dl360 gen9":       "HPE DL360 G9",
    "proliant dl380 gen9":       "HPE DL380 G9",
    "proliant dl360 gen10":      "HPE DL360 G10",
    "proliant dl380 gen10":      "HPE DL380 G10",
    "proliant dl360 gen10 plus": "HPE DL360 G10+",
    "proliant dl380 gen10 plus": "HPE DL380 G10+",
    "proliant dl320 gen11":      "HPE DL320 G11",
    "proliant dl360 gen11":      "HPE DL360 G11",
    "proliant dl380 gen11":      "HPE DL380 G11",
}

# ── HPE MSA storage arrays ───────────────────────────────────────────────────
# Keys: raw MSA "product-id" string, lowercased.
# Note: MSA 2040-class firmware reports disk fields via "show disk-parameters"
# while newer MSA 2060-class reports them via "show disks". The storage
# collector tries both commands, so this map does not drive that logic.
STORAGE_MODEL_MAP = {
    "msa 2040 san":  "HPE MSA 2040",
    "msa 2040":      "HPE MSA 2040",
    "msa 2042 san":  "HPE MSA 2042",
    "msa 2050 san":  "HPE MSA 2050",
    "msa 2052 san":  "HPE MSA 2052",
    "msa 2060 san":  "HPE MSA 2060",
    "msa 2060":      "HPE MSA 2060",
    "msa 2062 san":  "HPE MSA 2062",
    "msa 2062":      "HPE MSA 2062",
}

# ── HPE B-Series (Brocade OEM) SAN switches ──────────────────────────────────
# Keys: raw Brocade `switchshow` "switchType" / model string, lowercased.
SWITCH_MODEL_MAP = {
    # Brocade / HPE B-Series common models (HPE SNxxxx = rebadged Brocade)
    "brocade 300":              "HPE B-series 300",
    "brocade 320":              "HPE B-series 320",
    "brocade 5100":             "HPE B-series 5100",
    "brocade 5300":             "HPE B-series 5300",
    "brocade 6505":             "HPE B-series 6505",
    "brocade 6510":             "HPE B-series 6510",
    "brocade 6520":             "HPE B-series 6520",
    "brocade 6547":             "HPE B-series 6547",
    "brocade 7800":             "HPE B-series 7800",
    "brocade 7840":             "HPE B-series 7840",
    "brocade dcx 4s":           "HPE B-series DCX 4s",
    "brocade dcx-4s":           "HPE B-series DCX 4s",
    "brocade sx6":              "HPE B-series SX6",
    "hpe sn6500b":              "HPE SN6500B",
    "hpe sn6010b":              "HPE SN6010B",
    "hpe sn6010c":              "HPE SN6010C",
    "hpe sn6500c":              "HPE SN6500C",
    "hpe sn6700b":              "HPE SN6700B",
    "hpe sn8700c":              "HPE SN8700C",
    "hpe sn8600c":              "HPE SN8600C",
    # Fallback friendly names for bare Brocade model strings
    "300":                      "HPE B-series 300",
    "6505":                     "HPE B-series 6505",
    "6510":                     "HPE B-series 6510",
    "6520":                     "HPE B-series 6520",
}

# ── Cisco Catalyst switches ──────────────────────────────────────────────────
# Keys: raw `show version` model string, lowercased. Cisco PIDs are nearly
# canonical already; keep aliases for common reporting variants.
CISCO_MODEL_MAP = {
    "ws-c2960x-48fps-l": "Cisco WS-C2960X-48FPS-L",
    "ws-c2960x-24ps-l":  "Cisco WS-C2960X-24PS-L",
    "c9300-48u":         "Cisco C9300-48U",
    "c9300-48p":         "Cisco C9300-48P",
    "c9300-24t":         "Cisco C9300-24T",
    "c9200l-48p-4g":     "Cisco C9200L-48P-4G",
    "c9200l-48t-4x":     "Cisco C9200L-48T-4X",
    "c9200-48p":         "Cisco C9200-48P",
    "c3850-48p":         "Cisco WS-C3850-48P",
    "ws-c3850-48p":      "Cisco WS-C3850-48P",
}
