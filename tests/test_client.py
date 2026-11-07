"""The connection: handshake order, keepalive, back-off per close code, stale keys, pairing state (A2)."""

from __future__ import annotations

import pytest

from conftest import Env
from torpor import protocol
from torpor.protocol import Close


def connected(env: Env) -> Env:
    env.pair()
    env.desk()
    env.start()
    env.advance(2)
    assert env.ha.current is not None
    assert env.agent is not None
    return env


def status(env: Env) -> dict:  # type: ignore[type-arg]
    assert env.agent is not None
    return env.agent.client.status()


def test_the_agent_dials_the_torpor_endpoint_with_a_hello(env: Env) -> None:
    connected(env)
    assert env.ha.transport().urls == [] and env.ha.connections[0].url == "ws://192.0.2.10:8123/api/torpor/v1/ws"
    assert env.ha.connections[0].hello is not None
    assert env.ha.connections[0].hello.agent_id == env.agent_id


def test_https_becomes_wss(env: Env) -> None:
    env.pair(url="https://192.0.2.10")
    env.start()
    env.advance(1)
    assert env.ha.connections[0].url == "wss://192.0.2.10/api/torpor/v1/ws"


def test_the_agent_waits_for_auth_ok_before_any_frame(env: Env) -> None:
    env.pair()
    env.desk()
    original = env.ha.send
    held: list[tuple] = []  # type: ignore[type-arg]

    def hold(conn, message):  # type: ignore[no-untyped-def]
        if message["type"] == "auth_ok":
            held.append((conn, message))
            return
        original(conn, message)

    env.ha.send = hold  # type: ignore[method-assign]
    env.start()
    env.advance(5)
    conn = env.ha.connections[0]
    assert conn.texts == 2 and conn.received == []
    env.ha.send = original  # type: ignore[method-assign]
    original(*held[0])
    env.advance(1)
    assert [m["type"] for m in conn.received][:2] == ["capabilities", "state"]


def test_a_handshake_that_takes_over_10_s_is_closed_4408(env: Env) -> None:
    env.pair()
    env.ha.hello = lambda conn, text: None  # type: ignore[method-assign]
    env.start()
    env.advance(11)
    assert ("agent", Close.TIMEOUT, "handshake timed out") in env.ha.closes


def test_the_agent_pings_every_30_s_and_home_assistant_answers(env: Env) -> None:
    connected(env)
    env.advance(95)
    pings = env.ha.received("ping")
    assert [p["t"] for p in pings] == [1, 2, 3]
    assert env.ha.current is not None


def test_90_s_without_a_frame_closes_4408(env: Env) -> None:
    connected(env)
    env.ha.send = lambda conn, message: None  # type: ignore[method-assign]
    env.advance(125)
    assert ("agent", Close.TIMEOUT, "no frame for 90 s") in env.ha.closes


def test_home_assistant_pings_get_a_pong(env: Env) -> None:
    connected(env)
    conn = env.ha.current
    assert conn is not None
    env.ha.send(conn, {"type": "ping", "t": 77})
    env.loop.run_due()
    assert {"type": "pong", "t": 77} in env.ha.received("pong")


@pytest.mark.parametrize(
    ("code", "delay", "state"),
    [
        (Close.RECONNECT, 1, "backoff"),
        (Close.SUPERSEDED, 60, "backoff"),
        (Close.RATE_LIMITED, 300, "backoff"),
        (Close.NOT_READY, 60, "backoff"),
        (Close.NOT_LOCAL, 900, "rejected"),
        (Close.UNSUPPORTED, 3600, "rejected"),
        (1006, 2, "backoff"),
    ],
)
def test_the_retry_delay_follows_the_close_code(env: Env, code: int, delay: float, state: str) -> None:
    connected(env)
    count = len(env.ha.connections)
    env.ha.drop(code)
    env.loop.run_due()
    assert status(env)["state"] == state
    env.advance(delay - 0.5, step=0.25)
    assert len(env.ha.connections) == count
    env.advance(1, step=0.25)
    assert len(env.ha.connections) == count + 1


def test_jitter_moves_a_delay_by_at_most_20_percent(env: Env) -> None:
    connected(env)
    assert env.agent is not None
    env.agent.client.jitter = lambda: 0.0
    start = env.loop.time()
    env.ha.drop(Close.SUPERSEDED)
    env.loop.run_due()
    assert status(env)["retry_at"] == pytest.approx(start + 48)
    env.advance(60)
    env.agent.client.jitter = lambda: 1.0
    start = env.loop.time()
    env.ha.drop(Close.SUPERSEDED)
    env.loop.run_due()
    assert status(env)["retry_at"] == pytest.approx(start + 72)


def test_an_unreachable_home_assistant_backs_off_2_4_8_up_to_60_s(env: Env) -> None:
    env.pair()
    env.ha.reachable = False
    env.start()
    delays = []
    for _ in range(7):
        env.loop.run_due()
        retry = status(env)["retry_at"]
        delays.append(round(retry - env.loop.time()))
        env.advance(retry - env.loop.time() + 0.01, step=0.5)
    assert delays == [2, 4, 8, 16, 32, 60, 60]
    assert status(env)["last_close"]["reason"] == "connection refused"


