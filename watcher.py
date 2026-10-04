#!/usr/bin/env python3
"""Sync labelled Docker containers into Nextcloud's External Sites app.

Each running container that yields a URL gets a link in Nextcloud. One watcher can
read several Docker hosts. It only ever updates or deletes the link ids recorded in
its own state file, so hand-made links are never touched, and it keeps those ids
wherever it can, since each user's own menu order is tied to them.
"""

import hashlib
import http.client
import json
import logging
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

log = logging.getLogger("watcher")

VERSION = "1.0.0"
USER_AGENT = f"nc-link-watcher/{VERSION}"

NO_ICON = ""                        # External Sites decides what to show
NC_DEFAULT_ICON = "external.svg"    # what External Sites may store in place of no icon
EVERYONE = "*"                      # groups label value that overrides DEFAULT_GROUPS
ICON_RETRY_SECONDS = 6 * 3600       # a missing icon is unlikely to appear sooner
LABELS = "nextcloud-links"           # every label the watcher reads starts with this
HEALTH_PORT_DEFAULT = "8080"
DOCKER_SOCKET = Path("/var/run/docker.sock")   # used when DOCKER_HOST is not set
SVG, PNG = "image/svg+xml", "image/png"


def is_true(value, default=False):
    value = (value or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def describe(exc):
    """A short reason for an exception, without the stack of wrapped errors around it.
    Wrapped errors are followed too: requests wraps connection errors in its own."""
    if isinstance(exc, (NextcloudError, DockerError)):
        return str(exc)
    cause = exc
    while cause is not None:
        if isinstance(cause, requests.exceptions.SSLError):
            return "TLS verification failed"
        if isinstance(cause, requests.exceptions.Timeout):
            return "timed out"
        if isinstance(cause, requests.exceptions.ConnectionError):
            reason = os_reason(cause)
            return f"unreachable: {reason}" if reason else "unreachable"
        cause = cause.__cause__ or cause.__context__
    log.debug("%r", exc)   # the full error; the log line is short
    return f"{type(exc).__name__}: {exc}"


def os_reason(exc):
    """The innermost OS error's text, e.g. "Permission denied" for a socket the user can't open."""
    reason, cause = None, exc
    while cause is not None:
        if isinstance(cause, OSError) and cause.strerror:
            reason = cause.strerror
        cause = cause.__cause__ or cause.__context__
    return reason
    log.debug("%r", exc)   # the full error; the log line is short
    return f"{type(exc).__name__}: {exc}"


def split_list(value):
    # Sorted and de-duplicated so comparing against what Nextcloud holds is order-independent.
    return sorted({item.strip() for item in (value or "").split(",") if item.strip()})


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class Config:
    """Settings from the environment. Any bad value stops the app: only a config
    change and a restart can fix it."""

    def __init__(self, environ=os.environ):
        def text(name, default=None):
            value = environ.get(name, "").strip() or default
            if value is None:
                sys.exit(f"Missing required environment variable {name}")
            return value

        def number(name, default, low, high=None):
            try:
                value = int(text(name, str(default)))
            except ValueError:
                sys.exit(f"{name}: not a whole number")
            if value < low or (high is not None and value > high):
                sys.exit(f"{name}: out of range")
            return value

        def flag(name, default):
            value = environ.get(name, "").strip()
            if value and is_true(value, None) is None:
                sys.exit(f"{name}: not true or false")
            return is_true(value, default)

        self.nc_url = text("NEXTCLOUD_URL").rstrip("/")
        if not re.match(r"^https?://[^/]+", self.nc_url):
            sys.exit("NEXTCLOUD_URL: not an http(s) URL")
        self.nc_user = text("NEXTCLOUD_USER")
        self.nc_password = text("NEXTCLOUD_APP_PASSWORD")
        self.verify_tls = flag("NEXTCLOUD_VERIFY_TLS", True)

        self.docker_hosts = docker_hosts(environ)

        self.default_groups = split_list(environ.get("DEFAULT_GROUPS"))
        if EVERYONE in self.default_groups:
            sys.exit(f"DEFAULT_GROUPS: {EVERYONE} not allowed; empty means everyone")
        self.remove_grace = number("REMOVE_GRACE", 30, 0)
        self.sync_interval = number("SYNC_INTERVAL", 300, 1)

        self.icons_dir = Path(text("ICONS_DIR", "/icons"))
        if self.icons_dir.exists() and not self.icons_dir.is_dir():
            sys.exit("ICONS_DIR: not a directory")
        self.state_file = Path(text("STATE_FILE", "/data/state.json"))
        self.health_port = number("HEALTH_PORT", HEALTH_PORT_DEFAULT, 1, 65535)


DOCKER_URL = re.compile(r"^(unix|tcp|http|https)://.+")
HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def docker_hosts(environ):
    """DOCKER_HOSTS -> {host name: Docker URL}. An entry without "name=" is the
    unnamed host, whose link keys are bare container names; a named host's keys are
    "<name>:<container>". Without DOCKER_HOSTS there is one unnamed host, from
    DOCKER_HOST or the mounted socket (URL None)."""
    value = environ.get("DOCKER_HOSTS", "").strip()
    if not value:
        url = environ.get("DOCKER_HOST", "").strip()
        if url and not DOCKER_URL.match(url):
            sys.exit("DOCKER_HOST: not a unix://, tcp:// or http(s):// URL")
        return {"": url or None}
    hosts = {}
    for entry in (item.strip() for item in value.split(",") if item.strip()):
        name, url = entry.split("=", 1) if "=" in entry else ("", entry)
        name, url = name.strip(), url.strip()
        if name and not HOST_NAME.match(name):
            sys.exit(f"DOCKER_HOSTS: bad host name: {name}")
        if not DOCKER_URL.match(url):
            sys.exit(f"DOCKER_HOSTS: not a unix://, tcp:// or http(s):// URL: {url}")
        if name in hosts:
            sys.exit(f"DOCKER_HOSTS: {name or 'unnamed host'} listed twice")
        hosts[name] = url
    return hosts


def host_of(key):
    """The host name a link key belongs to ("" for the unnamed host)."""
    return key.split(":", 1)[0] if ":" in key else ""


# --------------------------------------------------------------------------
# Docker client: the two Engine API calls the watcher needs, over plain HTTP.
# --------------------------------------------------------------------------

class DockerError(Exception):
    pass


@dataclass
class Container:
    id: str
    name: str
    labels: dict


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Docker:
    TIMEOUT = 30

    def __init__(self, url):
        scheme, rest = url.split("://", 1)
        self.scheme, self.address = scheme, rest.rstrip("/")

    def _connection(self):
        if self.scheme == "unix":
            return UnixHTTPConnection(self.address, self.TIMEOUT)
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.address, timeout=self.TIMEOUT,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self.address, timeout=self.TIMEOUT)   # tcp:// or http://

    def _open(self, path, stream=False):
        """GET path -> (connection, response); the caller closes the connection.
        A stream has no read timeout once the headers are in."""
        conn = self._connection()
        try:
            conn.connect()
            sock = conn.sock   # http.client may let go of it once the response is read
            conn.request("GET", path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            if stream:
                sock.settimeout(None)
        except OSError as exc:
            conn.close()
            raise DockerError(f"unreachable: {exc.strerror or exc}") from exc
        if resp.status != 200:
            body = resp.read()
            conn.close()
            try:
                message = json.loads(body).get("message")
            except (ValueError, AttributeError):
                message = None
            raise DockerError(f"HTTP {resp.status}: {message or resp.reason}")
        return conn, resp

    def containers(self):
        """The running containers."""
        conn, resp = self._open("/containers/json")
        try:
            data = json.loads(resp.read())
        except OSError as exc:
            raise DockerError(f"unreachable: {exc.strerror or exc}") from exc
        except ValueError as exc:
            raise DockerError("not a Docker API") from exc
        finally:
            conn.close()
        result = []
        for item in data:
            # Names also lists legacy link aliases ("/other/alias"); the real name has no inner slash.
            names = [n.lstrip("/") for n in item.get("Names") or []]
            name = next((n for n in names if "/" not in n), names[0] if names else item["Id"][:12])
            result.append(Container(item["Id"], name, item.get("Labels") or {}))
        return result

    def events(self):
        """Container events, as dicts, until the connection drops."""
        query = urllib.parse.urlencode({"filters": json.dumps({"type": ["container"]})})
        conn, resp = self._open(f"/events?{query}", stream=True)   # quiet for hours is normal
        try:
            for line in resp:
                if line.strip():
                    yield json.loads(line)
        except OSError as exc:
            raise DockerError(f"unreachable: {exc.strerror or exc}") from exc
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Nextcloud client
# --------------------------------------------------------------------------

class NextcloudError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def error_detail(body):
    """The reason in an OCS error body, with the offending field if External Sites names one."""
    ocs = body.get("ocs") if isinstance(body, dict) else None
    if not isinstance(ocs, dict):
        return None
    data = ocs.get("data") if isinstance(ocs.get("data"), dict) else {}
    meta = ocs.get("meta") if isinstance(ocs.get("meta"), dict) else {}
    message = data.get("error") or data.get("message") or meta.get("message")
    if message and data.get("field"):
        message = f"{message} ({data['field']})"
    return message


class Nextcloud:
    SITES = "/ocs/v2.php/apps/external/api/v1/sites"

    def __init__(self, cfg):
        self.base, self.user = cfg.nc_url, cfg.nc_user
        self.http = requests.Session()
        self.http.auth = (cfg.nc_user, cfg.nc_password)
        self.http.verify = cfg.verify_tls
        # OCS-APIRequest is required by the OCS endpoints, and it is also what lets the
        # plain (non-OCS) icon upload route pass Nextcloud's CSRF check without a session.
        self.http.headers.update({"OCS-APIRequest": "true", "Accept": "application/json",
                                  "User-Agent": USER_AGENT})

    def _request(self, method, path, **kwargs):
        try:
            resp = self.http.request(method, self.base + path, timeout=30, **kwargs)
        except requests.RequestException as exc:
            log.debug("%s %s -> %r", method, path, exc)   # the full error; the log line is short
            raise
        try:
            body = resp.json()
        except ValueError:
            body = None
        log.debug("%s %s -> HTTP %s %s: %s", method, path, resp.status_code,
                  resp.headers.get("Content-Type", ""), resp.text[:500])
        if resp.status_code == 401:
            # Only a config change and a restart can fix this, so stop. SystemExit gets
            # past every `except Exception`; restarting is up to Docker's restart policy.
            raise SystemExit(f"Nextcloud: login failed as {self.user}: {error_detail(body) or resp.reason}")
        if not resp.ok:
            raise NextcloudError(f"HTTP {resp.status_code}: {error_detail(body) or resp.reason}",
                                 resp.status_code)
        return body

    def _ocs(self, method, path, **kwargs):
        body = self._request(method, path, **kwargs)
        try:
            return body["ocs"]["data"]
        except (KeyError, TypeError):
            # A 200 that isn't OCS is usually a login or proxy page: wrong URL, not a server fault.
            raise NextcloudError(f"{method} {path} did not return an API response; check NEXTCLOUD_URL")

    def check(self):
        """Boot check: the API answers, the login works, and it may manage External Sites.

        A URL that answers with something other than Nextcloud's API is a config
        error, so it stops the app. The rest can be fixed in Nextcloud, so they don't.
        """
        try:
            self._ocs("GET", "/ocs/v2.php/cloud/user")
        except NextcloudError as exc:
            if exc.status in (None, 404):   # not an API response, or no such API here
                raise SystemExit("NEXTCLOUD_URL: not a Nextcloud API") from exc
            raise
        try:
            self.admin()
        except NextcloudError as exc:
            reason = {403: "not an admin", 404: "External sites app not enabled"}.get(exc.status)
            if reason:
                raise NextcloudError(reason, exc.status) from exc
            raise

    def admin(self):
        """Every site with all its fields, plus the names of the icons already uploaded."""
        data = self._ocs("GET", self.SITES)
        return ({int(s["id"]): s for s in data["sites"]}, {i["icon"] for i in data["icons"]})

    def groups(self):
        return set(self._ocs("GET", "/ocs/v2.php/cloud/groups")["groups"])

    def add(self, site):
        return int(self._ocs("POST", self.SITES, json=site)["id"])

    def update(self, site_id, site):
        self._ocs("PUT", f"{self.SITES}/{site_id}", json=site)

    def delete(self, site_id):
        self._ocs("DELETE", f"{self.SITES}/{site_id}")

    def upload_icon(self, filename, content, mime):
        self._request("POST", "/index.php/apps/external/icons",
                      files={"uploadicon": (filename, content, mime)})


# --------------------------------------------------------------------------
# Icons are referenced by name: a bare name is a file in the icons folder, a
# prefixed name comes from an icon set.
# --------------------------------------------------------------------------

# SVG only, because Nextcloud accepts SVGs at any size but PNGs only at 16/24/32 px.
ICON_SETS = {
    "di-": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/{}.svg",
    "mdi-": "https://cdn.jsdelivr.net/npm/@mdi/svg@latest/svg/{}.svg",
    "si-": "https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/{}.svg",
    "sh-": "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/{}.svg",
}
LOCAL_TYPES = {".svg": SVG, ".png": PNG}


def icon_name(label):
    """The icon name in a label, lower-cased, or None if it isn't one.

    The strict pattern is what keeps a label from escaping the icons folder or
    reshaping the download URL. No dots, so no extensions and no paths.
    """
    name = (label or "").strip().lower()
    return name if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) else None


