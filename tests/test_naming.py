"""Tests for inventory item naming helpers (servers + storage)."""
import sync_all_to_netbox as mod


def test_name_cpu_strips_vendor_prefix():
    assert mod.name_cpu({"Model": "Intel Xeon Gold 6230 CPU @ 2.10GHz"}) == \
        "Xeon Gold 6230 CPU @ 2.10GHz"
    assert mod.name_cpu({"Model": "AMD EPYC 7543 32-Core Processor"}) == \
        "EPYC 7543 32-Core Processor"
    assert mod.name_cpu({}) == "CPU"


def test_name_ram_capacity_and_speed():
    mm = {"CapacityMiB": 32768, "OperatingSpeedMhz": 3200,
          "MemoryDeviceType": "DDR4 SDRAM"}
    assert mod.name_ram(mm) == "RAM 32GB 3200"
    assert mod.name_ram({"CapacityMiB": 16384}) == "RAM 16GB"
    assert mod.name_ram({}) == "RAM"


def test_name_disk_ssd_with_capacity_and_protocol():
    drv = {"CapacityBytes": 480032981402, "MediaType": "SSD", "Protocol": "SATA"}
    assert mod.name_disk(drv) == "SSD 480GB SATA"


def test_name_disk_hdd_snapped_to_standard_size():
    drv = {"CapacityBytes": 1200000000000, "MediaType": "HDD",
           "RotationSpeedRPM": 10000}
    assert mod.name_disk(drv) == "HDD 1.2TB SAS"


def test_name_psu():
    assert mod.name_psu({"PowerCapacityWatts": 800}) == "PSU 800W"
    assert mod.name_psu({"Model": "HPE 500W Flex Slot"}) == "PSU 500W"
    assert mod.name_psu({}) == "PSU"


def test_name_nic():
    pci = {"DeviceLocation": "PCI-E Slot 2"}
    assert mod.name_nic("HPE Ethernet 10Gb 2-port 530SFP+ Adapter", pci) == \
        "530SFP+-Slot2"
    assert mod.name_nic("", None) == "NIC"


def test_name_hba():
    name = mod.name_hba("Emulex LPe16002 16Gb 2-port FC HBA", "PCI-E Slot 4")
    assert name == "HBA-16Gb-Slot4"
    assert mod.name_hba("", "") == "HBA"


def test_is_ssd():
    assert mod.is_ssd({"MediaType": "SSD"}) is True
    assert mod.is_ssd({"MediaType": "HDD"}) is False
    assert mod.is_ssd({"Model": "Samsung SSD 860 EVO"}) is True
    assert mod.is_ssd({"Model": "WD Blue 1TB"}) is False


def test_is_ssd_storage_handles_firmware_field_differences():
    # "show disks" (newer firmware) field names
    assert mod.is_ssd_storage({"drive-type": "SSD", "model": "HPE 480GB"}) is True
    assert mod.is_ssd_storage({"drive-type": "SAS"}) is False
    # "show disk-parameters" (older firmware) field names
    assert mod.is_ssd_storage({"disk-type": "SSD"}) is True
    assert mod.is_ssd_storage({"disk-description": "FLASH drive"}) is True
    assert mod.is_ssd_storage({}) is False


def test_name_storage_disk():
    assert mod.name_storage_disk(
        {"size": "1.8TB", "drive-type": "SAS", "serial-number": "X"}) == "HDD 1.8TB"
    assert mod.name_storage_disk(
        {"size": "480GB", "drive-type": "SSD"}) == "SSD 480GB"
    # disk-statistics rows: no size/model -> fall back to location
    assert mod.name_storage_disk({"location": "1.5"}) == "HDD 1.5"


def test_name_storage_psu_and_controller():
    assert mod.name_storage_psu({"location": "1.1"}) == "PSU 1.1"
    assert mod.name_storage_controller({"controller-id": "A"}) == "Controller A"
