# trips

Production-oriented [OpenTripPlanner](https://www.opentripplanner.org/) (OTP)
deployment using the **official `opentripplanner/opentripplanner:2.10.0` image**.
An existing Traefik container terminates HTTPS and password-protects every OTP
endpoint. The default hostname is **trips.simovi.org**.

The stack follows OTP's [container instructions](https://docs.opentripplanner.org/en/dev-2.x/Container-Image/):
build and save a graph once, then load it on each server startup. No custom image,
Traefik deployment, host port publishing, or automatic feed updates are included.

## Prerequisites

- Docker Engine and Docker Compose v2 or newer.
- A running Traefik v2/v3 with the Docker provider enabled, connected to the
  existing **`traefik_proxy`** network.
- Traefik HTTP/HTTPS entrypoints (defaults: `web`/`websecure`) and an existing
  certificate resolver (default: `letsencrypt`). This stack does not configure
  Traefik's ACME account, challenge, Docker socket, or certificates.
- DNS A/AAAA records for your hostname pointing at Traefik, and ports 80/443
  reachable as required by your certificate setup.
- `curl` and an `htpasswd` implementation supporting BCrypt (`-B`, for example
  Apache `apache2-utils` on Debian/Ubuntu or `httpd` from Homebrew).
- Sufficient disk/RAM for your region. Defaults allow **12 GiB for graph building**
  (8 GiB JVM heap) and **6 GiB for serving** (4 GiB heap). These are starting
  points, not guarantees for a particular dataset; size the host accordingly.

## 1. Configure

Run commands from this repository's root:

```sh
cp .env.example .env
chmod 600 .env
mkdir -p data
id -u
id -g
```

Edit `.env`. On Linux, set `OTP_UID`/`OTP_GID` to the numeric owner of `data/`,
using the values reported above. The builder needs write access; the runtime
mounts only the saved graph read-only, not the input feeds. On Docker Desktop,
ensure the directory is shared with Docker.

Important variables (all defaults are in `.env.example`):

- `OTP_HOST`: public hostname, without scheme/path; default `trips.simovi.org`.
- `TRAEFIK_NETWORK`: existing external network, default `traefik_proxy`.
- `TRAEFIK_HTTP_ENTRYPOINT` / `TRAEFIK_HTTPS_ENTRYPOINT`: existing entrypoint names.
- `TRAEFIK_CERT_RESOLVER`: existing ACME resolver name. Set it to an empty value
  if certificates are already supplied by Traefik's default TLS store.
- `OTP_AUTH_USERS`: required BCrypt `username:hash` entry; see below.
- `OTP_IMAGE`: official image tag (or digest-pinned reference). Keep the builder
  and runtime on the same version. Avoid `latest` in production.
- `OTP_DATA_DIR`: persistent data path, default `./data`. Relative paths are
  relative to this Compose file. If changed, adjust all `data/` commands below.
- `OTP_JAVA_OPTIONS` / `OTP_BUILD_JAVA_OPTIONS`: server/build JVM options.
- `OTP_MEMORY_LIMIT` / `OTP_BUILD_MEMORY_LIMIT`: container memory limits.
  Keep heap sizes below these limits to leave native-memory headroom.
- `OTP_CPUS` / `OTP_BUILD_CPUS`: CPU limits.
- `OTP_HEALTH_START_PERIOD`: graph-loading grace period, default `180s`;
  increase for large graphs.

### Set the API password

Generate a BCrypt hash interactively. The password is not placed in shell
history, command-line arguments, Docker labels, or `.env` in plaintext:

```sh
OTP_AUTH_USERS="$(htpasswd -nB otp)" &&
  printf "\nOTP_AUTH_USERS='%s'\n" "$OTP_AUTH_USERS" >> .env
unset OTP_AUTH_USERS
```

This appends the generated entry, overriding the initial empty assignment.
Use a strong, unique password and change `otp` to your preferred username.
For subsequent rotations, remove the old `OTP_AUTH_USERS` assignments before
generating the replacement, then recreate the service.

**Keep the single quotes and literal `$` characters in `.env`; do not double
them.** Compose reads single-quoted `.env` values literally. Dollar-sign doubling
is needed for hashes hardcoded directly in Compose YAML, which this stack avoids.
Multiple users can be supplied as comma-separated `username:hash` entries.
Treat the hash as sensitive: `.env` is gitignored, but Docker administrators and
anyone able to inspect resolved Compose configuration can see it. Use
`docker compose config --quiet` instead of printing the resolved configuration.
Compose refuses to run if `OTP_AUTH_USERS` is unset or empty.

## 2. Download input data

OTP needs street data covering the transit service area as well as GTFS. For
Costa Rica, this example uses Geofabrik's country extract and the provided
INCOFER feed. URLs can be overridden in the shell:

```sh
GTFS_URL="${GTFS_URL:-https://feeds.simovi.org/incofer/schedule/gtfs.zip}"
OSM_URL="${OSM_URL:-https://download.geofabrik.de/central-america/costa-rica-latest.osm.pbf}"

curl --fail --location --retry 3 "$GTFS_URL" -o data/incofer.gtfs.zip.download &&
  mv data/incofer.gtfs.zip.download data/incofer.gtfs.zip
curl --fail --location --retry 3 "$OSM_URL" -o data/costa-rica.osm.pbf.download &&
  mv data/costa-rica.osm.pbf.download data/costa-rica.osm.pbf
```

The temporary names prevent failed downloads from replacing valid inputs. Feed
names **must contain `gtfs` and end in `.zip`**, and OSM files must end in `.pbf`.
OTP scans the data directory: keep only the intended input feeds/extracts there,
not old copies with matching extensions. For a smaller deployment, use a clipped
OSM extract covering all stops and access streets.

Downloads are a host-side preparation step; the builder has no network access.
Do not put remote input URLs in OTP configuration without changing this policy.
Check GTFS validity dates and OSM coverage before building. A graph can load
successfully while containing no transit service for the date you query.
Respect feed terms and [OpenStreetMap attribution/licensing](https://www.openstreetmap.org/copyright).

## 3. Build the graph and start

```sh
docker network inspect traefik_proxy
docker compose config --quiet
docker compose --profile build pull
docker compose --profile build run --rm otp-build
test -s data/graph.obj
docker compose up -d --wait --wait-timeout 600 otp
docker compose ps
docker compose logs --follow --tail 100 otp
```

Use your configured network name for `docker network inspect` if different.
The network must already exist and contain Traefik; do not create a separate
replacement network. `otp-build` runs only when explicitly invoked with its
profile. A failed build must be fixed before starting the runtime.
The builder copies `config/otp-config.json` into the data directory before
invoking the official image entrypoint. Edit the source in `config/`, not the
generated copy in `data/`.

The runtime loads `data/graph.obj` and listens on container port 8080. A missing
graph fails the bind mount rather than creating an empty directory. Only
Traefik receives public traffic; **there is deliberately no `ports:` mapping**.
Containers run as a non-root UID, with dropped capabilities, a read-only root
filesystem, temporary `/tmp`, bounded logs, and resource limits.

`config/otp-config.json` enables the GTFS GraphQL and actuator APIs. Health checks
probe `/otp/actuators/health` inside the container without requiring curl/wget in
the image. Traefik normally excludes unhealthy containers. Docker's restart
policy restarts exited processes, not merely unhealthy ones; monitor health and
logs externally. `--wait` verifies local readiness, not DNS/TLS/authentication.

### Verify public routing and authentication

After the container is healthy:

```sh
OTP_BASE_URL="${OTP_BASE_URL:-https://trips.simovi.org}"

# Expect an HTTPS redirect; never send credentials to an HTTP URL.
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  "http://${OTP_BASE_URL#https://}/otp/actuators/health"

# Expect 401 without credentials.
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  "$OTP_BASE_URL/otp/actuators/health"

# Prompts for the password; expect 200.
curl --fail-with-body --user otp --output /dev/null --write-out '%{http_code}\n' \
  "$OTP_BASE_URL/otp/actuators/health"
```

All OTP paths, including GraphiQL, health, and metrics, require authentication
through Traefik. The HTTP router only redirects; it never forwards to OTP.
Do not use `curl -k` or disable certificate verification.

**Trust boundary:** Basic Auth is enforced by Traefik, not by OTP. Other containers
on `traefik_proxy` and host/Docker administrators can reach OTP directly and
bypass it. Only attach trusted workloads to that network, protect Docker access,
and do not expose port 8080 or add an unauthenticated router. This is not a
multi-tenant network isolation scheme. Traefik strips Authorization before
forwarding requests; the backend connection is HTTP on the Docker network.

Router/middleware/service names use the fixed `trips-otp` prefix. If deploying
multiple copies behind the same Traefik, rename these labels and their
references to avoid collisions; changing only `COMPOSE_PROJECT_NAME` is not enough.

## API examples

OTP 2.10's [GTFS GraphQL API](https://docs.opentripplanner.org/en/v2.10.0/apis/GTFS-GraphQL-API/)
is at **`https://trips.simovi.org/otp/gtfs/v1`**. These examples do not use the old
OTP 1 REST `/otp/routers/default/plan` endpoint. Override `OTP_BASE_URL` for another
hostname. GraphQL can return HTTP 200 with an `errors` array: check the JSON
response, not only the HTTP status.

### curl: routes and stops

`--user otp` prompts for the password rather than storing it in your command
history or process arguments:

```sh
OTP_BASE_URL="${OTP_BASE_URL:-https://trips.simovi.org}"
curl --fail-with-body --user otp \
  --header 'Content-Type: application/json' \
  --data '{"query":"{ routes { gtfsId shortName longName mode agency { name } } }"}' \
  "$OTP_BASE_URL/otp/gtfs/v1"

curl --fail-with-body --user otp \
  --header 'Content-Type: application/json' \
  --data '{"query":"{ stops { gtfsId name lat lon } }"}' \
  "$OTP_BASE_URL/otp/gtfs/v1"
```

These list queries return all loaded routes/stops; keep response size in mind
for large multi-agency graphs.

### curl: plan a trip in San José

This uses OTP's `planConnection` query. Change the coordinates and supply a
departure with a timezone offset **within the feed's service period**. Costa Rica
uses UTC−06:00. Walking-only results or no itineraries do not necessarily indicate
a server failure.

```sh
OTP_BASE_URL="${OTP_BASE_URL:-https://trips.simovi.org}"
printf 'Departure (ISO 8601, e.g. 2026-11-02T07:00:00-06:00): '
read -r DEPARTURE
curl --fail-with-body --user otp \
  --header 'Content-Type: application/json' \
  --data @- "$OTP_BASE_URL/otp/gtfs/v1" <<JSON
{
  "query": "query Trip(\$departure: OffsetDateTime!) { planConnection(origin: { location: { coordinate: { latitude: 9.933, longitude: -84.077 } } }, destination: { location: { coordinate: { latitude: 9.998, longitude: -84.117 } } }, dateTime: { earliestDeparture: \$departure }, modes: { direct: [WALK], transit: { transit: [{ mode: RAIL }, { mode: BUS }] } }) { edges { node { start end legs { mode from { name } to { name } route { shortName longName } } } } } }",
  "variables": { "departure": "$DEPARTURE" }
}
JSON
```

The sample coordinates are illustrative, not a guarantee of INCOFER service.
Find actual stop locations with the stops query. For schema exploration, open
`https://trips.simovi.org/graphiql` and authenticate.

### Python: list routes and plan a trip

This example uses only the Python 3 standard library. It prompts securely for
the password, validates HTTPS, has a request timeout, and checks GraphQL errors.
Set `OTP_BASE_URL` and `OTP_USERNAME` in your environment if needed. For
noninteractive automation, provision the password through a secret manager,
not a hardcoded string or committed script.

```python
import base64
import getpass
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

base_url = os.environ.get("OTP_BASE_URL", "https://trips.simovi.org").rstrip("/")
if urlsplit(base_url).scheme != "https":
    raise ValueError("Use HTTPS to protect Basic Auth credentials")
username = os.environ.get("OTP_USERNAME", "otp")
password = getpass.getpass("OTP password: ")
token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")


def graphql(query, variables=None):
    request = Request(
        f"{base_url}/otp/gtfs/v1",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"OTP HTTP error: {exc.code}") from None
    except URLError:
        raise RuntimeError("OTP connection or TLS failure") from None
    if result.get("errors"):
        raise RuntimeError(result["errors"])
    return result["data"]


routes = graphql("{ routes { gtfsId shortName longName mode } }")
print(json.dumps(routes, indent=2, ensure_ascii=False))

# Enter an offset-aware date covered by the loaded GTFS schedule.
departure = input("Departure (e.g. 2026-11-02T07:00:00-06:00): ").strip()
trip = graphql(
    """
    query Trip($departure: OffsetDateTime!) {
      planConnection(
        origin: {
          location: { coordinate: { latitude: 9.933, longitude: -84.077 } }
        }
        destination: {
          location: { coordinate: { latitude: 9.998, longitude: -84.117 } }
        }
        dateTime: { earliestDeparture: $departure }
        modes: {
          direct: [WALK]
          transit: { transit: [{ mode: RAIL }, { mode: BUS }] }
        }
      ) {
        edges {
          node {
            start
            end
            legs {
              mode
              from { name }
              to { name }
              route { shortName longName }
            }
          }
        }
      }
    }
    """,
    {"departure": departure},
)
print(json.dumps(trip, indent=2, ensure_ascii=False))
```

## Operations

- **Stop:** `docker compose down`. Input files and graph remain in `data/`.
- **Logs/status:** `docker compose logs --tail 100 otp` and `docker compose ps`.
- **Password rotation:** replace the hash in `.env`, then
  `docker compose up -d --force-recreate otp`. A plain restart does not apply new
  environment/labels.
- **Feed/OSM refresh:** download new inputs, stop OTP with
  `docker compose stop otp`, back up the current graph outside `data/`, rerun
  `docker compose --profile build run --rm otp-build`, and only after success run
  `docker compose up -d --force-recreate --wait --wait-timeout 600 otp`.
  This simple workflow has downtime; never rebuild/overwrite a live graph.
  Restore the backed-up graph if a build fails. For zero-downtime upgrades,
  build in a separate directory/host and deploy a tested blue/green instance.
- **Upgrade:** back up `.env`, configuration, graph, and source-data versions;
  change `OTP_IMAGE`, pull, rebuild the graph with that image, and recreate OTP.
  Serialized graphs are version-sensitive; do not assume an older graph will load.
  Roll back the image **and matching graph/configuration** together.
- **Backup:** preserve data/configuration plus the image version (ideally digest).
  Store backups outside the input directory and protect credential backups.
- **Monitoring:** alert on health failures, restarts/OOM, disk usage, certificate
  expiry, stale feeds, and GTFS service coverage. Authenticated Prometheus metrics
  are at `/otp/actuators/prometheus`.
- **Troubleshooting:** 401 means missing/incorrect credentials; 404/502/503 may mean
  an unhealthy OTP, a mismatched Traefik entrypoint/resolver/network, or router
  conflicts. A missing `graph.obj` needs a successful build first. Permission
  failures need correct data ownership and readable config/scripts. OOM requires
  more memory, a smaller OSM extract, or adjusted heap/container limits.

## Local validation

```sh
python3 -m unittest discover -s tests -v
```

The tests require Docker Compose but do not start services, download data, or
modify your `.env`. They check configuration, fail-closed authentication,
literal hash interpolation, environment overrides, and readiness behavior.

## References

- [Official container image](https://hub.docker.com/r/opentripplanner/opentripplanner)
- [OTP container instructions](https://docs.opentripplanner.org/en/dev-2.x/Container-Image/)
- [Version-matched GraphQL tutorial](https://docs.opentripplanner.org/en/v2.10.0/apis/GraphQL-Tutorial/)
- [Traefik Basic Auth](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/basicauth/)
