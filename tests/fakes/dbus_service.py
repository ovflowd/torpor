"""A few fake services on a private dbus-daemon, for the GioBus test. Needs PyGObject.

It owns org.freedesktop.login1 and org.kde.ScreenBrightness on the bus whose address is its first
argument, prints "ready" when they are exported, and exits when stdin closes. It never talks to
the real system or session bus.
"""

import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

XML = """
<node>
  <interface name="org.freedesktop.login1.Manager">
    <method name="ListSessions"><arg type="a(susso)" direction="out"/></method>
    <method name="Inhibit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="s" direction="in"/><arg type="h" direction="out"/>
    </method>
    <method name="Suspend"><arg type="b" direction="in"/></method>
    <method name="Echo">
      <arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="a{sv}" direction="in"/>
      <arg type="s" direction="out"/>
    </method>
    <signal name="PrepareForSleep"><arg type="b"/></signal>
  </interface>
  <interface name="org.kde.ScreenBrightness.Display">
    <property name="Label" type="s" access="read"/>
    <property name="MaxBrightness" type="i" access="read"/>
  </interface>
</node>
"""


def main() -> int:
    address = sys.argv[1]
    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    bus = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    loop = GLib.MainLoop()

    def call(conn, sender, path, iface, method, params, invocation):  # type: ignore[no-untyped-def]
        if method == "ListSessions":
            invocation.return_value(GLib.Variant("(a(susso))", ([("3", 1000, "alex", "seat0", "/s/3")],)))
        elif method == "Inhibit":
            read, write = os.pipe()
            os.close(write)
            fds = Gio.UnixFDList.new_from_array([read])
            invocation.return_value_with_unix_fd_list(GLib.Variant("(h)", (0,)), fds)
        elif method == "Suspend":
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "Permission denied")
            conn.emit_signal(
                None,
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                GLib.Variant("(b)", (True,)),
            )
        elif method == "Echo":
            text, number, options = params.unpack()
            invocation.return_value(GLib.Variant("(s)", (f"{text}:{number}:{options['reason']}",)))

    def get(conn, sender, path, iface, prop):  # type: ignore[no-untyped-def]
        return {"Label": GLib.Variant("s", "Example 27Q"), "MaxBrightness": GLib.Variant("i", 10000)}[prop]

    bus.register_object("/org/freedesktop/login1", node.interfaces[0], call, None, None)
    bus.register_object("/org/kde/ScreenBrightness/display0", node.interfaces[1], None, get, None)
    owned = []

    def acquired(_conn, name):  # type: ignore[no-untyped-def]
        owned.append(name)
        if len(owned) == 2:
            print("ready", flush=True)

    for name in ("org.freedesktop.login1", "org.kde.ScreenBrightness"):
        Gio.bus_own_name_on_connection(bus, name, Gio.BusNameOwnerFlags.NONE, acquired, None)

    def stdin_closed(_fd, _condition):  # type: ignore[no-untyped-def]
        loop.quit()
        return False

    GLib.io_add_watch(
        GLib.IOChannel.unix_new(0), GLib.PRIORITY_DEFAULT, GLib.IOCondition.HUP | GLib.IOCondition.IN, stdin_closed
    )
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
