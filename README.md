# nc-link-watcher

> NOTE: Fully Vibe Coded with Claude.

Keeps the links in Nextcloud's External Sites app in step with Docker container labels.

- A running container with a `nextcloud-links.url` label gets a link.
- When the container stops, or its labels are removed, the link is removed after a grace period.
- One watcher can read several Docker hosts.
- The watcher only changes links listed in its own `data/state.json` (link key -> Nextcloud link
  id). Links made by hand are never touched.
- Link ids are kept wherever possible: changes are made in place, a renamed container keeps its
  link, and so does a container that is back within the grace period. Each user's own app menu
  order is tied to these ids.

## Setup

1. In Nextcloud, enable the **External sites** app and create an app password for an admin account
   (Settings -> Security -> Devices & sessions).
2. Fill in `NEXTCLOUD_URL`, `NEXTCLOUD_USER` and `NEXTCLOUD_APP_PASSWORD` in `docker-compose.yml`.
3. `docker compose up -d`, then `docker logs -f nc-link-watcher`.

The compose file uses `ghcr.io/zach-sb/nc-link-watcher:latest`. Released images are published for
amd64 and arm64, tagged `1.0.0`, `1.0`, `1` and `latest`. To build it yourself:

```sh
docker buildx build -t nc-link-watcher:1.0.0 --load .
```

## Labels

