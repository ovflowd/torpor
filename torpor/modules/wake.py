"""wake: report the MACs Home Assistant sends the magic packet to, and whether WoL looks armed."""

from __future__ import annotations

import logging
from typing import Any

from torpor.backends import net
from torpor.model import Facts
from torpor.modules.base import Context, Module, problem
from torpor.runtime import run, tool

log = logging.getLogger("torpor")

REFRESH_S = 600


class Wake(Module):
    id = "wake"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.nics: list[net.Nic] = []
        self.scanned_at: float | None = None
        self.scanning = False

    def start(self) -> None:
        self.refresh()

    def resumed(self) -> None:
        self.refresh()

    def tick(self, facts: Facts) -> None:
        if self.scanned_at is None or self.ctx.rt.loop.monotonic() - self.scanned_at >= REFRESH_S:
            self.refresh()

    def scan(self) -> list[net.Nic]:
        rt = self.ctx.rt
        nmcli = tool(rt.env, "TORPOR_NMCLI", "nmcli")
        url = self.ctx.config.ha_url
        address = net.resolve_ha(url) if url else None
        return net.scan(
            rt.sysfs, rt.proc, nmcli=nmcli, run=lambda argv: run(argv, timeout=10, env=rt.env), ha_address=address
        )

    def scan_now(self) -> None:
        if self.enabled:
            self.nics = self.scan()
            self.scanned_at = self.ctx.rt.loop.monotonic()

    def refresh(self) -> None:
        if not self.enabled or self.scanning:
            return
        rt = self.ctx.rt
        self.scanning = True

        def done(nics: list[net.Nic] | None, error: BaseException | None) -> None:
            self.scanning = False
            self.scanned_at = rt.loop.monotonic()
            if error is not None:
                self.ctx.failures.fail("wake", f"listing network interfaces failed: {error}")
                return
            self.ctx.failures.clear("wake")
            if nics is not None and nics != self.nics:
                self.nics = nics
                self.ctx.changed()

        rt.loop.run_in_worker(self.scan, done)

    def capabilities(self) -> dict[str, Any]:
        if not self.enabled:
            return self.disabled_capabilities()
        problems = []
        if not self.nics:
            problems.append(problem("no_nic", "no physical network interface with a MAC address"))
        elif not any(nic.wol == "armed" for nic in self.nics):
            problems.append(problem("wol_not_armed", "Wake-on-LAN is not armed on any interface", "torporctl doctor"))
        return {
            "enabled": True,
            "available": bool(self.nics),
            "backend": None,
            "problems": problems,
            "nics": [nic.wire() for nic in self.nics],
        }