def named_icon_url(name):
    """Download URL for an icon set name, or None for a bare (local) name."""
    for prefix, template in ICON_SETS.items():
        if name.startswith(prefix) and len(name) > len(prefix):
            return template.format(name[len(prefix):])
    return None


def fetch_icon(url):
    resp = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
    if resp.status_code == 404:
        raise ValueError("not found")
    resp.raise_for_status()
    if b"<svg" not in resp.content[:2000].lower():
        raise ValueError("not an SVG")
    return resp.content


def local_icon(icons_dir, name):
    """<name>.svg or <name>.png from the icons folder -> (extension, content, mime), or None."""
    if not icons_dir.is_dir():
        return None
    files = {f.name.lower(): f for f in icons_dir.iterdir() if f.is_file()}
    for ext, mime in LOCAL_TYPES.items():
        if name + ext in files:
            return ext, files[name + ext].read_bytes(), mime
    return None


def local_icon_filename(key, ext):
    """Uploaded name of a link's local icon: "<container>.svg", "<container>-<id>.svg",
    "<host>-<container>.svg".

    Named after the container, so watchers on different hosts only clash when their
    container names do. The name stays the same when the file is replaced, so the
    new upload overwrites the old one in Nextcloud rather than piling up.
    """
    return f"{key.replace(':', '-').replace('/', '-')}{ext}"


