"""S7 Electron app sessions backed by the S1 registry and S2 launch custody."""

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import pwd
from typing import Callable

from bus.broker.broker import BrokerError
from session.launch import (
    BINARIES, DISPLAY, LOOPBACK, OPTIONAL_FLAGS, RECEIPT, LaunchError, Launcher,
    LinuxHost, _PortCollision, _profile_path,
)
from session.registry import LaunchNotRunning, OwnedInstance, SessionRefused, SessionRegistry, SiteProfile

from .oauth_browser import OAUTH_BROWSERS


APP_BINARIES = {
    "Codex Desktop": "/usr/lib/chatgpt/ChatGPT",
    "Antigravity 2": "/opt/antigravity-app/antigravity",
}
CDP_ADDRESS_FLAG = f"--remote-debugging-address={LOOPBACK}"
CDP_PORT_FLAG = "--remote-debugging-port="
APP_FLAGS = (CDP_ADDRESS_FLAG, f"{CDP_PORT_FLAG}<int>", *OPTIONAL_FLAGS)
WRAPPER = Path(__file__).with_name("oauth_browser.py").resolve()


@dataclass(frozen=True)
class AppQualification:
    backend: str
    atspi_observation_usable: bool
    descendants_can_reach_cdp: bool

    @classmethod
    def load(cls, app, receipt):
        try:
            data = json.loads(Path(receipt).read_bytes())
            evidence = data["apps"][app]
            cdp = evidence["cdp"]
            rules = data["backend_rules"]["cdp"]
            qualified = (
                data["schema"] == "vp.l0_receipt/v1"
                and data["steps"]["L0-7"]["status"] == "PASS"
                and evidence["kind"] == "app" and evidence["binary"] == APP_BINARIES[app]
                and evidence["primary_backend"] == "cdp"
                and cdp["status"] == "PASS" and cdp["available"] is True
                and cdp["children_inherit_socket"] is True
                and rules["inherited_socket_children_share_reachability"] is True
                and rules["bind_address"] == LOOPBACK and rules["readiness"] == "GET /json/version"
                and rules["read_profile_files"] is False and rules["app_isolated_user_data_dir"] is False
                and tuple(rules["app_allowed_flags"]) == APP_FLAGS
            )
            atspi = evidence.get("atspi", {})
            atspi_usable = atspi.get("available") is True and atspi.get("status") == "USABLE"
        except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
            raise LaunchNotRunning("DISPLAY_UNSUPPORTED", "App evidence is unavailable") from error
        if not qualified:
            raise LaunchNotRunning("DISPLAY_UNSUPPORTED", "App has no qualified launch policy")
        return cls("cdp", atspi_usable, True)


def _app_argv(binary, port, flags):
    return (binary, CDP_ADDRESS_FLAG, f"{CDP_PORT_FLAG}{port}", *flags)


class _ElectronHost(LinuxHost):
    def __init__(self, environment):
        super().__init__()
        self._env.update(environment)


def _page_target(port):
    """Read the unique page ID from the owned listener without selector or URL heuristics."""
    from channel.cdp import _connect

    cdp = _connect(port)
    try:
        pages = [item["targetId"] for item in cdp.send("Target.getTargets")["targetInfos"]
                 if item["type"] == "page"]
        return pages[0] if len(pages) == 1 else None
    finally:
        cdp.close()


@dataclass(frozen=True, kw_only=True)
class _OwnedApp(OwnedInstance):
    probe: Callable[[], str]


