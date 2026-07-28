"""Tests for the HPE MSA XML API response parsing."""
import pytest
from xml.etree import ElementTree as ET

import netbox_sync.collectors.msa as mod


SAMPLE = """<RESPONSE>
  <OBJECT basetype="drives" name="0.1" oid="1">
    <PROPERTY name="serial-number">SN12345</PROPERTY>
    <PROPERTY name="size">1.8TB</PROPERTY>
  </OBJECT>
  <OBJECT basetype="status" name="status" oid="0">
    <PROPERTY name="response-type">Success</PROPERTY>
  </OBJECT>
</RESPONSE>"""


def test_parse_objects_skips_status_and_collects_properties():
    objects = mod.StorageSession._parse_objects(ET.fromstring(SAMPLE))
    assert len(objects) == 1
    obj = objects[0]
    assert obj["basetype"] == "drives"
    assert obj["serial-number"] == "SN12345"
    assert obj["size"] == "1.8TB"


def test_response_status_success():
    xml = ET.fromstring(
        '<RESPONSE><OBJECT name="status">'
        '<PROPERTY name="response-type">Success</PROPERTY>'
        '<PROPERTY name="response">OK</PROPERTY>'
        '</OBJECT></RESPONSE>')
    props = mod.StorageSession._response_status(xml)
    assert props["response"] == "OK"


def test_response_status_error_raises():
    xml = ET.fromstring(
        '<RESPONSE><OBJECT name="status">'
        '<PROPERTY name="response-type">Error</PROPERTY>'
        '<PROPERTY name="response">Authorization failed</PROPERTY>'
        '</OBJECT></RESPONSE>')
    with pytest.raises(RuntimeError, match="Authorization failed"):
        mod.StorageSession._response_status(xml)


def test_response_status_missing_object_raises():
    xml = ET.fromstring("<RESPONSE></RESPONSE>")
    with pytest.raises(RuntimeError, match="missing status"):
        mod.StorageSession._response_status(xml)
