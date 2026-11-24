"""Create only synthetic users, tokens, and inventory in disposable NetBox."""
import json
import os
from pathlib import Path

from django.contrib.auth import get_user_model
from users.models import Token
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

credentials = json.loads(Path("/opt/netbox-sync-runtime/credentials.json").read_text(encoding="utf-8"))
User = get_user_model()
admin, _ = User.objects.get_or_create(username=credentials["username"], defaults={
    "is_superuser": True, "is_active": True, "email": "acceptance@example.invalid"})
admin.set_password(credentials["password"])
admin.is_superuser = admin.is_active = True
admin.save()
Token.objects.filter(user=admin, key=None, token=os.environ["NETBOX_TOKEN"], version=1).delete()
Token.objects.create(user=admin, key=None, token=os.environ["NETBOX_TOKEN"], version=1, write_enabled=True)
Token.objects.filter(user=admin, key=None, token=os.environ["MCP_NETBOX_TOKEN"], version=1).delete()
Token.objects.create(user=admin, key=None, token=os.environ["MCP_NETBOX_TOKEN"], version=1, write_enabled=False)

site, _ = Site.objects.get_or_create(name="Synthetic Acceptance Site", slug="synthetic-acceptance")
manufacturer, _ = Manufacturer.objects.get_or_create(name="Synthetic Vendor", slug="synthetic-vendor")
device_type, _ = DeviceType.objects.get_or_create(manufacturer=manufacturer, model="Fixture Server", slug="fixture-server")
role, _ = DeviceRole.objects.get_or_create(name="Server", slug="server", defaults={"color": "607d8b", "vm_role": False})
for index in range(1, 4):
    device, _ = Device.objects.get_or_create(name=f"fixture-server-{index}", defaults={
        "site": site, "device_type": device_type, "role": role,
        "serial": f"FIXTURE-{index:03d}", "status": "active",
        "custom_field_data": {"redfish_enabled": True},
    })
print(f"Synthetic acceptance inventory ready: {Device.objects.filter(site=site).count()} devices")
