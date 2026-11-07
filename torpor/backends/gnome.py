"""GNOME's session manager idle inhibit."""

from __future__ import annotations

from torpor.bus import Bus

SESSION_MANAGER = ("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager")
SUSPEND = 4
IDLE = 8


class GnomeInhibit:
    name = "gnome"
    bus_name = SESSION_MANAGER[0]

    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def inhibit(self, reason: str) -> object:
        (cookie,) = self.bus.call(
            SESSION_MANAGER, "Inhibit", ("torpor", 0, reason, SUSPEND | IDLE), signature="susu", reply="u"
        )
        return cookie

    def release(self, token: object) -> None:
        self.bus.call(SESSION_MANAGER, "Uninhibit", (token,), signature="u")
