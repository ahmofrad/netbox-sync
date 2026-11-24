# Disposable NetBox 4.5.1 acceptance

This Compose project is exclusively for synthetic local acceptance. It publishes NetBox only on `127.0.0.1:18080`; PostgreSQL and Redis are not published. Do not add production endpoints or credentials.

From the repository root, after building the wheel:

```powershell
python acceptance\prepare.py
docker compose -f acceptance/compose.yaml up -d --wait
docker compose -f acceptance/compose.yaml exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py migrate
docker compose -f acceptance/compose.yaml exec -T --user root netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py collectstatic --no-input
docker compose -f acceptance/compose.yaml exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py check
```

Runtime environment, generated synthetic administrator credentials, and results are Git-ignored. To completely dispose of this project's containers and volumes:

```powershell
docker compose -f acceptance/compose.yaml down -v --remove-orphans
```
