"""The XDG desktop portal's Inhibit, for desktops whose portal backend implements it."""

from __future__ import annotations

from torpor.bus import Bus, Variant

PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Inhibit")
REQUEST = "org.freedesktop.portal.Request"
SUSPEND = 4
IDLE = 8


class PortalInhibit:
    name = "portal"
    bus_name = PORTAL[0]

    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def inhibit(self, reason: str) -> object:
        (handle,) = self.bus.call(
            PORTAL, "Inhibit", ("", SUSPEND | IDLE, {"reason": Variant("s", reason)}), signature="sua{sv}", reply="o"
        )
        return handle

    def release(self, token: object) -> None:
        self.bus.call((PORTAL[0], str(token), REQUEST), "Close")
