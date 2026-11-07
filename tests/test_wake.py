"""wake: NICs, the WoL armed/off/unknown rules, and the primary interface (A8)."""

from __future__ import annotations

import json

import pytest

from conftest import Env, Tools
from fakes.tree import Tree
from torpor.backends import net
from torpor.runtime import run


def nm(tools: Tools, devices: dict[str, str], wol: dict[str, str]) -> None:
    tools.set(nm={"devices": devices, "wol": wol})


def scan(tree: Tree, tools: Tools, address: str | None = None) -> list[net.Nic]:
    environment = tools.env() | {"FAKE_TOOLS_STATE": str(tools.state_path)}
    return net.scan(
        tree.sysfs,
        tree.proc,
        nmcli=str(tools.folder / "nmcli"),
        run=lambda argv: run(argv, env=environment),
        ha_address=address,
    )


def test_physical_interfaces_are_listed_and_virtual_ones_skipped(tree: Tree, tools: Tools) -> None:
    tree.nic("enp5s0", "02:00:5E:10:00:01")
    tree.nic("wlp4s0", "02:00:5e:10:00:02", kind="wifi")
    tree.nic("docker0", "02:42:ac:11:00:01", physical=False)
    tree.nic("lo", "00:00:00:00:00:00")
    nics = scan(tree, tools)
    assert [(n.name, n.mac, n.kind) for n in nics] == [
        ("enp5s0", "02:00:5e:10:00:01", "ethernet"),
        ("wlp4s0", "02:00:5e:10:00:02", "wifi"),
    ]


@pytest.mark.parametrize(
    ("wakeup", "setting", "expected"),
    [
        ("enabled", "magic", "armed"),
        ("enabled", "phy, magic", "armed"),
        (None, "magic", "armed"),
        ("disabled", "magic", "off"),
        ("disabled", None, "off"),
        ("enabled", "default", "unknown"),
        ("enabled", "ignore", "unknown"),
        ("enabled", None, "unknown"),
    ],
)
def test_wol_is_armed_off_or_unknown(
    tree: Tree, tools: Tools, wakeup: str | None, setting: str | None, expected: str
) -> None:
    tree.nic("enp5s0", "02:00:5e:10:00:01", wakeup=wakeup)
    if setting is not None:
        nm(tools, {"enp5s0": "Wired connection 1"}, {"Wired connection 1": setting})
    assert scan(tree, tools)[0].wol == expected


def test_nmcli_is_asked_about_the_active_connection_by_its_id(tree: Tree, tools: Tools) -> None:
    tree.nic("enp5s0", "02:00:5e:10:00:01")
    nm(tools, {"enp5s0": "Office LAN"}, {"Office LAN": "magic"})
    scan(tree, tools)
    calls = tools.calls("nmcli")
    assert calls == [
        ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", "enp5s0"],
        ["nmcli", "-g", "802-3-ethernet.wake-on-lan", "connection", "show", "id", "Office LAN"],
    ]


def test_primary_is_the_interface_with_the_route_to_home_assistant(tree: Tree, tools: Tools) -> None:
    tree.nic("enp5s0", "02:00:5e:10:00:01")
    tree.nic("enp3s0", "02:00:5e:10:00:02")
    tree.route("enp5s0", "0.0.0.0", "0.0.0.0")
    tree.route("enp3s0", "192.0.2.0", "255.255.255.0")
    by_route = {n.name: n.primary for n in scan(tree, tools, "192.0.2.10")}
    assert by_route == {"enp5s0": False, "enp3s0": True}
    by_default = {n.name: n.primary for n in scan(tree, tools, "198.51.100.7")}
    assert by_default == {"enp5s0": True, "enp3s0": False}
    assert {n.name: n.primary for n in scan(tree, tools, None)} == {"enp5s0": True, "enp3s0": False}


def test_the_agent_reports_nics_and_flags_wol_not_armed(env: Env) -> None:
    env.tree.nic("enp5s0", "02:00:5e:10:00:01")
    env.tree.route("enp5s0", "0.0.0.0", "0.0.0.0")
    env.pair()
    env.world.greeter()
    env.start()
    env.advance(2)
    wake = env.capabilities()["modules"]["wake"]
    assert wake["available"] is True and wake["backend"] is None
    assert wake["nics"] == [
        {"name": "enp5s0", "mac": "02:00:5e:10:00:01", "kind": "ethernet", "wol": "unknown", "primary": True}
    ]
    assert wake["problems"][0]["code"] == "wol_not_armed"
    assert "wake" not in env.last_state()


def test_the_agent_refreshes_nics_after_a_resume(env: Env) -> None:
    env.tree.nic("enp5s0", "02:00:5e:10:00:01")
    env.pair()
    env.world.greeter()
    env.start()
    env.advance(2)
    nm(env.tools, {"enp5s0": "Wired"}, {"Wired": "magic"})
    env.world.sleep(True)
    env.world.sleep(False)
    env.advance(2)
    assert env.capabilities()["modules"]["wake"]["nics"][0]["wol"] == "armed"
    assert json.dumps(env.capabilities()).count("02:00:5e:10:00:01") == 1


def test_no_nic_means_unavailable(env: Env) -> None:
    env.pair()
    env.start()
    env.advance(2)
    assert env.capabilities()["modules"]["wake"]["available"] is False
