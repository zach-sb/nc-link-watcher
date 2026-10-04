"""Tests for the sync logic using in-memory fakes for Docker and Nextcloud."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import watcher

P = "nextcloud-links"


class FakeContainer:
    def __init__(self, name, labels, cid=None):
        self.name, self.labels, self.id = name, labels, cid or f"id-{name}"


class FakeDocker:
    def __init__(self):
        self.running, self.fail = [], False
        self.containers = self

    def list(self):
        if self.fail:
            raise RuntimeError("socket unreachable")
        return list(self.running)


class FakeNextcloud:
    def __init__(self):
        self.sites, self.icons, self.max_id = {}, {"external.svg"}, 0
        self.group_ids = {"family", "admins"}
        self.steal_next_create = False
        self.check_error = None
        self.undo_next_write = False    # another watcher saves a stale copy over our change
        self.uploads = {}

    def check(self):
        if self.check_error:
            raise self.check_error

    def admin(self):
        return {k: dict(v) for k, v in self.sites.items()}, set(self.icons)

    def groups(self):
        return set(self.group_ids)

    def add(self, site):
        self.max_id += 1
        stored = dict(site, id=self.max_id, redirect=bool(site["redirect"]))
        if self.steal_next_create:      # another watcher won the race for this id
            self.steal_next_create = False
            stored = dict(stored, url="https://someone-else.example.com")
        self.sites[self.max_id] = stored
        return self.max_id

    def update(self, site_id, site):
        if not self._undone():
            self.sites[site_id] = dict(site, id=site_id, redirect=bool(site["redirect"]))

    def delete(self, site_id):
        if not self._undone():
            self.sites.pop(site_id, None)

    def _undone(self):
        undone, self.undo_next_write = self.undo_next_write, False
        return undone

    def upload_icon(self, filename, content, mime="image/svg+xml"):
        self.icons.add(filename)
        self.uploads[filename] = (content, mime)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "NEXTCLOUD_URL": "https://cloud.example.com", "NEXTCLOUD_USER": "a",
            "NEXTCLOUD_APP_PASSWORD": "b", "STATE_FILE": os.path.join(self.tmp.name, "state.json"),
            "ICONS_DIR": os.path.join(self.tmp.name, "icons"),
        }
        self.docker, self.nc, self.now = FakeDocker(), FakeNextcloud(), 1000.0
        self.w = watcher.Watcher({"": lambda: self.docker}, self.nc, None)
        self.configure()
        for target, stub in (("fetch_icon", lambda url: b"<svg/>"), ("time.sleep", lambda s: None)):
            patcher = mock.patch(f"watcher.{target}", stub)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, **extra):
        self.cfg = self.w.cfg = watcher.Config(dict(self.env, **extra))

    def run_sync(self, after=0):
        self.now += after
        return self.w.sync(now=self.now)

    def state(self):
        return watcher.load_state(Path(self.cfg.state_file))

    def start(self, cname, raw=None, cid=None, on=None, **labels):
        docker = on or self.docker
        docker.running = [c for c in docker.running if c.name != cname]
        all_labels = {f"{P}.{k}": v for k, v in labels.items()}
        all_labels.update(raw or {})
        docker.running.append(FakeContainer(cname, all_labels, cid))

    def stop(self, cname, on=None):
        docker = on or self.docker
        docker.running = [c for c in docker.running if c.name != cname]

    def test_create_update_remove(self):
        self.start("jellyfin", url="https://jf.example.com", name="Jellyfin", icon="di-jellyfin", groups="family")
        self.run_sync()
        self.assertEqual(self.state(), {"jellyfin": 1})
        site = self.nc.sites[1]
        self.assertEqual((site["name"], site["icon"], site["groups"], site["redirect"]),
                         ("Jellyfin", "di-jellyfin.svg", ["family"], True))

        self.start("jellyfin", url="https://jf.example.com", name="Films", embed="true")
        self.run_sync()
        self.assertEqual((self.nc.sites[1]["name"], self.nc.sites[1]["redirect"]), ("Films", False))
        self.assertEqual(self.nc.max_id, 1)

        self.stop("jellyfin")
        self.run_sync()
        self.run_sync(after=61)
        self.assertEqual((self.nc.sites, self.state()), ({}, {}))

    def test_grace_keeps_link_through_a_restart(self):
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.stop("app")
        self.assertAlmostEqual(self.run_sync(), 30)
        self.assertAlmostEqual(self.run_sync(after=20), 10)
        self.assertEqual(self.state(), {"app": 1})
        self.start("app", url="https://app.example.com")
        self.run_sync(after=5)
        self.stop("app")                      # timer restarts from zero
        self.run_sync(after=15)
        self.run_sync(after=29)
        self.assertEqual(self.state(), {"app": 1})
        self.run_sync(after=2)
        self.assertEqual(self.nc.sites, {})

    def test_grace_also_applies_when_the_watcher_starts(self):
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.stop("app")
        self.w = watcher.Watcher({"": lambda: self.docker}, self.nc, self.cfg)   # the watcher restarts
        self.run_sync()
        self.assertEqual(self.state(), {"app": 1})
        self.start("app", url="https://app.example.com")         # its container comes up late
        self.run_sync(after=10)
        self.assertEqual(self.state(), {"app": 1})                 # same link, same id

    def test_grace_zero_removes_immediately(self):
        self.configure(REMOVE_GRACE="0")
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.stop("app")
        self.run_sync()
        self.assertEqual(self.nc.sites, {})

    def test_default_groups(self):
        self.configure(DEFAULT_GROUPS="admins")
        self.start("a", url="https://a.example.com")
        self.start("b", url="https://b.example.com", groups="family")
        self.start("c", url="https://c.example.com", groups="*")
        self.run_sync()
        groups = {s["url"][8]: s["groups"] for s in self.nc.sites.values()}
        self.assertEqual(groups, {"a": ["admins"], "b": ["family"], "c": []})

    def test_extra_links_on_a_container(self):
        self.start("watcher", raw={
            f"{P}.router.url": "https://router.lan",
            f"{P}.router.name": "Router",
            f"{P}.nas.url": "https://nas.lan",
            f"{P}.nas.groups": "admins",
        })
        self.run_sync()
        self.assertEqual(sorted(self.state()), ["watcher/nas", "watcher/router"])
        by_url = {s["url"]: s for s in self.nc.sites.values()}
        self.assertEqual(by_url["https://router.lan"]["name"], "Router")
        self.assertEqual((by_url["https://nas.lan"]["name"], by_url["https://nas.lan"]["groups"]),
                         ("nas", ["admins"]))

    def test_labels_without_url_are_reported(self):
        self.start("app", name="App", raw={f"{P}.nas.icon": "mdi-nas", "other.label": "x"})
        self.start("plain", raw={"other.label": "x"})
        with self.assertLogs("watcher", "ERROR") as logs:
            self.run_sync()
        self.assertEqual(logs.output, ["ERROR:watcher:app: missing url", "ERROR:watcher:app/nas: missing url"])
        self.assertEqual(self.nc.sites, {})

    def test_messages_are_short(self):
        self.start("app", url="https://app.example.com", icon="di-jellyfin")
        with self.assertLogs("watcher", "INFO") as logs:
            self.run_sync()
            self.start("app", url="https://app.example.com", name="App")
            self.run_sync()
            self.stop("app")
            self.run_sync(after=0)
            self.run_sync(after=61)
        self.assertEqual(logs.output, [
            "INFO:watcher:Nextcloud: connected", "INFO:watcher:app: icon uploaded: di-jellyfin.svg",
            "INFO:watcher:app: created", "INFO:watcher:app: updated", "INFO:watcher:app: removed"])

    def test_nextcloud_rejection_shows_its_reason(self):
        body = {"ocs": {"meta": {"message": ""}, "data": {"error": "The given url is invalid", "field": "url"}}}
        self.assertEqual(watcher.error_detail(body), "The given url is invalid (url)")
        self.assertIsNone(watcher.error_detail(None))
        self.nc.add = mock.Mock(side_effect=watcher.NextcloudError("HTTP 400: The given url is invalid (url)"))
        self.start("app", url="ftp://app.example.com")
        with self.assertLogs("watcher", "ERROR") as logs:
            self.run_sync()
        self.assertEqual(logs.output, ["ERROR:watcher:app: create failed: HTTP 400: The given url is invalid (url)"])

    def test_invalid_label_values_are_errors_and_leave_the_link_alone(self):
        self.start("app", url="https://app.example.com", groups="family")
        self.run_sync()
        for bad, problem in (({"embed": "yse"}, "embed: not true or false"),
                             ({"enable": "nah"}, "enable: not true or false"),
                             ({"groups": "family,*"}, "groups: * with other groups"),
                             ({"url": ""}, "missing url")):
            labels = dict({"url": "https://changed.example.com", "groups": "family"}, **bad)
            self.start("app", **labels)
            with self.assertLogs("watcher", "ERROR") as logs:
                self.run_sync(after=100)                  # well past the grace period
            self.assertEqual(logs.output, [f"ERROR:watcher:app: {problem}"])
            self.assertEqual((self.state(), self.nc.sites[1]["url"]), ({"app": 1}, "https://app.example.com"))
            self.start("app", url="https://app.example.com", groups="family")
            self.run_sync()

    def test_enable_false_skips_container(self):
        self.start("a", url="https://a.example.com", enable="false")
        self.run_sync()
        self.assertEqual(self.nc.sites, {})

    def test_hand_made_link_with_same_url_is_left_alone(self):
        self.nc.add(watcher.Link("Mine", "https://app.example.com", "", (), False).site("external.svg"))
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.assertEqual(self.state(), {"app": 2})
        self.stop("app")
        self.run_sync()
        self.run_sync(after=61)
        self.assertEqual(list(self.nc.sites), [1])
        self.assertEqual(self.nc.sites[1]["name"], "Mine")

    def test_docker_failure_deletes_nothing(self):
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.docker.fail = True
        self.assertIsNone(self.run_sync(after=500))
        self.assertEqual(len(self.nc.sites), 1)

    def test_missing_group_leaves_link_untouched(self):
        self.start("app", url="https://app.example.com", groups="family")
        self.run_sync()
        self.start("app", url="https://app.example.com", groups="nope")
        self.run_sync()
        self.assertEqual(self.nc.sites[1]["groups"], ["family"])

    def test_missing_group_never_creates_public_link(self):
        self.configure(DEFAULT_GROUPS="nope")
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.assertEqual(self.nc.sites, {})

    def test_link_deleted_by_hand_is_recreated(self):
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.nc.sites.clear()
        self.run_sync()
        self.assertEqual(self.state(), {"app": 2})

    def test_id_collision_is_retried(self):
        self.nc.steal_next_create = True
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.assertEqual(self.state(), {"app": 2})
        self.assertEqual(self.nc.sites[1]["url"], "https://someone-else.example.com")

    def test_undone_update_is_retried(self):
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.nc.undo_next_write = True
        self.start("app", url="https://app.example.com", name="App")
        self.run_sync()
        self.assertEqual(self.nc.sites[1]["name"], "App")

    def test_undone_delete_is_retried(self):
        self.configure(REMOVE_GRACE="0")
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.nc.undo_next_write = True
        self.stop("app")
        self.run_sync()
        self.assertEqual((self.nc.sites, self.state()), ({}, {}))

    def test_delete_retry_spares_a_link_that_took_the_same_id(self):
        self.configure(REMOVE_GRACE="0")
        self.start("app", url="https://app.example.com")
        self.run_sync()
        theirs = watcher.Link("Theirs", "https://theirs.example.com", "", (), False).site("external.svg")
        def delete_then_reuse_id(site_id):       # another watcher creates straight after
            self.nc.sites.pop(site_id)
            self.nc.sites[site_id] = dict(theirs, id=site_id, redirect=True)
        self.nc.delete = delete_then_reuse_id
        self.stop("app")
        self.run_sync()
        self.assertEqual((self.nc.sites[1]["name"], self.state()), ("Theirs", {}))

    def test_link_deleted_by_hand_after_container_stopped_is_forgotten(self):
        self.configure(REMOVE_GRACE="0")
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.nc.sites.clear()
        self.nc.delete = mock.Mock(side_effect=AssertionError("must not be called"))
        self.stop("app")
        self.run_sync()
        self.assertEqual(self.state(), {})

    def test_problems_are_logged_once_and_when_resolved(self):
        self.start("app", url="https://app.example.com", groups="nope")
        with self.assertLogs("watcher", "INFO") as logs:
            self.run_sync()
            self.run_sync(after=300)
            self.nc.group_ids.add("nope")
            self.run_sync(after=300)
            self.run_sync(after=300)
        messages = [line for line in logs.output if "app:" in line and "created" not in line]
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0].startswith("ERROR") and messages[0].endswith("app: unknown group: nope"))
        self.assertEqual(messages[1], "INFO:watcher:app: resolved")

    def test_skipped_sync_is_logged_once_and_keeps_link_problems(self):
        self.start("app", url="https://app.example.com", groups="nope")
        self.run_sync()
        with self.assertLogs("watcher", "INFO") as logs:
            self.docker.fail = True
            self.run_sync()
            self.run_sync()
            self.docker.fail = False
            self.run_sync()
        self.assertEqual(logs.output, ["ERROR:watcher:Docker: RuntimeError: socket unreachable",
                                       "INFO:watcher:Docker: resolved"])

    def test_bare_icon_names_are_local_files_uploaded_as_the_container(self):
        self.start("app", url="https://app.example.com", icon="Jellyfin")
        self.start("mine", url="https://mine.example.com", icon="myapp")
        with self.assertLogs("watcher", "ERROR") as logs:
            self.run_sync()                               # no icons folder at all yet
        self.assertEqual(logs.output, ["ERROR:watcher:app: icon not found: jellyfin",
                                       "ERROR:watcher:mine: icon not found: myapp"])
        self.assertEqual((self.nc.sites[1]["icon"], self.nc.uploads), ("", {}))

        icons = Path(self.cfg.icons_dir)
        icons.mkdir()
        (icons / "jellyfin.svg").write_bytes(b"<svg>one</svg>")
        (icons / "MyApp.png").write_bytes(b"png-bytes")
        self.run_sync()
        self.assertEqual(self.nc.sites[1]["icon"], "app.svg")
        self.assertEqual(self.nc.uploads["app.svg"], (b"<svg>one</svg>", "image/svg+xml"))
        self.assertEqual(self.nc.sites[2]["icon"], "mine.png")
        self.assertEqual(self.nc.uploads["mine.png"][1], "image/png")

        self.nc.uploads.clear()
        self.run_sync()                                           # unchanged -> not uploaded again
        self.assertEqual(self.nc.uploads, {})
        (icons / "jellyfin.svg").write_bytes(b"<svg>two</svg>")   # replaced -> uploaded over the old one
        self.run_sync()
        self.assertEqual(self.nc.uploads, {"app.svg": (b"<svg>two</svg>", "image/svg+xml")})
        self.assertEqual(self.nc.sites[1]["icon"], "app.svg")

        (icons / "jellyfin.svg").unlink()                         # removed -> an error, no icon
        self.run_sync()
        self.assertEqual(self.nc.sites[1]["icon"], "")

    def test_health_follows_sync_results(self):
        server = watcher.health_server(self.w, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        self.assertEqual(watcher.healthcheck(port), 1)     # nothing has synced yet
        self.run_sync()
        self.assertEqual(watcher.healthcheck(port), 0)

        self.docker.fail = True                            # failures alone don't flip it straight away
        self.run_sync()
        self.assertEqual(watcher.healthcheck(port), 0)
        self.w.last_good_sync -= 3 * self.cfg.sync_interval
        self.assertEqual(watcher.healthcheck(port), 1)

    def test_icon_names_only(self):
        self.assertEqual(watcher.icon_name(" Jellyfin "), "jellyfin")
        self.assertTrue(watcher.named_icon_url("mdi-home").endswith("@mdi/svg@latest/svg/home.svg"))
        self.assertTrue(watcher.named_icon_url("di-jellyfin").endswith("dashboard-icons/svg/jellyfin.svg"))
        self.assertTrue(watcher.named_icon_url("sh-jellyfin").endswith("selfhst/icons/svg/jellyfin.svg"))
        self.assertIsNone(watcher.named_icon_url("jellyfin"))
        self.assertIsNone(watcher.named_icon_url("mdi-"))
        for not_a_name in ("https://x.example.com/a.svg", "/icons/a.png", "../secret", "",
                           "jellyfin.png", "mdi-home-#ff0000"):
            self.assertIsNone(watcher.icon_name(not_a_name))

    def test_unrecognised_icon_is_an_error_and_sent_as_no_icon(self):
        self.start("app", url="https://app.example.com", icon="https://x.example.com/app.png")
        with self.assertLogs("watcher", "ERROR") as logs:
            self.run_sync()
        self.assertEqual(self.nc.sites[1]["icon"], "")
        self.assertEqual(self.nc.uploads, {})
        self.assertEqual(logs.output, ["ERROR:watcher:app: unrecognised icon: https://x.example.com/app.png"])

    def test_no_icon_is_stable_whatever_nextcloud_stores_for_it(self):
        self.start("app", url="https://app.example.com")
        self.run_sync()
        self.assertEqual(self.nc.sites[1]["icon"], "")
        self.nc.sites[1]["icon"] = "external.svg"      # External Sites' own stand-in
        self.nc.update = mock.Mock(side_effect=AssertionError("must not be called"))
        self.run_sync()

    def test_boot_check_blocks_sync_until_nextcloud_answers(self):
        self.start("app", url="https://app.example.com")
        self.nc.check_error = watcher.NextcloudError("not an admin", 403)
        with self.assertLogs("watcher", "INFO") as logs:
            self.assertIsNone(self.run_sync())
            self.assertIsNone(self.run_sync())
            self.assertFalse(self.w.healthy())
            self.nc.check_error = None
            self.run_sync()
        self.assertEqual(logs.output[0], "ERROR:watcher:Nextcloud: not an admin")
        self.assertIn("INFO:watcher:Nextcloud: resolved", logs.output)
        self.assertEqual((self.state(), self.w.healthy()), ({"app": 1}, True))

    def test_login_failure_stops_the_app_even_mid_sync(self):
        self.start("app", url="https://app.example.com", icon="di-jellyfin")
        self.nc.upload_icon = mock.Mock(side_effect=SystemExit("Nextcloud: login failed"))
        with self.assertRaises(SystemExit):
            self.run_sync()

    def test_boot_check_reasons(self):
        class Resp:
            def __init__(self, status, body):
                self.status_code, self.ok, self.body, self.reason = status, status < 400, body, "Reason"
                self.headers, self.text = {}, ""
            def json(self):
                if self.body is None:
                    raise ValueError
                return self.body
        ok = Resp(200, {"ocs": {"data": {"id": "a"}}})
        nc = watcher.Nextcloud(self.cfg)
        unauthorised = Resp(401, {"ocs": {"meta": {"message": "Current user is not logged in"}, "data": []}})
        for response, reason in ((unauthorised, "Nextcloud: login failed as a: Current user is not logged in"),
                                 (Resp(401, None), "Nextcloud: login failed as a: Reason"),
                                 (Resp(200, None), "NEXTCLOUD_URL: not a Nextcloud API"),
                                 (Resp(404, None), "NEXTCLOUD_URL: not a Nextcloud API")):
            with mock.patch.object(nc.http, "request", return_value=response):
                with self.assertRaisesRegex(SystemExit, reason):
                    nc.check()
        for responses, reason in (
            ([ok, Resp(403, {"ocs": {}})], "not an admin"),
            ([ok, Resp(404, None)], "External sites app not enabled"),
        ):
            with mock.patch.object(nc.http, "request", side_effect=responses):
                with self.assertRaisesRegex(watcher.NextcloudError, reason):
                    nc.check()

    def test_bad_settings_stop_the_app(self):
        for extra, reason in (({"NEXTCLOUD_URL": "cloud.example.com"}, "NEXTCLOUD_URL: not an http"),
                              ({"NEXTCLOUD_VERIFY_TLS": "maybe"}, "NEXTCLOUD_VERIFY_TLS: not true or false"),
                              ({"SYNC_INTERVAL": "0"}, "SYNC_INTERVAL: out of range"),
                              ({"REMOVE_GRACE": "-1"}, "REMOVE_GRACE: out of range"),
                              ({"HEALTH_PORT": "70000"}, "HEALTH_PORT: out of range"),
                              ({"HEALTH_PORT": "x"}, "HEALTH_PORT: not a whole number"),
                              ({"DEFAULT_GROUPS": "admins,*"}, "DEFAULT_GROUPS: \\* not allowed"),
                              ({"ICONS_DIR": __file__}, "ICONS_DIR: not a directory")):
            with self.assertRaisesRegex(SystemExit, reason):
                self.configure(**extra)

    def test_state_file_that_cannot_be_written_stops_the_app(self):
        blocker = Path(self.tmp.name, "file")
        blocker.write_text("")
        with self.assertRaisesRegex(SystemExit, "State file: not writable"):
            watcher.check_writable(blocker / "state.json")
        watcher.check_writable(Path(self.cfg.state_file))   # the normal case passes

        self.start("app", url="https://app.example.com")
        with mock.patch("watcher.save_state", side_effect=OSError(30, "Read-only file system")):
            with self.assertRaisesRegex(SystemExit, "State file: not writable: Read-only file system"):
                self.run_sync()

    def test_docker_needs_a_url_or_the_socket(self):
        missing = Path(self.tmp.name, "docker.sock")
        with self.assertRaisesRegex(SystemExit, "Docker: DOCKER_HOST not set and .* not mounted"):
            watcher.check_docker_access({"": None}, missing)
        watcher.check_docker_access({"": "tcp://socket-proxy:2375"}, missing)
        missing.touch()
        watcher.check_docker_access({"": None}, missing)

    def test_docker_hosts_setting(self):
        hosts = watcher.docker_hosts
        self.assertEqual(hosts({}), {"": None})
        self.assertEqual(hosts({"DOCKER_HOST": "tcp://proxy:2375"}), {"": "tcp://proxy:2375"})
        self.assertEqual(hosts({"DOCKER_HOSTS": "unix:///var/run/docker.sock, nas=tcp://nas:2375"}),
                         {"": "unix:///var/run/docker.sock", "nas": "tcp://nas:2375"})
        for value, reason in (("nas=tcp://a:1,nas=tcp://b:1", "nas listed twice"),
                              ("unix:///a,tcp://b:1", "unnamed host listed twice"),
                              ("NAS=tcp://a:1", "bad host name: NAS"),
                              ("nas=nas:2375", "not a unix://")):
            with self.assertRaisesRegex(SystemExit, reason):
                hosts({"DOCKER_HOSTS": value})
        with self.assertRaisesRegex(SystemExit, "DOCKER_HOST: not a unix://"):
            hosts({"DOCKER_HOST": "nas:2375"})

    def test_several_hosts_one_down_leaves_its_links_alone(self):
        nas = FakeDocker()
        self.w = watcher.Watcher({"": lambda: self.docker, "nas": lambda: nas}, self.nc, self.cfg)
        self.start("app", url="https://app.example.com", icon="myapp")
        self.start("app", url="https://nas-app.example.com", icon="myapp", on=nas)
        icons = Path(self.cfg.icons_dir)
        icons.mkdir()
        (icons / "myapp.svg").write_bytes(b"<svg/>")
        self.run_sync()
        self.assertEqual(self.state(), {"app": 1, "nas:app": 2})
        self.assertEqual((self.nc.sites[1]["icon"], self.nc.sites[2]["icon"]), ("app.svg", "nas-app.svg"))

        nas.fail = True
        self.stop("app")
        with self.assertLogs("watcher", "ERROR") as logs:
            self.run_sync(after=100)
            self.run_sync(after=100)
        self.assertEqual(logs.output, ["ERROR:watcher:Docker nas: RuntimeError: socket unreachable"])
        self.assertEqual(self.state(), {"nas:app": 2})      # the reachable host's link went

        self.docker.fail = True                           # every host down: the sync is skipped
        self.assertIsNone(self.run_sync(after=100))

    def test_wrapped_connection_errors_are_short(self):
        class DockerException(Exception):
            pass
        try:
            try:
                raise watcher.requests.exceptions.ConnectionError("Max retries exceeded ...")
            except Exception as inner:
                raise DockerException(f"Error while fetching server API version: {inner}") from inner
        except DockerException as exc:
            self.assertEqual(watcher.describe(exc), "unreachable")

    def test_connection_errors_name_the_os_error(self):
        try:
            try:
                raise PermissionError(13, "Permission denied")
            except Exception as inner:
                raise watcher.requests.exceptions.ConnectionError(inner)
        except Exception as exc:
            self.assertEqual(watcher.describe(exc), "unreachable: Permission denied")

    def test_docker_client_failure_is_reported_not_fatal(self):
        def unreachable():
            raise RuntimeError("Error while fetching server API version")
        self.w = watcher.Watcher({"": unreachable}, self.nc, self.cfg)
        with self.assertLogs("watcher", "ERROR") as logs:
            self.assertIsNone(self.run_sync())
        self.assertEqual(logs.output, ["ERROR:watcher:Docker: RuntimeError: Error while fetching server API version"])

    def test_renamed_container_keeps_its_link(self):
        self.start("old", url="https://app.example.com", cid="c1",
                   raw={f"{P}.admin.url": "https://app.example.com/admin"})
        self.run_sync()
        self.assertEqual(self.state(), {"old": 1, "old/admin": 2})
        self.stop("old")
        self.start("new", url="https://app.example.com", cid="c1",
                   raw={f"{P}.admin.url": "https://app.example.com/admin"})
        with self.assertLogs("watcher", "INFO") as logs:
            self.run_sync()
        self.assertEqual(self.state(), {"new": 1, "new/admin": 2})
        self.assertEqual(sorted(self.nc.sites), [1, 2])
        self.assertEqual(self.nc.sites[1]["name"], "new")             # renamed in place
        self.assertIn("INFO:watcher:new: renamed from old", logs.output)

    def test_recreated_container_with_a_new_name_gets_a_new_link(self):
        self.start("old", url="https://app.example.com", cid="c1")
        self.run_sync()
        self.stop("old")
        self.start("new", url="https://app.example.com", cid="c2")    # a different container
        self.run_sync()
        self.assertEqual(self.state(), {"old": 1, "new": 2})

    def test_unreadable_state_file_skips_sync_instead_of_duplicating(self):
        self.start("app", url="https://app.example.com")
        self.run_sync()
        Path(self.cfg.state_file).write_text("{not json")
        self.assertIsNone(self.run_sync())
        self.assertEqual(len(self.nc.sites), 1)

    def test_failed_icon_falls_back_and_is_not_retried_every_sync(self):
        calls = []
        def failing(url):
            calls.append(url)
            raise OSError("cdn down")
        with mock.patch("watcher.fetch_icon", failing):
            self.start("app", url="https://app.example.com", icon="di-jellyfin")
            self.run_sync()
            self.run_sync(after=300)
            self.assertEqual((self.nc.sites[1]["icon"], len(calls)), ("", 1))
            self.run_sync(after=watcher.ICON_RETRY_SECONDS)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
