# Local usage dashboard service

`server/hub.py` collects usage events from each configured CLIProxyAPI instance, serves the
compiled official frontend from `/opt/pi-hub-metrics/current/index.html`, and exposes an
organization-scoped same-origin management gateway on `127.0.0.1:8320`. It uses only Python's
standard library. Releases live under
`/opt/pi-hub-metrics/releases/<commit>/`; `/opt/pi-hub-metrics/current` points at the selected
release. Keep the existing root `metrics.py` and `index.html` in place for rollback.

The config file at `/etc/pi-hub-metrics/config.json` is provisioned on the host and must not be
committed. Shape:

```json
{
  "public_origin": "https://panel.example.ts.net",
  "clients": [
    {"name": "oasis", "label": "Oasis", "port": 8317, "key": "SET_ON_HOST"},
    {"name": "stradem", "label": "Stradem", "port": 8318, "key": "SET_ON_HOST"}
  ]
}
```

`public_origin` is optional and must be the exact HTTPS origin used by the browser (for example the
TailScale Serve hostname); it grants no network binding, and the service still binds only to
loopback. Without it, the dashboard accepts loopback hosts/origins only. Use unique lowercase
alphanumeric organization names with optional hyphens; `all` is reserved. Labels are clipped to 60
characters. The dashboard exposes `/api/organizations`, `/api/summary`, and
`/export.csv` as JSON/CSV derived from stored usage aggregates, plus `/` for the single-file
frontend. Summary `days` is bounded to 1–365, and organization filters use only configured names.
Host/Origin validation protects routing and cross-site requests; it is not user authentication.
Restrict the public origin with TailScale ACLs or another access-control layer.

Native admin API calls use `/organizations/<configured-id>/v8/management/*`. The gateway always
connects to that organization's configured loopback port, replaces browser Authorization with the
server-side management key, and never returns that key. Every gateway call must include
`X-Hub-Request: 1`; browser requests must be same-origin and use an allowed loopback or configured
public host. The gateway rejects
cross-site fetches, unknown organizations, paths with traversal/double encoding, external absolute
upstream redirects, unsupported methods, oversized bodies, and unapproved content types. Browser
cookies, Authorization, and forwarding headers are never passed upstream. Request/response headers
are allowlisted; API status, binary bodies, ETag, Content-Disposition, Content-Type, and CLI Proxy
version/build/plugin-capability headers are preserved. Body limit is 64 MiB; response limit is 128
MiB. The usage summary and CSV endpoints remain read-only.

Model discovery uses `/organizations/<configured-id>/v1/models`. The server reads that backend's
`/v8/management/config/access/api-keys` through its management key and uses a configured client API
key for `/v1/models`; browser-stored keys are ignored. Plugin resource URLs are available only when
the backend reports `X-CPA-SUPPORT-PLUGIN: 1` and the exact resource path appears in its plugin menu
metadata. Unsupported backends and undeclared paths return 404; there is no arbitrary upstream path
proxy.

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