def test_an_http_404_is_the_normal_back_off_and_says_so(env: Env) -> None:
    env.pair()
    env.ha.http_status = 404
    env.start()
    env.loop.run_due()
    last = status(env)
    assert last["state"] == "backoff" and last["last_close"]["http"] == 404


def test_failures_reset_once_a_connection_stayed_up_for_60_s(env: Env) -> None:
    env.pair()
    env.ha.reachable = False
    env.start()
    env.advance(15)
    assert status(env)["failures"] >= 3
    env.ha.reachable = True
    env.advance(20)
    assert env.ha.current is not None
    env.advance(61)
    assert status(env)["failures"] == 0
    env.ha.drop()
    env.loop.run_due()
    assert round(status(env)["retry_at"] - env.loop.time()) == 2


def test_a_stale_key_never_sends_a_proof_and_waits_15_minutes(env: Env) -> None:
    env.pair()
    env.ha.entries[env.agent_id] = bytes(32)
    env.start()
    env.loop.run_due()
    env.advance(1)
    conn = env.ha.connections[0]
    assert conn.texts == 1, "the agent answered a challenge it could not verify"
    assert env.ha.bans == 0
    assert env.ha.closes == [("agent", Close.AUTH_FAILED, "home assistant did not prove the key")]
    last = status(env)
    assert last["state"] == "rejected"
    assert last["last_close"]["code"] == Close.AUTH_FAILED
    assert last["retry_at"] - env.loop.time() == pytest.approx(900, rel=0.21)


def test_resume_does_not_dial_early_after_a_rejection(env: Env) -> None:
    env.pair()
    env.ha.entries[env.agent_id] = bytes(32)
    env.start()
    env.advance(1)
    env.world.sleep(True)
    env.world.sleep(False)
    env.advance(5)
    assert len(env.ha.connections) == 1


def test_a_frame_that_fails_authentication_closes_4400(env: Env) -> None:
    connected(env)
    conn = env.ha.current
    assert conn is not None
    assert conn.session is not None
    conn.deliver("binary", b"\x01" + conn.session.sent.to_bytes(8, "big") + b"forged frame and a forged tag")
    env.loop.run_due()
    assert ("agent", Close.MALFORMED, "frame failed authentication") in env.ha.closes


def test_a_text_message_after_the_handshake_closes_4400(env: Env) -> None:
    connected(env)
    conn = env.ha.current
    assert conn is not None
    conn.deliver("text", '{"type":"challenge"}')
    env.loop.run_due()
    assert ("agent", Close.MALFORMED, "text message after the handshake") in env.ha.closes


def test_a_known_message_from_the_wrong_side_closes_4400(env: Env) -> None:
    connected(env)
    conn = env.ha.current
    assert conn is not None
    env.ha.send(conn, {"type": "state", "state": "idle"})
    env.loop.run_due()
    assert ("agent", Close.MALFORMED, "unexpected state") in env.ha.closes


def test_unknown_message_types_are_ignored(env: Env) -> None:
    connected(env)
    conn = env.ha.current
    assert conn is not None
    env.ha.send(conn, {"type": "future_feature", "x": 1})
    env.advance(1)
    assert env.ha.current is conn


def test_without_a_key_the_agent_says_not_paired_and_picks_one_up(env: Env) -> None:
    env.start()
    env.advance(1)
    assert status(env)["state"] == "not_paired"
    assert env.ha.connections == []
    env.pair()
    env.advance(11)
    assert status(env)["state"] == "connected"


def test_capabilities_go_first_then_state_then_config_comes_back(env: Env) -> None:
    connected(env)
    conn = env.ha.connections[0]
    assert [m["type"] for m in conn.received][:2] == ["capabilities", "state"]
    assert env.agent is not None and env.agent.ctx.ha.received


def test_state_changes_are_sent_at_most_once_per_second(env: Env) -> None:
    connected(env)
    before = len(env.states())
    for index in range(5):
        env.tree.connector("card0-DP-2", dpms="Off" if index % 2 == 0 else "On")
        assert env.agent is not None
        env.agent.tick()
        env.loop.advance(0.2, step=0.1)
    sent = env.states()[before:]
    assert 1 <= len(sent) <= 2


def test_protocol_constants_match_the_spec() -> None:
    assert protocol.PING_INTERVAL_S == 30 and protocol.IDLE_TIMEOUT_S == 90 and protocol.GOING_DOWN_ACK_S == 2


def test_a_url_the_transport_refuses_backs_off_instead_of_hanging(env: Env) -> None:
    env.pair()

    def refuse(url, events, *, tls_verify):  # type: ignore[no-untyped-def]
        raise ValueError(f"not a URL: {url}")

    env.start()
    assert env.agent is not None
    env.agent.client.transport.connect = refuse  # type: ignore[method-assign]
    env.ha.drop()
    env.advance(5)
    last = status(env)
    assert last["state"] == "backoff" and last["last_close"]["reason"].startswith("can't connect")