# --------------------------------------------------------------------------
# Docker: turn container labels into the links we want to exist
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Link:
    name: str
    url: str
    icon: str        # icon name from the label, not yet a file in Nextcloud
    groups: tuple
    embed: bool

    def site(self, icon_file):
        """The record Nextcloud stores. PUT replaces the whole record, so every field
        is always sent, including the ones this tool never varies."""
        return {
            "name": self.name, "url": self.url, "icon": icon_file,
            "groups": list(self.groups), "redirect": 0 if self.embed else 1,
            "lang": "", "type": "link", "device": "",
        }


FIELDS = ("url", "name", "icon", "groups", "embed")


def read_fields(labels, base):
    return {field: labels.get(f"{base}.{field}", "").strip() for field in FIELDS}


class InvalidLabel(ValueError):
    pass


def make_link(cfg, fields, default_name):
    """The Link the labels describe. Raises InvalidLabel for a value that makes no sense."""
    if not fields["url"]:
        raise InvalidLabel("missing url")
    if not fields["groups"]:
        groups = cfg.default_groups
    elif fields["groups"] == EVERYONE:
        groups = []
    else:
        groups = split_list(fields["groups"])
        if EVERYONE in groups:
            raise InvalidLabel(f"groups: {EVERYONE} with other groups")
    embed = is_true(fields["embed"], None) if fields["embed"] else False
    if embed is None:
        raise InvalidLabel("embed: not true or false")
    return Link(fields["name"] or default_name, fields["url"], fields["icon"], tuple(groups), embed)


