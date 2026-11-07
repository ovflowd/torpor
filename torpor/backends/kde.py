"""KDE Plasma: PowerDevil's screen brightness (org.kde.ScreenBrightness) and its idle inhibit."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from torpor.bus import Bus, Done, SignalCallback

BRIGHTNESS = ("org.kde.ScreenBrightness", "/org/kde/ScreenBrightness", "org.kde.ScreenBrightness")
DISPLAY = "org.kde.ScreenBrightness.Display"
INHIBIT = (
    "org.freedesktop.PowerManagement",
    "/org/freedesktop/PowerManagement/Inhibit",
    "org.freedesktop.PowerManagement.Inhibit",
)
CONTEXT = "torpor"
NO_OSD = 1


@dataclass(frozen=True)
class KdeDisplay:
    name: str
    label: str
    internal: bool
    maximum: int
    value: int


class ScreenBrightness:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def present(self) -> bool:
        return self.bus.has_owner(BRIGHTNESS[0])

    def displays(self) -> list[KdeDisplay]:
        names = self.bus.get(BRIGHTNESS[0], BRIGHTNESS[1], BRIGHTNESS[2], "DisplaysDBusNames")
        found: list[KdeDisplay] = []
        for name in names:
            props = self.bus.get_all(BRIGHTNESS[0], f"{BRIGHTNESS[1]}/{name}", DISPLAY)
            maximum = int(props.get("MaxBrightness", 0))
            if maximum <= 0:
                continue
            found.append(
                KdeDisplay(
                    name=str(name),
                    label=str(props.get("Label", name)),
                    internal=bool(props.get("IsInternal", False)),
                    maximum=maximum,
                    value=int(props.get("Brightness", 0)),
                )
            )
        return found

    def set(self, name: str, value: int, done: Done) -> None:
        self.bus.call_async(
            (BRIGHTNESS[0], f"{BRIGHTNESS[1]}/{name}", DISPLAY),
            "SetBrightnessWithContext",
            (value, NO_OSD, CONTEXT),
            signature="ius",
            done=done,
        )

    def on_changed(self, callback: Callable[[str, int, str], None]) -> None:
        self.bus.subscribe(
            BRIGHTNESS[0],
            BRIGHTNESS[2],
            "BrightnessChanged",
            BRIGHTNESS[1],
            lambda _path, args: callback(str(args[0]), int(args[1]), str(args[3]) if len(args) > 3 else ""),
        )

    def on_displays(self, callback: Callable[[str, str], None]) -> None:
        def handler(member: str) -> SignalCallback:
            return lambda _path, args: callback(member, str(args[0]))

        for member in ("DisplayAdded", "DisplayRemoved"):
            self.bus.subscribe(BRIGHTNESS[0], BRIGHTNESS[2], member, BRIGHTNESS[1], handler(member))


class PowerDevilInhibit:
    name = "kde"
    bus_name = INHIBIT[0]

    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def inhibit(self, reason: str) -> object:
        (cookie,) = self.bus.call(INHIBIT, "Inhibit", ("Torpor", reason), signature="ss", reply="u")
        return cookie

    def release(self, token: object) -> None:
        self.bus.call(INHIBIT, "UnInhibit", (token,), signature="u")