| Label | Meaning |
| --- | --- |
| `nextcloud-links.url` | The link address. Required. |
| `nextcloud-links.name` | Link text. Defaults to the container name. |
| `nextcloud-links.icon` | An icon name; see [Icons](#icons). |
| `nextcloud-links.groups` | Comma-separated Nextcloud group ids. `*` = everyone. Absent = `DEFAULT_GROUPS`. |
| `nextcloud-links.embed` | `true` opens the site inside Nextcloud. Default `false`: opens it directly. |
| `nextcloud-links.enable` | `false` ignores this container, so its links are removed. Default `true`. |

```yaml
labels:
  nextcloud-links.url: https://jellyfin.example.com
  nextcloud-links.name: Jellyfin
  nextcloud-links.icon: di-jellyfin
  nextcloud-links.groups: family,admins
```

A label value that makes no sense is logged as an error, and that link is left exactly as it is:
not created, changed or removed. That covers a `url` missing while other labels are set,
`embed` or `enable` not being `true`/`false`, `*` mixed with other groups, and a group that doesn't
exist in Nextcloud (never widened to everyone). Labels outside `nextcloud-links.*` are ignored.

### Extra links

Any container can declare more links, for a router, a NAS or a second page of an app, as
`nextcloud-links.<id>.*` with the same fields (except `enable`):

```yaml
labels:
  nextcloud-links.router.url: https://router.lan
  nextcloud-links.router.name: Router
  nextcloud-links.router.icon: mdi-router-wireless
  nextcloud-links.router.groups: admins
```

For things with no container of their own, put them on the watcher's container.

## Icons

An icon name is either a set icon, by prefix, or one of your own:

| Name | Source |
| --- | --- |
| `di-jellyfin` | [Dashboard Icons](https://github.com/homarr-labs/dashboard-icons) |
| `mdi-home` | [Material Design Icons](https://pictogrammers.com/library/mdi/) |
| `si-github` | [Simple Icons](https://simpleicons.org) |
| `sh-jellyfin` | [selfh.st icons](https://github.com/selfhst/icons) |
| `myapp` | `icons/myapp.svg` or `icons/myapp.png` next to `docker-compose.yml` |

Anything else, such as a URL or a name with an extension, is logged as `unrecognised icon`, and the
link is sent without an icon; what that looks like is up to External Sites.

Set icons are downloaded as SVGs from jsDelivr and uploaded to Nextcloud once. If a download fails,
the error is logged, the link goes without an icon, and the download is retried after 6 hours.

Your own icons:

- SVGs can be any size. PNGs must be exactly 16, 24 or 32 pixels square, or Nextcloud rejects them.
- Each is uploaded under the link's key: `<container>.svg`, `<container>-<id>.svg` for extra links,
  `<host>-<container>.svg` for a named host. A container named like a set icon (say `mdi-home`)
  would overwrite that icon; rename the container to fix it.
- Replace the file and the next sync uploads it over the old one. Nextcloud tells browsers to cache
  icons for a day, so a changed icon can take that long to show.

## Docker

Mount `/var/run/docker.sock` (as the compose file does), or set `DOCKER_HOST` to a socket proxy that
allows reading containers and events. The watcher never changes anything in Docker.

### Several hosts

List them in `DOCKER_HOSTS`, comma-separated. One entry may be unnamed; the rest are `name=url`:

```yaml
DOCKER_HOSTS: "unix:///var/run/docker.sock,nas=tcp://nas-proxy:2375"
```

Links from the unnamed host are keyed by container name (`jellyfin`), and those from a named host by
`<name>:<container>` (`nas:jellyfin`), so adding a host later leaves existing links and their ids as
they are. Host names are lower-case letters, digits, `-` and `_`.

If a host can't be reached, `Docker <name>: unreachable` is logged and its links are left exactly as
they are while the other hosts keep syncing.

### User and group

The watcher takes no commands; it only reads Docker and writes to Nextcloud and `data/`. It runs as
root by default and works as any user:

```yaml
user: "1000:1000"
group_add: ["969"]   # the host's docker group id (getent group docker), to read a mounted socket
```

`data/` must be writable by that user. A socket proxy needs no `group_add`.

## Settings

All settings are environment variables. Lists are comma-separated.

| Setting | Default | Meaning |
| --- | --- | --- |
| `NEXTCLOUD_URL` | required | `https://cloud.example.com` |
| `NEXTCLOUD_USER` | required | Login name (user id) of an admin account. |
| `NEXTCLOUD_APP_PASSWORD` | required | An app password for that account. |
| `NEXTCLOUD_VERIFY_TLS` | `true` | `false` turns off certificate checks. For a certificate from your own CA, mount the CA file and set `REQUESTS_CA_BUNDLE` to its path instead. |
| `DEFAULT_GROUPS` | empty | Groups for links with no `groups` label. Empty = everyone. |
| `REMOVE_GRACE` | `30` | Seconds a container may be gone before its link is removed. `0` = at once. Also applies when the watcher starts, so containers that come up after it keep their link ids. |
| `DOCKER_HOST` | `/var/run/docker.sock` | One Docker host, as `unix://`, `tcp://` or `http(s)://`. |
| `DOCKER_HOSTS` | empty | Several Docker hosts; see [Several hosts](#several-hosts). Replaces `DOCKER_HOST`. |
| `SYNC_INTERVAL` | `300` | Seconds between full syncs. Container events trigger one within seconds. |
| `ICONS_DIR` | `/icons` | Folder of your own icons. |
| `STATE_FILE` | `/data/state.json` | Where the ids of the watcher's links are kept. |
| `HEALTH_PORT` | `8080` | Loopback port for the container's healthcheck. Not published. |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every Nextcloud request; see [Troubleshooting](#troubleshooting). |

## Log

Every line is `<subject>: <what>`. The subject is a link key (`<container>`, `<container>/<id>`,
`<host>:<container>`) or a source (`Docker`, `Docker <host>`, `Nextcloud`, `State file`).

```
app: created
app: icon uploaded: di-jellyfin.svg
app: embed: not true or false
app: unknown group: family
app: create failed: HTTP 400: The given url is invalid (url)
nas:app: renamed from nas:old-app
Docker nas: unreachable: Connection refused
Docker nas: resolved
```

Problems are logged as `ERROR` once when they appear and once as `resolved` when they clear, not on
every sync. A restart logs the current problems again.

## Health and stopping

On start the watcher checks that the Nextcloud API answers, the login works, the account is an
admin and External sites is enabled. Until that passes nothing is synced; the reason is logged as
`Nextcloud: <reason>` and the check is repeated every sync.

The image has a Docker healthcheck. The container shows `unhealthy` when no sync has completed for
three `SYNC_INTERVAL`s. A sync is skipped, and nothing is deleted, while Nextcloud, the state file,
or every Docker host can't be read.

Errors that only a config change and a restart can fix stop the watcher, logged as `CRITICAL`.
Whether it comes back is up to the container's restart policy.

| Log | Cause |
| --- | --- |
| `Missing required environment variable X` | |
| `X: not a whole number`, `X: out of range`, `X: not true or false` | A setting has a bad value. |
| `NEXTCLOUD_URL: not an http(s) URL` | |
| `NEXTCLOUD_URL: not a Nextcloud API` | The URL answers, but not as Nextcloud (checked at start). |
| `Nextcloud: login failed as <user>: …` | Wrong user or app password, at start or later. |
| `State file: not writable: …` | At start or later. Carrying on would create duplicate links. |
| `Docker: DOCKER_HOST not set and /var/run/docker.sock not mounted` | |
| `DOCKER_HOST: …`, `DOCKER_HOSTS: …` | A bad URL, a bad host name, or a host listed twice. |
| `DEFAULT_GROUPS: * not allowed; empty means everyone` | |
| `ICONS_DIR: not a directory` | |
| `HEALTH_PORT: …` | The port can't be opened. |
| `LOG_LEVEL: not a log level` | |

Everything else can be fixed without restarting the watcher, so it is logged and retried: Nextcloud
or Docker unreachable, a certificate error, the account not being an admin, External sites being
disabled, a missing group, or an unreadable `state.json`.

## Troubleshooting

`LOG_LEVEL: DEBUG` logs every Nextcloud request with its response, the full text of connection
errors, and the user and app password length the watcher started with. The password itself is
never logged.

`Nextcloud: login failed as <user>: …`

- `NEXTCLOUD_USER` must be the account's login name (its user id), not its email or display name.
- The app password must belong to that account and not have been revoked.
- In `docker-compose.yml`, a `$` in a value must be written as `$$`.
- A reverse proxy in front of Nextcloud must pass the `Authorization` header through. Test from the
  Docker host:
  `curl -u <user>:<app password> -H 'OCS-APIRequest: true' <NEXTCLOUD_URL>/ocs/v2.php/cloud/user`
- Each failed login counts towards Nextcloud's brute-force protection, which slows further attempts
  from that IP.

## Behaviour notes

- Every create, update and delete is read back from Nextcloud and repeated if it didn't stick.
  External Sites keeps all links in one setting with no locking, so anything else writing to it at
  the same moment could otherwise undo the change.
- A renamed container keeps its link while the watcher runs. A rename while it's stopped looks like
  a new container, so the link is recreated.
- A link deleted by hand in Nextcloud is recreated while its container runs, and forgotten once its
  container is gone.
- Links are tracked by id only. If `state.json` is lost, the watcher creates fresh links and the old
  ones must be deleted by hand. If it exists but can't be read, syncing stops until it is fixed or
  deleted, rather than duplicating every link.
- Groups only hide the menu entry; they don't protect the service itself.

## Development

```sh
pip install -r requirements.txt
python -m unittest
```

CI runs the tests and builds the image on every push. Pushing a tag `v<VERSION>` (it must match
`VERSION` in `watcher.py`) also publishes the image to GHCR.

## License

MIT. See [LICENSE](LICENSE).