def read_containers(docker_client, cfg, host=""):
    """One host's running containers -> ({link key: Link}, {link key: invalid label
    problem}, {link key: identity}).

    Keys are container *names*, not ids, because an id changes every time a
    container is recreated and the link should survive that. A container's own
    link is keyed "<name>"; extra links declared as "nextcloud-links.<id>.*" are
    keyed "<name>/<id>". A named host's keys are prefixed "<host>:".

    The identity (host, container id, link id) is what lets a rename keep its link.
    """
    # A link's own fields are one level deep (nextcloud-links.url) and an extra
    # link's two (nextcloud-links.router.url), so an id can't be mistaken for a field.
    p = LABELS
    extra_link = re.compile(rf"^{re.escape(p)}\.([A-Za-z0-9_-]+)\.(?:{'|'.join(FIELDS)})$")
    wanted, invalid, identity = {}, {}, {}
    for container in docker_client.containers():   # running only, so a stopped service has no link
        labels = container.labels or {}
        enable = labels.get(f"{p}.enable", "").strip()
        if enable and is_true(enable, None) is None:
            enable_problem = "enable: not true or false"
        elif not is_true(enable, True):
            continue
        else:
            enable_problem = None

        base = f"{host}:{container.name}" if host else container.name
        candidates = {base: (read_fields(labels, p), container.name, "")}
        for link_id in {m.group(1) for m in map(extra_link.match, labels) if m}:
            candidates[f"{base}/{link_id}"] = (read_fields(labels, f"{p}.{link_id}"), link_id, link_id)

        for key, (fields, default_name, link_id) in candidates.items():
            if not any(fields.values()) and not (enable_problem and key == base):
                continue   # no labels for this link at all
            try:
                if enable_problem:
                    raise InvalidLabel(enable_problem)
                wanted[key] = make_link(cfg, fields, default_name)
                identity[key] = (host, container.id, link_id)
            except InvalidLabel as exc:
                invalid[key] = str(exc)
    return wanted, invalid, identity


