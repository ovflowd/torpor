#!/usr/bin/env python3
"""Regenerate tests/vectors/*.json and tests/vectors/SHA256SUMS from fixed, synthetic inputs.

Run it only after a deliberate protocol change, then copy protocol.py, test_protocol.py and
tests/vectors/ to ovflowd/ha-torpor unchanged. Key schedules are recomputed here through
cryptography's own HKDF as a second path, so a slip in protocol.py's HKDF fails loudly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand  # noqa: E402

from torpor import protocol as p  # noqa: E402

VECTORS = ROOT / "tests" / "vectors"
MACHINE_ID = "0123456789abcdef0123456789abcdef"
AGENT_ID_FROM_SYSTEMD = "1f399f30-a3d9-488e-a63c-cbeb7333a961"


def seed(label: str, size: int = 32) -> bytes:
    return hashlib.sha256(f"torpor test vector: {label}".encode()).digest()[:size]


KEY = seed("pairing key")
OLD_KEY = seed("old pairing key")
CODE = seed("pairing code", p.CODE_BYTES)
AGENT_EPH = p.Ephemeral(seed("agent nonce"), seed("agent private"))
HA_EPH = p.Ephemeral(seed("ha nonce"), seed("ha private"))


def hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def expand(prk: bytes, info: bytes, length: int) -> bytes:
    return HKDFExpand(algorithm=hashes.SHA256(), length=length, info=info).derive(prk)


def require(condition: bool, what: str) -> None:
    if not condition:
        raise SystemExit(f"cross-check failed: {what}")


CAPABILITIES: dict[str, Any] = {
    "type": "capabilities",
    "observe_only": False,
    "host": {
        "hostname": "desk",
        "os": "Example Linux 1.0",
        "kernel": "6.17.0-example",
        "agent": "0.1.0",
        "python": "3.14.0",
        "vendor": "Example Computers",
        "model": "Tower 5",
        "desktop": "KDE",
        "linger": True,
    },
    "modules": {
        "core": {"enabled": True, "available": True, "backend": "logind", "problems": []},
        "power": {
            "enabled": True,
            "available": True,
            "backend": "logind",
            "problems": [],
            "actions": {"lock": True, "suspend": True, "hibernate": False, "poweroff": False, "reboot": False},
        },
        "keep_awake": {
            "enabled": True,
            "available": True,
            "backend": "kde",
            "problems": [],
            "actions": {"keep_awake": True},
        },
        "wake": {
            "enabled": True,
            "available": True,
            "backend": None,
            "problems": [],
            "nics": [
                {"name": "enp5s0", "mac": "02:00:5e:10:00:01", "kind": "ethernet", "wol": "armed", "primary": True}
            ],
        },
        "brightness": {
            "enabled": True,
            "available": True,
            "backend": None,
            "problems": [],
            "actions": {"brightness": True},
            "displays": [{"id": "3f2a9c1b04de", "label": "Example 27Q", "backend": "kde", "internal": False}],
        },
    },
}

STATE: dict[str, Any] = {
    "type": "state",
    "state": "in_use",
    "session": "unlocked",
    "display_on": True,
    "remote": [],
    "busy": [],
    "boot_time": 1762500000,
    "power": {"can_suspend": "yes", "can_hibernate": "no", "blockers": []},
    "keep_awake": {"on": False, "remaining": None, "backend": "kde", "applied": False},
    "brightness": {
        "lux": 84.0,
        "displays": {"3f2a9c1b04de": {"percent": 31, "auto": True, "overridden": False, "target": 31, "error": None}},
    },
}

CONFIG: dict[str, Any] = {
    "type": "config",
    "modules": {"power": True, "keep_awake": True, "wake": True, "brightness": True},
    "brightness": {
        "lux": True,
        "curve": {"anchor_lux": 100.0, "anchor_pct": 40, "slope": 25.0, "floor": 10, "ceiling": 80},
        "auto": {"3f2a9c1b04de": True},
    },
}

SESSION_FLOW: list[tuple[str, dict[str, Any]]] = [
    ("ha", {"type": "auth_ok", "pairing": False, "ha_version": "2026.11.0", "integration": "0.1.0"}),
    ("agent", CAPABILITIES),
    ("agent", STATE),
    ("ha", CONFIG),
    ("ha", {"type": "input", "name": "lux", "value": 84.0, "age": 12.5}),
    ("ha", {"type": "command", "id": "c1", "name": "suspend", "args": {"force": False}}),
    (
        "agent",
        {
            "type": "result",
            "id": "c1",
            "ok": False,
            "error": {
                "code": "refused",
                "message": "1 app blocks sleep",
                "blockers": [{"who": "Example Player", "why": "Playing video", "what": "sleep"}],
            },
        },
    ),
    (
        "ha",
        {"type": "command", "id": "c2", "name": "brightness_set", "args": {"display": "3f2a9c1b04de", "percent": 45}},
    ),
    ("agent", {"type": "result", "id": "c2", "ok": True, "data": {"percent": 45}}),
    ("ha", {"type": "command", "id": "c3", "name": "keep_awake", "args": {"on": True, "for": 43200}}),
    ("agent", {"type": "result", "id": "c3", "ok": True, "data": {}}),
    ("agent", {"type": "ping", "t": 1}),
    ("ha", {"type": "pong", "t": 1}),
    ("agent", {"type": "going_down", "id": "g1", "kind": "sleep"}),
    ("ha", {"type": "ack", "id": "g1"}),
]

PAIRING_FLOW: list[tuple[str, dict[str, Any]]] = [
    ("ha", {"type": "auth_ok", "pairing": True, "ha_version": "2026.11.0", "integration": "0.1.0"}),
    ("agent", CAPABILITIES),
    ("ha", {"type": "paired", "id": "p1"}),
    ("agent", {"type": "ack", "id": "p1"}),
]


def run_flow(flow: list[tuple[str, dict[str, Any]]], agent: p.Session, ha: p.Session) -> list[dict[str, Any]]:
    frames = []
    for sender, message in flow:
        seal, peer = (agent, ha) if sender == "agent" else (ha, agent)
        seq = seal.sent
        frame = seal.seal(message)
        require(peer.open(frame) == message, f"round trip of {message['type']}")
        frames.append({"from": sender, "seq": seq, "message": message, "frame": frame.hex()})
    return frames


def handshake(pairing: bool) -> dict[str, Any]:
    agent_id = p.agent_id_from_machine_id(MACHINE_ID)
    if pairing:
        agent = p.AgentHandshake(
            agent_id=agent_id, agent="0.1.0", ephemeral=AGENT_EPH, code=CODE, hostname="desk", os="Example Linux 1.0"
        )
    else:
        agent = p.AgentHandshake(agent_id=agent_id, agent="0.1.0", ephemeral=AGENT_EPH, key=KEY)
    hello = agent.hello_text()
    ha = p.HomeAssistantHandshake(p.parse_hello(hello), ephemeral=HA_EPH, secret=CODE if pairing else KEY)
    challenge = ha.challenge()
    auth = agent.answer(challenge)
    ha_session = ha.verify(auth)
    require(agent.session is not None, "agent session")
    agent_session = agent.session

    dh = p.shared_secret(AGENT_EPH.private, p.public_key(HA_EPH.private))
    require(dh == p.shared_secret(HA_EPH.private, p.public_key(AGENT_EPH.private)), "x25519 symmetry")
    agent_epk = p.public_key(AGENT_EPH.private)
    ha_epk = p.public_key(HA_EPH.private)
    if pairing:
        head = p.lp(b"torpor/1 pairing", "1", p.pair_id(CODE), agent_id, "0.1.0", "desk", "Example Linux 1.0")
        salt = hmac.digest(b"torpor/1 pairing code", CODE, "sha256")
        require(salt == p.code_key(CODE), "code key")
        label, size = b"torpor/1 pairing keys", 160
    else:
        head = p.lp(b"torpor/1 session", "1", agent_id, "0.1.0")
        salt, label, size = KEY, b"torpor/1 session keys", 128
    transcript = hashlib.sha256(head + p.lp(AGENT_EPH.nonce, agent_epk, HA_EPH.nonce, ha_epk)).digest()
    okm = hkdf(salt, dh, label + transcript, size)
    keys = {
        "ha_proof": okm[:32].hex(),
        "agent_proof": okm[32:64].hex(),
        "agent_to_ha": okm[64:96].hex(),
        "ha_to_agent": okm[96:128].hex(),
    }
    result: dict[str, Any] = {
        "inputs": {
            "machine_id": MACHINE_ID,
            "agent_id": agent_id,
            "agent": "0.1.0",
            "agent_nonce": AGENT_EPH.nonce.hex(),
            "agent_private": AGENT_EPH.private.hex(),
            "ha_nonce": HA_EPH.nonce.hex(),
            "ha_private": HA_EPH.private.hex(),
        },
        "hello": hello,
        "challenge": challenge,
        "auth": auth,
        "dh": dh.hex(),
        "transcript": transcript.hex(),
        "keys": keys,
    }
    if pairing:
        prk = hmac.digest(salt, dh, "sha256")
        fingerprint = p.format_fingerprint(expand(prk, b"torpor/1 fingerprint" + transcript, 5))
        require(agent.fingerprint == ha.fingerprint == fingerprint, "fingerprint")
        require(agent.pairing_key == ha.pairing_key == okm[128:160], "pairing key")
        result["inputs"] |= {"code": CODE.hex(), "code_text": p.format_code(CODE), "hostname": "desk"}
        result["inputs"]["os"] = "Example Linux 1.0"
        result |= {"pair_id": p.pair_id(CODE), "fingerprint": fingerprint, "pairing_key": okm[128:160].hex()}
        frames = run_flow(PAIRING_FLOW, agent_session, ha_session)
    else:
        result["inputs"]["key"] = KEY.hex()
        frames = run_flow(SESSION_FLOW, agent_session, ha_session)
    result["frames"] = frames
    return result


def primitives() -> dict[str, Any]:
    hkdf_case = {
        "source": "RFC 5869 A.1",
        "ikm": "0b" * 22,
        "salt": "000102030405060708090a0b0c",
        "info": "f0f1f2f3f4f5f6f7f8f9",
        "length": 42,
        "prk": "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5",
        "okm": "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865",
    }
    prk = p.hkdf_extract(bytes.fromhex(hkdf_case["salt"]), bytes.fromhex(hkdf_case["ikm"]))
    require(prk.hex() == hkdf_case["prk"], "RFC 5869 PRK")
    require(p.hkdf_expand(prk, bytes.fromhex(hkdf_case["info"]), 42).hex() == hkdf_case["okm"], "RFC 5869 OKM")
    x25519_case = {
        "source": "RFC 7748 6.1",
        "alice_private": "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a",
        "alice_public": "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a",
        "bob_private": "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb",
        "bob_public": "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f",
        "shared": "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742",
    }
    alice = bytes.fromhex(x25519_case["alice_private"])
    bob_public = bytes.fromhex(x25519_case["bob_public"])
    require(p.public_key(alice).hex() == x25519_case["alice_public"], "RFC 7748 public key")
    require(p.shared_secret(alice, bob_public).hex() == x25519_case["shared"], "RFC 7748 shared secret")
    aead_case = {
        "source": "RFC 8439 2.8.2",
        "key": bytes(range(0x80, 0xA0)).hex(),
        "nonce": "070000004041424344454647",
        "aad": "50515253c0c1c2c3c4c5c6c7",
        "plaintext": (
            b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, "
            b"sunscreen would be it."
        ).hex(),
        "ciphertext": (
            "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d63dbea45e8ca9671282fafb69da92728b"
            "1a71de0a9e060b2905d6a5b67ecd3b3692ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
            "3ff4def08e4b7a9de576d26586cec64b6116"
        ),
        "tag": "1ae10b594f09e26a7e902ecbd0600691",
    }
    sealed = ChaCha20Poly1305(bytes.fromhex(aead_case["key"])).encrypt(
        bytes.fromhex(aead_case["nonce"]), bytes.fromhex(aead_case["plaintext"]), bytes.fromhex(aead_case["aad"])
    )
    require(sealed.hex() == aead_case["ciphertext"] + aead_case["tag"], "RFC 8439 AEAD")
    agent_id = p.agent_id_from_machine_id(MACHINE_ID)
    require(agent_id == AGENT_ID_FROM_SYSTEMD, "agent_id matches systemd-id128")
    code_text = p.format_code(CODE)
    return {
        "hkdf": hkdf_case,
        "x25519": x25519_case,
        "chacha20poly1305": aead_case,
        "lp": [{"parts": ["", "ab"], "hex": p.lp(b"", "ab").hex()}, {"parts": ["torpor"], "hex": p.lp("torpor").hex()}],
        "agent_id": {
            "app_id": str(p.APP_ID),
            "machine_id": MACHINE_ID,
            "agent_id": agent_id,
            "checked_with": "systemd 262: systemd-id128 machine-id --uuid --app-specific=<app_id>",
        },
        "bad_machine_ids": ["", "0" * 32, "0123", "g123456789abcdef0123456789abcdef", MACHINE_ID + "00"],
        "code": {
            "bytes": CODE.hex(),
            "text": code_text,
            "pair_id": p.pair_id(CODE),
            "code_key": p.code_key(CODE).hex(),
            "accepted": [code_text, code_text.upper(), code_text.replace("-", " "), code_text.removeprefix("trp1-")],
            "rejected": [
                "",
                "trp1-",
                code_text[:-1],
                code_text + "a",
                code_text.replace("a", "1", 1) + "!",
                "trp2" + code_text[4:],
            ],
        },
        "fingerprint": {"raw": "0001020304", "text": p.format_fingerprint(bytes.fromhex("0001020304"))},
    }


def expect_error(action: Any) -> dict[str, Any]:
    try:
        action()
    except p.ProtocolError as error:
        return {"close": int(error.close), "ban": error.ban}
    raise SystemExit("expected a ProtocolError")


def failures(session: dict[str, Any], pairing: dict[str, Any]) -> dict[str, Any]:
    agent_id = p.agent_id_from_machine_id(MACHINE_ID)
    stale_agent = p.AgentHandshake(agent_id=agent_id, agent="0.1.0", ephemeral=AGENT_EPH, key=OLD_KEY)
    stale_ha = p.HomeAssistantHandshake(p.parse_hello(stale_agent.hello_text()), ephemeral=HA_EPH, secret=KEY)
    stale_challenge = stale_ha.challenge()
    decoy = p.HomeAssistantHandshake(p.parse_hello(session["hello"]), ephemeral=HA_EPH, secret=None)
    decoy_challenge = decoy.challenge()
    pair_decoy = p.HomeAssistantHandshake(p.parse_hello(pairing["hello"]), ephemeral=HA_EPH, secret=None)
    pair_decoy_challenge = pair_decoy.challenge()
    forged_auth = json.dumps({"type": "auth", "proof": p.b64(bytes(32))}, separators=(",", ":"))
    forged_pair_auth = json.dumps({"type": "pair_auth", "proof": p.b64(bytes(32))}, separators=(",", ":"))

    def ha(hello: str, secret: bytes | None) -> p.HomeAssistantHandshake:
        return p.HomeAssistantHandshake(p.parse_hello(hello), ephemeral=HA_EPH, secret=secret)

    cases: dict[str, Any] = {
        "stale_key": {
            "about": "The PC holds an old key. It rejects Home Assistant's proof and never sends auth, so no ban.",
            "agent_key": OLD_KEY.hex(),
            "ha_key": KEY.hex(),
            "hello": stale_agent.hello_text(),
            "challenge": stale_challenge,
            "agent_error": expect_error(lambda: stale_agent.answer(stale_challenge)),
        },
        "unknown_agent": {
            "about": "No entry has this agent_id. Home Assistant answers with a decoy; nothing counts toward a ban.",
            "challenge": decoy_challenge,
            "agent_error": expect_error(
                lambda: p.AgentHandshake(agent_id=agent_id, agent="0.1.0", ephemeral=AGENT_EPH, key=KEY).answer(
                    decoy_challenge
                )
            ),
            "auth": session["auth"],
            "ha_error": expect_error(lambda: ha(session["hello"], None).verify(session["auth"])),
        },
        "unknown_pair_id": {
            "about": "No pending pairing has this pair_id (expired, used, or a mistyped code).",
            "challenge": pair_decoy_challenge,
            "agent_error": expect_error(
                lambda: p.AgentHandshake(
                    agent_id=agent_id,
                    agent="0.1.0",
                    ephemeral=AGENT_EPH,
                    code=CODE,
                    hostname="desk",
                    os="Example Linux 1.0",
                ).answer(pair_decoy_challenge)
            ),
            "ha_error": expect_error(lambda: ha(pairing["hello"], None).verify(pairing["auth"])),
        },
        "wrong_agent_proof": {
            "about": "A known agent_id with a wrong proof: the only case Home Assistant feeds to its ban counter.",
            "auth": forged_auth,
            "ha_error": expect_error(lambda: ha(session["hello"], KEY).verify(forged_auth)),
        },
        "wrong_pairing_proof": {
            "about": "A known pair_id with a wrong proof counts toward the pairing's own limit, never a ban.",
            "auth": forged_pair_auth,
            "ha_error": expect_error(lambda: ha(pairing["hello"], CODE).verify(forged_pair_auth)),
        },
    }
    zero_epk = json.loads(session["hello"]) | {"epk": p.b64(bytes(32))}
    zero_epk_text = json.dumps(zero_epk, separators=(",", ":"))
    cases["low_order_epk"] = {
        "about": "An all-zero X25519 key gives an all-zero shared secret and is refused.",
        "hello": zero_epk_text,
        "ha_error": expect_error(lambda: ha(zero_epk_text, KEY)),
    }
    hello = json.loads(session["hello"])
    bad_hellos = [
        json.dumps(hello | {"proto": 2}),
        json.dumps(hello | {"proto": True}),
        json.dumps({k: v for k, v in hello.items() if k != "proto"}),
        json.dumps(hello | {"agent_id": "not-a-uuid"}),
        json.dumps(hello | {"agent_id": hello["agent_id"].upper()}),
        json.dumps(hello | {"nonce": p.b64(bytes(31))}),
        json.dumps(hello | {"nonce": "!!!!"}),
        json.dumps(hello | {"epk": 5}),
        json.dumps(hello | {"agent": ""}),
        json.dumps(hello | {"agent": "x" * 33}),
        json.dumps(hello | {"type": "auth"}),
        json.dumps(json.loads(pairing["hello"]) | {"hostname": ""}),
        json.dumps(json.loads(pairing["hello"]) | {"hostname": "desk\n"}),
        json.dumps(json.loads(pairing["hello"]) | {"pair_id": p.b64(bytes(9))}),
        "[]",
        "{",
        '{"type":"hello","proto":1,"x":NaN}',
        json.dumps(hello | {"pad": "x" * p.MAX_HANDSHAKE_TEXT}),
    ]
    cases["bad_hellos"] = [
        {"text": text, "error": expect_error(lambda t=text: p.parse_hello(t))} for text in bad_hellos
    ]

    first = bytes.fromhex(session["frames"][0]["frame"])
    second = bytes.fromhex(session["frames"][3]["frame"])
    tampered = bytearray(first)
    tampered[-1] ^= 0x01
    cases["frames"] = {
        "about": "Opened by a fresh agent session for the session vector; each must close with 4400.",
        "tampered": {"frame": bytes(tampered).hex(), "error": {"close": 4400, "ban": False}},
        "out_of_order": {"frame": second.hex(), "error": {"close": 4400, "ban": False}},
        "bad_version": {"frame": ("02" + first.hex()[2:]), "error": {"close": 4400, "ban": False}},
        "too_short": {"frame": first[:24].hex(), "error": {"close": 4400, "ban": False}},
        "own_frame": {"frame": session["frames"][1]["frame"], "error": {"close": 4400, "ban": False}},
    }
    return cases


def messages() -> dict[str, Any]:
    good = [
        ("agent", False, {"type": "state", "state": "idle"}),
        ("agent", False, {"type": "event", "name": "resumed"}),
        ("agent", False, {"type": "event", "name": "something_newer"}),
        ("agent", False, {"type": "result", "id": "c1", "ok": False, "error": {"code": "refused"}}),
        ("agent", False, {"type": "a_type_from_a_newer_agent", "x": 1}),
        ("ha", False, {"type": "input", "name": "lux", "value": None, "age": 0}),
        ("ha", False, {"type": "input", "name": "lux", "value": 3, "age": 0.5}),
        ("ha", False, {"type": "command", "id": "c9", "name": "a_newer_command", "args": {}}),
        ("ha", True, {"type": "paired", "id": "p1"}),
        ("agent", True, {"type": "ack", "id": "p1"}),
        ("ha", True, {"type": "ping", "t": 3.5}),
    ]
    bad = [
        ("ha", False, {"type": "state", "state": "idle"}),
        ("agent", False, {"type": "command", "id": "c1", "name": "lock", "args": {}}),
        ("agent", False, {"type": "state", "state": 3}),
        ("agent", False, {"type": "result", "id": "c1", "ok": False}),
        ("agent", False, {"type": "result", "id": "c1", "ok": False, "error": {"message": "no code"}}),
        ("agent", False, {"type": "result", "id": "c1", "ok": "yes"}),
        ("agent", False, {"type": "ping", "t": True}),
        ("ha", False, {"type": "input", "name": "lux", "value": "84", "age": 0}),
        ("ha", False, {"type": "input", "name": "lux", "value": 84}),
        ("ha", False, {"type": "command", "id": "c1", "name": "lock", "args": []}),
        ("agent", True, {"type": "state", "state": "idle"}),
        ("ha", True, {"type": "command", "id": "c1", "name": "lock", "args": {}}),
    ]
    undecodable = ["[]", '{"type":""}', '{"no_type":1}', '{"type":"ping","t":NaN}', '{"type":"ping","t":1e999}', "\xff"]
    return {
        "checked": [{"sender": s, "pairing": pairing, "message": m, "ok": True} for s, pairing, m in good]
        + [{"sender": s, "pairing": pairing, "message": m, "ok": False, "close": 4400} for s, pairing, m in bad],
        "undecodable": undecodable,
        "retry": [
            {"close": close, "failures": failures, "delay": p.retry_delay(close, failures)}
            for close, failures in [
                (None, 1),
                (None, 2),
                (None, 5),
                (None, 6),
                (None, 40),
                (1006, 3),
                (4000, 9),
                (4401, 1),
                (4403, 1),
                (4409, 1),
                (4426, 1),
                (4429, 1),
                (4503, 1),
                (404, 1),
            ]
        ],
    }


def write(name: str, data: Any) -> None:
    (VECTORS / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    VECTORS.mkdir(parents=True, exist_ok=True)
    session = handshake(pairing=False)
    pairing = handshake(pairing=True)
    write("primitives.json", primitives())
    write("session.json", session)
    write("pairing.json", pairing)
    write("failures.json", failures(session, pairing))
    write("messages.json", messages())
    sums = [ROOT / "torpor" / "protocol.py", ROOT / "tests" / "test_protocol.py"]
    sums += sorted(VECTORS.glob("*.json"))
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sums]
    (VECTORS / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