class AppLauncher:
    """Trusted provisioning for one Electron app, OAuth profile and capsule binding."""

    def __init__(self, app, profile_id, browser, capsule_id, *, receipt=RECEIPT,
                 host_factory=_ElectronHost, read_target=_page_target, flags=OPTIONAL_FLAGS):
        if app not in APP_BINARIES or browser not in OAUTH_BROWSERS:
            raise LaunchNotRunning("DISPLAY_UNSUPPORTED", "App or OAuth browser is unsupported")
        if not isinstance(capsule_id, str) or not capsule_id.strip():
            raise LaunchNotRunning("identity_mismatch", "Trusted capsule binding is required")
        self.qualification = AppQualification.load(app, receipt)
        self.site = SiteProfile(profile_id, Path(APP_BINARIES[app]), DISPLAY,
                                self.qualification.backend, True)
        self.browser = browser
        self.capsule_id = capsule_id
        self._flags = tuple(flags)
        if any(flag not in OPTIONAL_FLAGS for flag in self._flags) or len(set(self._flags)) != len(self._flags):
            raise LaunchNotRunning("-32602", "Electron app flag is outside the allowlist or duplicated")
        self._receipt = receipt
        self._host_factory = host_factory
        self._read_target = read_target

    def _environment(self, profile):
        return {
            "HOME": pwd.getpwuid(os.geteuid()).pw_dir,
            "BROWSER": str(WRAPPER),
            "VP_DIRECT_OAUTH_BROWSER": self.browser,
            "VP_DIRECT_OAUTH_PROFILE": str(profile),
        }

    def launch(self, site, profile_directory):
        if site != self.site:
            raise LaunchNotRunning("identity_mismatch", "Launch does not match the provisioned app")
        try:
            profile = _profile_path(profile_directory.parent, site.profile_id)
            if profile != profile_directory:
                raise LaunchError("OAuth profile does not match the registry reservation")
            if not os.access(WRAPPER, os.X_OK):
                raise LaunchError("The dedicated OAuth wrapper is unavailable")
            host = self._host_factory(self._environment(profile))
            browser = OAUTH_BROWSERS[self.browser]
            host.verify_binary(browser, BINARIES[browser])
            launcher = Launcher(profile.parent, receipt=self._receipt, host=host)
        except (LaunchError, OSError, KeyError) as error:
            raise LaunchNotRunning(getattr(error, "code", None) or "HOLD",
                                   "Electron launch preflight failed") from error

        tried = {launcher.policy.observed_port}
        for _ in range(launcher.port_attempts):
            try:
                port = host.allocate_port()
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    continue
                raise LaunchNotRunning("HOLD", "Loopback port allocation failed") from error
            if type(port) is not int or not 1 <= port <= 65535:
                raise LaunchNotRunning("PORT_BUSY", "No valid loopback TCP port was allocated")
            if port in tried:
                continue
            tried.add(port)
            try:
                app = launcher._launch_and_dismiss(_app_argv(str(site.executable), port, self._flags), port)
            except _PortCollision:
                continue
            return self._bind_target(app, host)
        raise LaunchNotRunning("PORT_BUSY", "Loopback debugging port retry bound reached")

    def _bind_target(self, app, host):
        def require_owned_window():
            if (not app._child.alive() or host.listener(app._child, app.cdp_port) != "owned"
                    or [w for w in host.windows() if w.pid == app.window.pid] != [app.window]):
                raise SessionRefused("identity_mismatch", "The app window or CDP listener changed")

        def probe():
            require_owned_window()
            target = self._read_target(app.cdp_port)
            require_owned_window()
            if not isinstance(target, str) or not target.strip():
                raise SessionRefused("identity_mismatch", "The app has no unique CDP page target")
            return target

        try:
            target = probe()
            if probe() != target:
                raise SessionRefused("identity_mismatch", "The app page target changed")
        except Exception as error:
            app.stop()
            raise LaunchNotRunning("identity_mismatch", "No stable app target could be bound") from error
        return _OwnedApp(app.window_binding, self.capsule_id, app.witness, app.stop,
                         cdp_port=app.cdp_port, cdp_target_id=target, probe=probe)


class AppSessionRegistry(SessionRegistry):
    """S1 entry point for Electron sessions. Provisioning is supplied by the operator."""

    def __init__(self, apps, **options):
        provisioned = dict(apps)
        by_site = {app.site: app for app in provisioned.values()}
        if (len(by_site) != len(provisioned)
                or any(browser != app.browser for (_, browser), app in provisioned.items())):
            raise ValueError("Each provisioned app site needs a unique launch binding and matching browser")
        super().__init__({key: app.site for key, app in provisioned.items()},
                         lambda site, profile: by_site[site].launch(site, profile), **options)
        self._apps = provisioned

    def vp_session_open(self, args):
        if not isinstance(args, dict) or args.get("kind") != "app":
            raise SessionRefused("-32602", "The Electron session entry point requires kind app")
        return super().vp_session_open(args)

    def probe_owned_login_target(self, session_id):
        """Check target identity in the launcher process during the operator's login check."""
        instance = self._owned.get(session_id) if os.getpid() == self._pid else None
        record = self.get_session(session_id)
        if (not isinstance(instance, _OwnedApp) or not record or record["state"] != "LOGIN_PENDING"
                or record["lease"]["holder"] is not None):
            raise SessionRefused("NO_LEASE", "An owned app in operator login mode is required")
        if (instance.probe() != record["cdp_target_id"] or self.get_session(session_id) != record):
            raise SessionRefused("identity_mismatch", "The registered login target changed")

    def observation_backend(self, session_id, *, broker, surface_id, agent_id, request_id, lock_id,
                            operation="observe"):
        """Resolve the registry's effective read backend for a current broker-locked request."""
        record = self.get_session(session_id)
        if (not record or record["kind"] != "app" or record["state"] != "AGENT"
                or not all(isinstance(value, str) and value
                           for value in (surface_id, agent_id, request_id, lock_id))):
            raise SessionRefused("NO_LEASE", "An active app session and target lock are required")
        candidates = [app for (site_group, _), app in self._apps.items()
                      if (site_group, app.site.profile_id, app.capsule_id)
                      == (record["site_group"], record["profile_id"], record["capsule_id"])]
        if (len(candidates) != 1 or operation != "observe" or record["cdp_allowed"] is not True
                or record["backend"] != candidates[0].qualification.backend):
            raise SessionRefused("DISPLAY_UNSUPPORTED", "The app operation or backend has no W-L0 qualification")
        lease = record["lease"]
        try:
            broker_lease = broker.lease(session_id)
            lock = broker.lock_status(surface_id) or {}
        except BrokerError as error:
            raise SessionRefused("NO_LEASE", "The broker app binding is unavailable") from error
        keys = ("session_id", "surface_id", "agent_id", "request_id", "lock_id", "incarnation", "authority_epoch",
                "status")
        expected = (session_id, surface_id, agent_id, request_id, lock_id, record["incarnation"],
                    lease["authority_epoch"], "held")
        if (lease["holder"] != agent_id or broker_lease != {**lease, "state": "AGENT"}
                or tuple(lock.get(key) for key in keys) != expected):
            raise SessionRefused("NO_LEASE", "The target lock does not grant this app observation")
        return record["backend"]