def watch_events(docker_factory, wake, subject="Docker"):
    """Wake the main loop on container changes so links follow within seconds.

    Losing the stream is not fatal: the periodic sync still runs, so we just reconnect.
    """
    interesting = {"start", "die", "destroy", "rename"}
    lost = False
    while True:
        try:
            events = docker_factory().events()
            if lost:
                log.info("%s events: resolved", subject)
                lost = False
            for event in events:
                if event.get("Action") in interesting:
                    wake.set()
        except Exception as exc:
            if not lost:   # once per outage, not every 10s
                log.warning("%s events: %s", subject, describe(exc))
                lost = True
        time.sleep(10)


# --------------------------------------------------------------------------
# State file: {link key: Nextcloud site id}. This is the list of links we own.
# --------------------------------------------------------------------------

def load_state(path):
    """A missing file means a first run. An unreadable one raises instead of
    starting empty, because "empty" would recreate every link as a duplicate."""
    if not path.exists():
        return {}
    return {str(key): int(site_id) for key, site_id in json.loads(path.read_text()).items()}


def state_not_writable(exc):
    return SystemExit(f"State file: not writable: {exc.strerror or exc}")


def check_writable(path):
    """Boot check. Without a writable state file no link can be owned, and only a
    fixed mount and a restart can change that."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.with_suffix(".tmp")
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        raise state_not_writable(exc) from exc


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)   # atomic, so a crash mid-write can't leave a half-written file


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def differs(existing, site):
    current = dict(existing,
                   icon=existing.get("icon") or NO_ICON,
                   groups=sorted(existing.get("groups") or []),
                   redirect=int(bool(existing.get("redirect"))))
    if site.get("icon") == NO_ICON and current["icon"] == NC_DEFAULT_ICON:
        current["icon"] = NO_ICON   # its own stand-in for "no icon", not a change to undo
    return any(current.get(field) != value for field, value in site.items())


def holds(sites, site_id, site):
    """True if Nextcloud has this id with these fields (all of them, or just the ones given)."""
    return site_id in sites and not differs(sites[site_id], site)


def write_verified(nc, write, check, key, attempts=3):
    """Make a change, then read Nextcloud back to confirm it stuck.

    Nextcloud keeps all sites in one config value with no locking. Two watchers
    writing at the same instant can be handed the same new id, or one can save a
    copy it read just before the other's change and so silently undo it. So every
    write is checked, and repeated if it was lost.
    """
    for attempt in range(1, attempts + 1):
        result = write()
        time.sleep(0.5 * attempt)   # give a competing writer time to land before we look
        sites, _ = nc.admin()
        if check(sites, result):
            return result
        log.warning("%s: overwritten, retrying", key)
    raise NextcloudError(f"overwritten {attempts} times")


class Watcher:
    def __init__(self, docker_factories, nc, cfg):
        """docker_factories: {host name: function returning a Docker client}."""
        self.docker_factories, self.nc, self.cfg = docker_factories, nc, cfg
        self.clients = {}          # host name -> Docker client, made on first use
        # All deliberately in memory only: after a restart failed icons get one fresh
        # attempt, local icons are uploaded once more in case they changed, and
        # current problems are logged again.
        self.gone_since = {}       # link key -> when its container was first seen missing
        self.icon_failed = {}      # (icon filename, content hash) -> (when to retry, problem)
        self.uploaded = {}         # local icon filename -> content hash uploaded this run
        self.problems = {}         # subject -> problem, as last logged
        self.found = {}            # problems found by the sync in progress
        self.connected = False     # boot check passed
        self.last_keys = {}        # identity -> link key, as of the last sync, to spot renames
        self.last_good_sync = None

    def healthy(self):
        """True while syncs are completing. Three intervals of slack, so a brief
        Docker or Nextcloud outage doesn't flip the container to unhealthy, but a
        hung loop or a lasting failure does."""
        if self.last_good_sync is None:
            return False
        return time.monotonic() - self.last_good_sync < 3 * self.cfg.sync_interval

    def sync(self, now=None):
        """Make Nextcloud match the running containers.

        Returns seconds until the next sync is needed, or None if this one was skipped.
        """
        now = time.monotonic() if now is None else now
        cfg = self.cfg
        self.found = {}

        # Boot check, repeated each sync until it passes. Until then the container
        # stays unhealthy, since a sync never completes.
        if not self.connected:
            try:
                self.nc.check()
            except Exception as exc:
                return self._skip({"Nextcloud": describe(exc)})
            self.connected = True
            if "Nextcloud" not in self.problems:   # a recovery is logged as "resolved" instead
                log.info("Nextcloud: connected")

        # Read everything before changing anything. A failed read must never look
        # like "no containers", or one Docker hiccup would wipe every link. A host
        # that can't be read has its links left exactly as they are.
        wanted, invalid, identity, down = self._read_docker()
        if len(down) == len(self.docker_factories):
            return self._skip(self.found)
        try:
            sites, icons = self.nc.admin()
            known_groups = self.nc.groups() if any(link.groups for link in wanted.values()) else set()
        except Exception as exc:
            return self._skip({"Nextcloud": describe(exc)})
        try:
            state = load_state(cfg.state_file)   # re-read each time so hand edits take effect
        except Exception as exc:
            return self._skip({"State file": f"unreadable: {exc}"})

        # A link with an invalid label is left exactly as it is, like one with an
        # unknown group: not created, changed or removed.
        self.found.update(invalid)
        self._follow_renames(wanted, identity, state)

        # Sorted so a first sync creates links alphabetically; creation order is the
        # only influence anyone has over menu order.
        for key, link in sorted(wanted.items()):
            self.gone_since.pop(key, None)
            try:
                self._upsert(key, link, state, sites, icons, known_groups, now)
            except Exception as exc:   # one bad link must not block the rest
                self.found[key] = describe(exc)

        next_due = cfg.sync_interval
        # Grace applies at startup too: after a host reboot the watcher can start before
        # other containers, and recreating their links would give them new ids, which
        # loses each user's own menu order for them.
        grace = cfg.remove_grace
        self.gone_since = {key: t for key, t in self.gone_since.items()
                           if key in state and host_of(key) not in down and key not in invalid}
        for key in sorted(set(state) - set(wanted)):
            if host_of(key) in down or key in invalid:
                continue
            gone_for = now - self.gone_since.setdefault(key, now)
            if gone_for < grace:
                # Still within the grace period: an image update or restart keeps its
                # link (and its place in the menu). Come back when the period ends.
                next_due = min(next_due, grace - gone_for)
                continue
            site_id = state[key]
            if site_id in sites:
                # Only the name and URL are compared, so the check can't mistake a
                # different link that later takes the same id for ours coming back.
                ours = {field: sites[site_id].get(field) for field in ("name", "url")}
                try:
                    self._apply(key, "remove", "removed", lambda: self.nc.delete(site_id),
                                lambda now_sites, _: not holds(now_sites, site_id, ours))
                except Exception as exc:
                    self.found[key] = describe(exc)
                    continue
            else:
                log.info("%s: link %s already deleted", key, site_id)
            del state[key]
            self._save(state)
        self._report(self.found)
        self.last_good_sync = time.monotonic()
        return max(next_due, 1)

    def _upsert(self, key, link, state, sites, icons, known_groups, now):
        unknown = [group for group in link.groups if group not in known_groups]
        if unknown:
            # Nextcloud would reject the call anyway. Dropping the bad group instead
            # would widen the link to everyone, so leave things exactly as they are.
            self.found[key] = f"unknown group: {', '.join(unknown)}"
            return

        site = link.site(self._icon_file(key, link.icon, icons, now))
        owned_id = state.get(key)
        if owned_id in sites:
            if differs(sites[owned_id], site):
                self._apply(key, "update", "updated", lambda: self.nc.update(owned_id, site),
                            lambda now_sites, _: holds(now_sites, owned_id, site))
            return

        # Either new, or our link was deleted by hand in Nextcloud. Ownership is by id
        # only, so a hand-made link with the same URL is left alone and a second is made.
        state[key] = self._apply(key, "create", "created", lambda: self.nc.add(site),
                                 lambda now_sites, site_id: holds(now_sites, site_id, site))
        self._save(state)   # straight away, so a crash can't orphan the new link

    def _icon_file(self, key, label, icons, now):
        """Name of an icon file that exists in Nextcloud for this label, uploading it if
        needed, or NO_ICON. Only two kinds of label are recognised: a bare name, for a
        file in the icons folder, or a prefixed icon set name. Anything else is an error."""
        if not label:
            return NO_ICON
        name = icon_name(label)
        if name is None:
            self.found[key] = f"unrecognised icon: {label}"
            return NO_ICON
        url = named_icon_url(name)

        if url is None:
            try:
                local = local_icon(self.cfg.icons_dir, name)
            except OSError as exc:
                self.found[key] = f"icon unreadable: {name}: {describe(exc)}"
                return NO_ICON
            if local is None:
                self.found[key] = f"icon not found: {name}"
                return NO_ICON
            ext, content, mime = local
            filename = local_icon_filename(key, ext)
            version = hashlib.sha256(content).hexdigest()
            # Same filename after an edit, so the content is what says it must go up again.
            if filename in icons and self.uploaded.get(filename) == version:
                return filename
        else:
            filename, content, mime, version = f"{name}.svg", None, SVG, None
            if filename in icons:
                return filename

        retry_at, problem = self.icon_failed.get((filename, version), (0, None))
        if retry_at > now:
            self.found[key] = problem
            return NO_ICON
        try:
            if url:
                content = fetch_icon(url)
            self._apply(key, "icon upload", f"icon uploaded: {filename}",
                        lambda: self.nc.upload_icon(filename, content, mime))
        except Exception as exc:
            # The link matters more than its icon, so send it without one, and don't
            # hammer the CDN or Nextcloud again on every sync. A local file that
            # changes gets a fresh attempt straight away, since its hash changes too.
            problem = f"icon unavailable: {label}: {describe(exc)}"
            self.icon_failed[(filename, version)] = (now + ICON_RETRY_SECONDS, problem)
            self.found[key] = problem
            return NO_ICON
        icons.add(filename)
        self.uploaded[filename] = version
        return filename

    def _read_docker(self):
        """Read every host. Returns (wanted, invalid, identity, hosts that failed)."""
        wanted, invalid, identity, down = {}, {}, {}, set()
        for host, factory in self.docker_factories.items():
            try:
                if host not in self.clients:
                    self.clients[host] = factory()
                host_wanted, host_invalid, host_identity = read_containers(self.clients[host], self.cfg, host)
            except Exception as exc:
                self.clients.pop(host, None)   # reconnect from scratch next time
                self.found[f"Docker {host}" if host else "Docker"] = describe(exc)
                down.add(host)
                continue
            wanted.update(host_wanted)
            invalid.update(host_invalid)
            identity.update(host_identity)
        return wanted, invalid, identity, down

    def _follow_renames(self, wanted, identity, state):
        """A renamed container keeps its link and id: the new key takes over the old
        key's entry when they belong to the same container and link."""
        for key in sorted(wanted):
            old = self.last_keys.get(identity[key])
            if key not in state and old and old in state and old not in wanted:
                state[key] = state.pop(old)
                self.gone_since.pop(old, None)
                self._save(state)
                log.info("%s: renamed from %s", key, old)
        self.last_keys = {identity[key]: key for key in wanted}

    @staticmethod
    def _is_source(subject):
        return subject in ("Nextcloud", "State file") or subject.split(" ")[0] == "Docker"

    def _skip(self, problems):
        """Report why the sync was skipped. Nothing was checked, so the link problems
        already known still stand. Returns None, the skipped-sync result."""
        found = {key: value for key, value in self.problems.items() if not self._is_source(key)}
        found.update(problems)
        self._report(found)
        return None

    def _report(self, found):
        """Log each problem once as an error when it appears or changes, and once
        when it clears, instead of repeating it on every sync."""
        for subject, problem in sorted(found.items()):
            if self.problems.get(subject) != problem:
                log.error("%s: %s", subject, problem)
        for subject in sorted(set(self.problems) - set(found)):
            log.info("%s: resolved", subject)
        self.problems = found

    def _apply(self, key, action, done, write, check=None):
        """Make one change and log it once made. With a check, the change is read back
        and repeated if lost."""
        try:
            result = write() if check is None else write_verified(self.nc, write, check, key)
        except Exception as exc:
            raise NextcloudError(f"{action} failed: {describe(exc)}") from exc
        log.info("%s: %s", key, done)
        return result

    def _save(self, state):
        try:
            save_state(self.cfg.state_file, state)
        except OSError as exc:
            # Carrying on would recreate links it can't record, as duplicates every sync.
            raise state_not_writable(exc) from exc


