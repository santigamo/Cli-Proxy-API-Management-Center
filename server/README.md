# Local usage dashboard service

`server/hub.py` collects usage events from each configured CLIProxyAPI instance and serves the
compiled official frontend from `/opt/pi-hub-metrics/current/index.html` on `127.0.0.1:8320`. It
uses only Python's standard library. Releases live under
`/opt/pi-hub-metrics/releases/<commit>/`; `/opt/pi-hub-metrics/current` points at the selected
release. Keep the existing root `metrics.py` and `index.html` in place for rollback.

The config file at `/etc/pi-hub-metrics/config.json` is provisioned on the host and must not be
committed. Shape:

```json
{
  "clients": [
    {"name": "oasis", "label": "Oasis", "port": 8317, "key": "SET_ON_HOST"},
    {"name": "stradem", "label": "Stradem", "port": 8318, "key": "SET_ON_HOST"}
  ]
}
```

Use unique lowercase alphanumeric names with optional hyphens. Labels are clipped to 60 characters.
The API exposes only `/api/organizations`, `/api/summary`, and `/export.csv` as JSON/CSV derived
from stored usage aggregates, plus `/` for the single-file frontend. Every API query validates
`days` from 1 to 365 and validates the requested organization against configured names. The browser
never receives the management key. There is no upstream proxy route or write endpoint.

The service stores a fixed metadata allowlist in the existing SQLite schema at
`/var/lib/pi-hub-metrics/usage.sqlite3`; prompt, response, API key, headers, and arbitrary queue
fields are discarded before persistence. The existing `events` and `health` schema is retained for
database compatibility. `server/pi-hub-metrics.service` is the systemd unit template for host-side
installation. Build the single-file UI with `VITE_HUB_MODE=true bun run build`, then install
`dist/index.html` and `server/hub.py` into a versioned release (preserving the `server/` directory).

## Queue semantics

The upstream `GET /v8/management/observability/usage/queue` endpoint pops records from a queue. This
collector therefore has a nonblocking `flock` on `collector.lock` to prevent a second consumer from
stealing events. A process crash after a pop and before its SQLite commit can lose that popped batch;
the queue endpoint does not provide an acknowledgment/transaction protocol. Keep this as the sole
consumer on each proxy instance and avoid running a second collector while the service is active.

## Run and verify

```sh
python3 server/hub.py --db /tmp/usage.sqlite3 --config /etc/pi-hub-metrics/config.json \
  --ui /path/to/dist/index.html
python3 -m unittest discover -s server/tests -v
```
