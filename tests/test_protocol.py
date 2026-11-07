"""Protocol v1 against the shared test vectors. This file is byte-identical in ovflowd/torpor and ovflowd/ha-torpor."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

try:
    from torpor import protocol as p
except ModuleNotFoundError:
    from custom_components.torpor import protocol as p

VECTORS = Path(__file__).parent / "vectors"


def load(name: str) -> Any:
    return json.loads((VECTORS / name).read_text())


PRIMITIVES = load("primitives.json")
SESSION = load("session.json")
PAIRING = load("pairing.json")
FAILURES = load("failures.json")
MESSAGES = load("messages.json")


def ephemeral(inputs: dict[str, str], side: str) -> p.Ephemeral:
    return p.Ephemeral(bytes.fromhex(inputs[f"{side}_nonce"]), bytes.fromhex(inputs[f"{side}_private"]))


def agent_for(vector: dict[str, Any], **override: Any) -> p.AgentHandshake:
    inputs = vector["inputs"]
    if "code" in inputs:
        secret: dict[str, Any] = {
            "code": bytes.fromhex(inputs["code"]),
            "hostname": inputs["hostname"],
            "os": inputs["os"],
        }
    else:
        secret = {"key": bytes.fromhex(inputs["key"])}
    arguments = {"agent_id": inputs["agent_id"], "agent": inputs["agent"], "ephemeral": ephemeral(inputs, "agent")}
    return p.AgentHandshake(**(arguments | secret | override))


def ha_for(vector: dict[str, Any], hello: str, secret: bytes | None) -> p.HomeAssistantHandshake:
    return p.HomeAssistantHandshake(p.parse_hello(hello), ephemeral=ephemeral(vector["inputs"], "ha"), secret=secret)


def secret_of(vector: dict[str, Any]) -> bytes:
    return bytes.fromhex(vector["inputs"].get("code") or vector["inputs"]["key"])


def raises(error: dict[str, Any], action: Any) -> None:
    with pytest.raises(p.ProtocolError) as caught:
        action()
    assert {"close": int(caught.value.close), "ban": caught.value.ban} == error


def test_protocol_file_and_vectors_match_the_pinned_sha256sums() -> None:
    files = {
        "protocol.py": Path(p.__file__),
        "test_protocol.py": Path(__file__),
        **{path.name: path for path in VECTORS.glob("*.json")},
    }
    pinned = dict(reversed(line.split("  ", 1)) for line in (VECTORS / "SHA256SUMS").read_text().splitlines())
    assert set(pinned) == set(files)
    for name, path in files.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pinned[name], f"{name} drifted from SHA256SUMS"


def test_hkdf_matches_rfc_5869() -> None:
    case = PRIMITIVES["hkdf"]
    prk = p.hkdf_extract(bytes.fromhex(case["salt"]), bytes.fromhex(case["ikm"]))
    assert prk.hex() == case["prk"]
    assert p.hkdf_expand(prk, bytes.fromhex(case["info"]), case["length"]).hex() == case["okm"]
    with pytest.raises(ValueError):
        p.hkdf_expand(prk, b"", 255 * 32 + 1)


def test_x25519_matches_rfc_7748() -> None:
    case = PRIMITIVES["x25519"]
    alice = bytes.fromhex(case["alice_private"])
    bob = bytes.fromhex(case["bob_private"])
    assert p.public_key(alice).hex() == case["alice_public"]
    assert p.public_key(bob).hex() == case["bob_public"]
    assert p.shared_secret(alice, bytes.fromhex(case["bob_public"])).hex() == case["shared"]
    assert p.shared_secret(bob, bytes.fromhex(case["alice_public"])).hex() == case["shared"]


def test_the_installed_chacha20_poly1305_matches_rfc_8439() -> None:
    case = PRIMITIVES["chacha20poly1305"]
    aead = ChaCha20Poly1305(bytes.fromhex(case["key"]))
    sealed = aead.encrypt(bytes.fromhex(case["nonce"]), bytes.fromhex(case["plaintext"]), bytes.fromhex(case["aad"]))
    assert sealed.hex() == case["ciphertext"] + case["tag"]


def test_length_prefixes_are_four_byte_big_endian() -> None:
    for case in PRIMITIVES["lp"]:
        assert p.lp(*case["parts"]).hex() == case["hex"]


def test_agent_id_is_the_systemd_app_specific_machine_id() -> None:
    case = PRIMITIVES["agent_id"]
    assert str(p.APP_ID) == case["app_id"]
    assert p.agent_id_from_machine_id(case["machine_id"]) == case["agent_id"]
    assert p.agent_id_from_machine_id(case["machine_id"].upper() + "\n") == case["agent_id"]
    for bad in PRIMITIVES["bad_machine_ids"]:
        with pytest.raises(ValueError):
            p.agent_id_from_machine_id(bad)


def test_pairing_codes_format_parse_and_reject_typos() -> None:
    case = PRIMITIVES["code"]
    code = bytes.fromhex(case["bytes"])
    assert p.format_code(code) == case["text"]
    assert p.pair_id(code) == case["pair_id"]
    assert p.code_key(code).hex() == case["code_key"]
    for text in case["accepted"]:
        assert p.parse_code(text) == code
    for text in case["rejected"]:
        with pytest.raises(ValueError):
            p.parse_code(text)
    with pytest.raises(ValueError):
        p.format_code(code[:-1])
    assert len(p.new_code()) == p.CODE_BYTES
    assert p.parse_code(p.format_code(p.new_code()))


def test_fingerprints_are_eight_base32_characters() -> None:
    case = PRIMITIVES["fingerprint"]
    assert p.format_fingerprint(bytes.fromhex(case["raw"])) == case["text"]


@pytest.mark.parametrize("vector", [SESSION, PAIRING], ids=["session", "pairing"])
def test_handshake_reproduces_every_message_byte_for_byte(vector: dict[str, Any]) -> None:
    agent = agent_for(vector)
    assert agent.hello_text() == vector["hello"]
    ha = ha_for(vector, vector["hello"], secret_of(vector))
    assert ha.challenge() == vector["challenge"]
    assert agent.answer(vector["challenge"]) == vector["auth"]
    ha.verify(vector["auth"])
    assert agent.fingerprint == ha.fingerprint == vector.get("fingerprint")
    pairing_key = vector.get("pairing_key")
    expected_key = bytes.fromhex(pairing_key) if pairing_key else None
    assert agent.pairing_key == ha.pairing_key == expected_key


@pytest.mark.parametrize("vector", [SESSION, PAIRING], ids=["session", "pairing"])
def test_published_keys_follow_from_the_transcript(vector: dict[str, Any]) -> None:
    inputs = vector["inputs"]
    hello = p.parse_hello(vector["hello"])
    agent_epk = p.public_key(bytes.fromhex(inputs["agent_private"]))
    ha_epk = p.public_key(bytes.fromhex(inputs["ha_private"]))
    if hello.pairing:
        head = p.lp(b"torpor/1 pairing", "1", hello.pair_id, hello.agent_id, hello.agent, hello.hostname, hello.os)
        salt, label, size = p.code_key(secret_of(vector)), b"torpor/1 pairing keys", 160
    else:
        head = p.lp(b"torpor/1 session", "1", hello.agent_id, hello.agent)
        salt, label, size = secret_of(vector), b"torpor/1 session keys", 128
    transcript = hashlib.sha256(head + p.lp(hello.nonce, agent_epk, bytes.fromhex(inputs["ha_nonce"]), ha_epk))
    assert transcript.hexdigest() == vector["transcript"]
    dh = p.shared_secret(bytes.fromhex(inputs["agent_private"]), ha_epk)
    assert dh.hex() == vector["dh"]
    okm = p.hkdf_expand(p.hkdf_extract(salt, dh), label + transcript.digest(), size)
    keys = vector["keys"]
    assert okm[:128].hex() == keys["ha_proof"] + keys["agent_proof"] + keys["agent_to_ha"] + keys["ha_to_agent"]
    if hello.pairing:
        assert okm[128:].hex() == vector["pairing_key"]


@pytest.mark.parametrize("vector", [SESSION, PAIRING], ids=["session", "pairing"])
def test_frames_match_in_both_directions(vector: dict[str, Any]) -> None:
    keys = vector["keys"]
    to_ha = bytes.fromhex(keys["agent_to_ha"])
    to_agent = bytes.fromhex(keys["ha_to_agent"])
    agent = p.Session(send_key=to_ha, receive_key=to_agent)
    ha = p.Session(send_key=to_agent, receive_key=to_ha)
    pairing = vector is PAIRING
    for entry in vector["frames"]:
        sender, receiver, peer = (agent, ha, "agent") if entry["from"] == "agent" else (ha, agent, "ha")
        assert sender.sent == entry["seq"]
        assert sender.seal(entry["message"]).hex() == entry["frame"]
        opened = receiver.open(bytes.fromhex(entry["frame"]))
        assert p.check(opened, peer, pairing=pairing) == entry["message"]


def test_handshake_sessions_use_the_published_keys() -> None:
    agent = agent_for(SESSION)
    ha = ha_for(SESSION, SESSION["hello"], secret_of(SESSION))
    agent.answer(ha.challenge())
    ha_session = ha.verify(SESSION["auth"])
    assert agent.session is not None
    first = SESSION["frames"][0]
    assert ha_session.seal(first["message"]).hex() == first["frame"]
    assert agent.session.open(bytes.fromhex(first["frame"])) == first["message"]


def test_a_stale_key_fails_on_the_pc_before_it_ever_sends_a_proof() -> None:
    case = FAILURES["stale_key"]
    agent = agent_for(SESSION, key=bytes.fromhex(case["agent_key"]))
    assert agent.hello_text() == case["hello"]
    ha = ha_for(SESSION, case["hello"], bytes.fromhex(case["ha_key"]))
    assert ha.challenge() == case["challenge"]
    raises(case["agent_error"], lambda: agent.answer(case["challenge"]))
    assert agent.session is None


def test_an_unknown_agent_gets_a_decoy_that_never_counts_toward_a_ban() -> None:
    case = FAILURES["unknown_agent"]
    decoy = ha_for(SESSION, SESSION["hello"], None)
    assert decoy.challenge() == case["challenge"]
    real = json.loads(SESSION["challenge"])
    fake = json.loads(case["challenge"])
    assert list(fake) == list(real)
    assert all(len(fake[key]) == len(real[key]) for key in ("nonce", "epk", "proof"))
    raises(case["agent_error"], lambda: agent_for(SESSION).answer(case["challenge"]))
    raises(case["ha_error"], lambda: ha_for(SESSION, SESSION["hello"], None).verify(case["auth"]))


def test_an_unknown_pair_id_gets_a_decoy_too() -> None:
    case = FAILURES["unknown_pair_id"]
    assert ha_for(PAIRING, PAIRING["hello"], None).challenge() == case["challenge"]
    raises(case["agent_error"], lambda: agent_for(PAIRING).answer(case["challenge"]))
    raises(case["ha_error"], lambda: ha_for(PAIRING, PAIRING["hello"], None).verify(PAIRING["auth"]))


def test_only_a_wrong_proof_for_a_known_key_is_ban_worthy() -> None:
    case = FAILURES["wrong_agent_proof"]
    assert case["ha_error"]["ban"] is True
    raises(case["ha_error"], lambda: ha_for(SESSION, SESSION["hello"], secret_of(SESSION)).verify(case["auth"]))
    pairing = FAILURES["wrong_pairing_proof"]
    assert pairing["ha_error"]["ban"] is False
    raises(pairing["ha_error"], lambda: ha_for(PAIRING, PAIRING["hello"], secret_of(PAIRING)).verify(pairing["auth"]))


def test_a_low_order_ephemeral_key_is_refused_on_both_sides() -> None:
    case = FAILURES["low_order_epk"]
    raises(case["ha_error"], lambda: ha_for(SESSION, case["hello"], secret_of(SESSION)))
    challenge = json.loads(SESSION["challenge"]) | {"epk": p.b64(bytes(32))}
    raises(case["ha_error"], lambda: agent_for(SESSION).answer(json.dumps(challenge)))


def test_bad_hellos_close_with_the_documented_codes() -> None:
    for case in FAILURES["bad_hellos"]:
        raises(case["error"], lambda text=case["text"]: p.parse_hello(text))
    assert {case["error"]["close"] for case in FAILURES["bad_hellos"]} == {p.Close.MALFORMED, p.Close.UNSUPPORTED}


def test_challenges_for_the_wrong_mode_or_version_are_refused() -> None:
    session_agent = agent_for(SESSION)
    raises({"close": 4400, "ban": False}, lambda: session_agent.answer(PAIRING["challenge"]))
    newer = json.loads(SESSION["challenge"]) | {"proto": 2}
    raises({"close": 4426, "ban": False}, lambda: agent_for(SESSION).answer(json.dumps(newer)))
    ha = ha_for(SESSION, SESSION["hello"], secret_of(SESSION))
    raises({"close": 4400, "ban": False}, lambda: ha.verify(PAIRING["auth"]))
    raises({"close": 4400, "ban": False}, lambda: ha.verify(b"\xff"))


@pytest.mark.parametrize("name", ["tampered", "out_of_order", "bad_version", "too_short", "own_frame"])
def test_damaged_replayed_or_reflected_frames_close_the_session(name: str) -> None:
    case = FAILURES["frames"][name]
    keys = SESSION["keys"]
    agent = p.Session(send_key=bytes.fromhex(keys["agent_to_ha"]), receive_key=bytes.fromhex(keys["ha_to_agent"]))
    raises(case["error"], lambda: agent.open(bytes.fromhex(case["frame"])))
    assert agent.received == 0


def test_a_replayed_frame_is_refused_after_the_original() -> None:
    keys = SESSION["keys"]
    agent = p.Session(send_key=bytes.fromhex(keys["agent_to_ha"]), receive_key=bytes.fromhex(keys["ha_to_agent"]))
    first = bytes.fromhex(SESSION["frames"][0]["frame"])
    agent.open(first)
    raises({"close": 4400, "ban": False}, lambda: agent.open(first))


def test_message_checks_follow_the_vectors() -> None:
    for case in MESSAGES["checked"]:
        if case["ok"]:
            assert p.check(case["message"], case["sender"], pairing=case["pairing"]) == case["message"]
        else:
            raises(
                {"close": case["close"], "ban": False},
                lambda c=case: p.check(c["message"], c["sender"], pairing=c["pairing"]),
            )
    for text in MESSAGES["undecodable"]:
        raises({"close": 4400, "ban": False}, lambda t=text: p.decode(t.encode()))
    raises({"close": 4400, "ban": False}, lambda: p.decode(b"\xff"))


def test_size_limits_hold_on_both_ends() -> None:
    keys = SESSION["keys"]
    agent = p.Session(send_key=bytes.fromhex(keys["agent_to_ha"]), receive_key=bytes.fromhex(keys["ha_to_agent"]))
    ha = p.Session(send_key=bytes.fromhex(keys["ha_to_agent"]), receive_key=bytes.fromhex(keys["agent_to_ha"]))
    envelope = len(p.encode({"type": "x", "pad": ""}))
    largest = {"type": "x", "pad": "a" * (p.MAX_PLAINTEXT - envelope)}
    frame = agent.seal(largest)
    assert len(frame) == p.MAX_FRAME
    assert ha.open(frame) == largest
    with pytest.raises(ValueError):
        agent.seal({"type": "x", "pad": "a" * p.MAX_PLAINTEXT})
    with pytest.raises(ValueError):
        p.encode({"pad": 1})
    raises({"close": 4400, "ban": False}, lambda: ha.open(frame + b"x"))
    raises({"close": 4400, "ban": False}, lambda: p.decode(b" " * (p.MAX_PLAINTEXT + 1)))
    raises({"close": 4400, "ban": False}, lambda: p.decode(b"[" * 60000))


def test_retry_delays_follow_the_close_codes() -> None:
    for case in MESSAGES["retry"]:
        assert p.retry_delay(case["close"], case["failures"]) == case["delay"]


def test_handshakes_refuse_ambiguous_or_wrong_sized_secrets() -> None:
    inputs = SESSION["inputs"]
    base = {"agent_id": inputs["agent_id"], "agent": inputs["agent"], "ephemeral": p.Ephemeral.generate()}
    with pytest.raises(ValueError):
        p.AgentHandshake(**base)
    with pytest.raises(ValueError):
        p.AgentHandshake(**base, key=bytes(32), code=bytes(20))
    with pytest.raises(ValueError):
        p.AgentHandshake(**base, key=bytes(31))
    with pytest.raises(ValueError):
        p.HomeAssistantHandshake(p.parse_hello(SESSION["hello"]), ephemeral=p.Ephemeral.generate(), secret=bytes(31))


def test_fresh_ephemerals_pair_and_talk_end_to_end() -> None:
    code = p.new_code()
    agent = p.AgentHandshake(
        agent_id=SESSION["inputs"]["agent_id"],
        agent="9.9.9",
        ephemeral=p.Ephemeral.generate(),
        code=p.parse_code(p.format_code(code)),
        hostname="laptop",
        os="",
    )
    hello = p.parse_hello(agent.hello_text())
    assert hello.pair_id == p.pair_id(code)
    ha = p.HomeAssistantHandshake(hello, ephemeral=p.Ephemeral.generate(), secret=code)
    ha_session = ha.verify(agent.answer(ha.challenge()))
    assert agent.session is not None
    assert agent.pairing_key == ha.pairing_key
    assert agent.fingerprint == ha.fingerprint
    assert agent.session.open(ha_session.seal({"type": "paired", "id": "p1"})) == {"type": "paired", "id": "p1"}

    key = agent.pairing_key
    assert key is not None
    agent = p.AgentHandshake(agent_id=hello.agent_id, agent="9.9.9", ephemeral=p.Ephemeral.generate(), key=key)
    ha = p.HomeAssistantHandshake(p.parse_hello(agent.hello_text()), ephemeral=p.Ephemeral.generate(), secret=key)
    ha_session = ha.verify(agent.answer(ha.challenge()))
    assert agent.session is not None
    assert ha_session.open(agent.session.seal({"type": "ping", "t": 1})) == {"type": "ping", "t": 1}
