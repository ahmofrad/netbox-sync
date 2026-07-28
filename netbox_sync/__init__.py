"""netbox_sync — HPE/Brocade hardware discovery and NetBox DCIM reconciliation.

Package layout:
    config      — .env loading, credentials, ranges, logging, validation
    models      — vendor model-name normalization maps
    utils       — pure helpers: naming, capacity math, serial handling, IP tools
    netbox      — NetBox API layer (CRUD, ensure/offline devices, inventory sync)
    collectors  — per-protocol hardware collectors (redfish / msa / brocade)
    scanner     — parallel IP-range probing across all device families
    sync        — run_sync orchestrator

Entry point: sync_all_to_netbox.py (scheduler).
"""
