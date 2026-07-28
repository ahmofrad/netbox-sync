"""Regression tests for _fc_interface_type.

Bug history: the original implementation used substring checks in the wrong
order (``"n1" in s`` before ``"n16" in s``), so Brocade N16 and N128 ports
were misclassified as 1GFC interfaces in NetBox.
"""
import sync_all_to_netbox as mod


def test_n16_maps_to_16gfc_not_1gfc():
    assert mod._fc_interface_type("N16") == "16gfc-sfpp"


def test_n128_maps_to_128gfc_not_1gfc():
    assert mod._fc_interface_type("N128") == "128gfc-qsfp28"


def test_all_brocade_speed_tokens():
    assert mod._fc_interface_type("N1") == "1gfc-sfp"
    assert mod._fc_interface_type("N2") == "2gfc-sfp"
    assert mod._fc_interface_type("N4") == "4gfc-sfp"
    assert mod._fc_interface_type("N8") == "8gfc-sfpp"
    assert mod._fc_interface_type("N16") == "16gfc-sfpp"
    assert mod._fc_interface_type("N32") == "32gfc-sfp28"
    assert mod._fc_interface_type("N64") == "64gfc-qsfpp"
    assert mod._fc_interface_type("N128") == "128gfc-qsfp28"


def test_gigabit_and_numeric_forms():
    assert mod._fc_interface_type("1G") == "1gfc-sfp"
    assert mod._fc_interface_type("16G") == "16gfc-sfpp"
    assert mod._fc_interface_type("16") == "16gfc-sfpp"
    assert mod._fc_interface_type("32") == "32gfc-sfp28"


def test_unknown_and_empty_speeds():
    assert mod._fc_interface_type("AN") == "other"
    assert mod._fc_interface_type("--") == "other"
    assert mod._fc_interface_type("") == "other"
    assert mod._fc_interface_type(None) == "other"
