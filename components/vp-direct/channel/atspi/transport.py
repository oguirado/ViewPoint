"""Persistent GDBus transport for a provisioned local desktop session."""

import math
from pathlib import Path
import re
from threading import Timer
from time import monotonic
from urllib.parse import unquote


class Refused(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


_TENANT_AX = Path("/run/newton-tenant/ax.sock")


def _local_address(address):
    match = isinstance(address, str) and re.fullmatch(
        r"unix:(path|abstract)=([^,;]+)(?:,guid=[0-9a-fA-F]{32})?", address)
    if not match:
        raise Refused("ATSPI_BUS_UNAVAILABLE")
    path = unquote(match[2])
    if "\0" in path or path == str(_TENANT_AX):
        raise Refused("ATSPI_BUS_UNAVAILABLE")
    if match[1] == "abstract":
        return address
    if not path.startswith("/"):
        raise Refused("ATSPI_BUS_UNAVAILABLE")
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        raise Refused("ATSPI_BUS_UNAVAILABLE") from None
    if resolved == _TENANT_AX:
        raise Refused("ATSPI_BUS_UNAVAILABLE")
    return address


def _remaining_ms(deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise Refused("ATSPI_TIMEOUT")
    return max(1, math.ceil(remaining * 1000))


class SessionBus:
    """Use the accessibility address, or discover it once on the session bus."""

    def __init__(self, session_env):
        self._address = session_env.get("AT_SPI_BUS_ADDRESS")
        self._session_address = session_env.get("DBUS_SESSION_BUS_ADDRESS")
        self._connection = None
        self._closed = False

    def _connect(self, address, deadline):
        address = _local_address(address)
        try:
            from gi.repository import Gio, GLib
        except ImportError:
            raise Refused("ATSPI_BUS_UNAVAILABLE") from None
        self._glib = GLib
        self._dbus_error = Gio.DBusError
        cancel = Gio.Cancellable()
        timer = Timer(_remaining_ms(deadline) / 1000, cancel.cancel)
        timer.daemon = True
        timer.start()
        try:
            flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
            conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, cancel)
            conn.set_exit_on_close(False)
            return conn
        except GLib.Error:
            raise Refused("ATSPI_BUS_UNAVAILABLE") from None
        finally:
            timer.cancel()

    def _call(self, conn, dest, path, method, sig, args, reply_sig, deadline):
        interface, name = method.rsplit(".", 1)
        payload = self._glib.Variant(sig, args) if sig else None
        try:
            result = conn.call_sync(dest, path, interface, name, payload,
                                    self._glib.VariantType.new(reply_sig), 0,
                                    _remaining_ms(deadline), None)
        except self._glib.Error as error:
            name = self._dbus_error.get_remote_error(error)
            if name == "org.freedesktop.DBus.Error.NameHasNoOwner" or (
                    dest.startswith(":") and name in {"org.freedesktop.DBus.Error.UnknownObject",
                                                       "org.freedesktop.DBus.Error.ServiceUnknown"}):
                raise Refused("ATSPI_RETIRED_OBJECT") from None
            raise Refused("ATSPI_CALL_FAILED") from None
        return result.unpack()[0]

    def call(self, dest, path, method, sig, args, reply_sig, deadline):
        if self._closed:
            raise Refused("ATSPI_BUS_UNAVAILABLE")
        if self._connection is None:
            address = self._address
            if not address:
                session = self._connect(self._session_address, deadline)
                try:
                    address = self._call(session, "org.a11y.Bus", "/org/a11y/bus",
                                         "org.a11y.Bus.GetAddress", "", (), "(s)", deadline)
                finally:
                    session.close_sync(None)
            self._connection = self._connect(address, deadline)
        if self._connection.is_closed():
            raise Refused("ATSPI_BUS_UNAVAILABLE")
        return self._call(self._connection, dest, path, method, sig, args, reply_sig, deadline)

    def close(self):
        self._closed = True
        conn = self._connection
        if conn is not None and not conn.is_closed():
            conn.close_sync(None)