# --------------------------------------------------------------------------
# Health: answered over loopback from memory, so the state file stays the only
# thing this program ever writes.
# --------------------------------------------------------------------------

def check_docker_access(hosts, socket_path):
    """Boot check: with no Docker URL given, the socket must be mounted. Neither
    missing is a config error; whether Docker answers is checked every sync."""
    if None in hosts.values() and not socket_path.is_socket():
        sys.exit(f"Docker: DOCKER_HOST not set and {socket_path} not mounted")


def health_server(watcher, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if watcher.healthy() else 503)
            self.end_headers()

        def log_message(self, *args):   # a probe every minute would drown the real log
            pass

    return HTTPServer(("127.0.0.1", port), Handler)


def healthcheck(port):
    """Exit status for Docker's HEALTHCHECK: 0 healthy, 1 not."""
    try:
        # No proxies: an HTTP_PROXY set for reaching Nextcloud must not swallow a loopback call.
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/", timeout=5)
        return 0
    except Exception:
        return 1


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    """Errors that only a config change and a restart can fix raise SystemExit with
    a message, from anywhere. They are logged here and the app stops; whether it
    comes back is up to Docker's restart policy."""
    level = os.environ.get("LOG_LEVEL", "").strip().upper() or "INFO"
    level_ok = level in logging.getLevelNamesMapping()
    logging.basicConfig(level=level if level_ok else "INFO", format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")   # ISO 8601
    try:
        if not level_ok:
            sys.exit("LOG_LEVEL: not a log level")
        run()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            log.critical("%s", exc.code)
            raise SystemExit(1) from None
        raise


def run():
    # As PID 1 in a container Python gets no default SIGTERM behaviour, so without
    # this `docker stop` would hang for its full timeout and then kill the process.
    # Ctrl+C (SIGINT) would otherwise end in a KeyboardInterrupt traceback.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: sys.exit(0))

    cfg = Config()
    check_writable(cfg.state_file)

    check_docker_access(cfg.docker_hosts, DOCKER_SOCKET)
    factories = {host: (lambda url=url or f"unix://{DOCKER_SOCKET}": Docker(url))
                 for host, url in cfg.docker_hosts.items()}
    watcher = Watcher(factories, Nextcloud(cfg), cfg)
    try:
        health = health_server(watcher, cfg.health_port)
    except OSError as exc:
        sys.exit(f"HEALTH_PORT: {exc.strerror or exc}")

    log.info("nc-link-watcher %s: %s; Docker: %s", VERSION, cfg.nc_url,
             ", ".join(f"{host}={url}" if host else (url or str(DOCKER_SOCKET))
                       for host, url in cfg.docker_hosts.items()))
    log.debug("Nextcloud user %s, app password %d characters, TLS verification %s",
              cfg.nc_user, len(cfg.nc_password), "on" if cfg.verify_tls else "off")

    wake = threading.Event()
    for host, make_client in factories.items():
        subject = f"Docker {host}" if host else "Docker"
        threading.Thread(target=watch_events, args=(make_client, wake, subject), daemon=True).start()
    threading.Thread(target=health.serve_forever, daemon=True).start()
    while True:
        due = watcher.sync()
        if wake.wait(timeout=due or cfg.sync_interval):
            time.sleep(2)   # `compose up` fires a burst of events; sync once, after it settles
            wake.clear()


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        # Read the port directly: the probe shouldn't need the Nextcloud settings to be valid.
        sys.exit(healthcheck(os.environ.get("HEALTH_PORT", "").strip() or HEALTH_PORT_DEFAULT))
    main()
