"""torpor.conf: defaults, warnings, safe fallbacks, and edits that keep comments (A10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from torpor import config as conf


def test_a_missing_file_means_every_default(tmp_path: Path) -> None:
    config, issues = conf.load(tmp_path / "nothing.conf")
    assert issues == []
    assert config == conf.Config()
    assert config.allow == {
        "lock": True,
        "suspend": True,
        "hibernate": False,
        "poweroff": False,
        "reboot": False,
        "keep_awake": True,
        "brightness": True,
    }
    assert config.modules == {"power": True, "keep_awake": True, "wake": True, "brightness": True}


def test_the_shipped_default_file_parses_to_the_defaults_without_issues() -> None:
    config, issues = conf.parse(conf.DEFAULT_TEXT)
    assert issues == []
    assert config == conf.Config()


@pytest.mark.parametrize("word", ["yes", "Yes", "true", "on", "1"])
def test_booleans_accept_every_spelling_of_yes(word: str) -> None:
    config, issues = conf.parse(f"[allow]\npoweroff = {word}\n")
    assert issues == [] and config.allow["poweroff"] is True


@pytest.mark.parametrize("word", ["no", "FALSE", "off", "0"])
def test_booleans_accept_every_spelling_of_no(word: str) -> None:
    config, _ = conf.parse(f"[allow]\nlock = {word}\n")
    assert config.allow["lock"] is False


def test_unknown_keys_and_sections_are_warnings_and_change_nothing() -> None:
    config, issues = conf.parse("[agent]\ncolour = blue\n[extra]\nx = 1\n")
    assert [i.level for i in issues] == ["warn", "warn"]
    assert "agent.colour" in issues[0].message and "[extra]" in issues[1].message
    assert config == conf.Config()


def test_an_invalid_value_falls_back_to_its_default_and_logs_an_error() -> None:
    config, issues = conf.parse("[busy]\nlease-ttl = forever\n[keep-awake]\nbackend = windows\n")
    assert config.lease_ttl == 300 and config.keep_awake_backend == "auto"
    assert [i.level for i in issues] == ["error", "error"]


def test_an_invalid_ceiling_value_means_no_never_the_default() -> None:
    config, issues = conf.parse("[allow]\nsuspend = sometimes\n[modules]\npower = maybe\n")
    assert config.allow["suspend"] is False
    assert config.modules["power"] is False
    assert all("using no" in issue.message for issue in issues)


def test_a_file_that_does_not_parse_allows_nothing() -> None:
    config, issues = conf.parse("[allow]\nsuspend = yes\nsuspend = yes\n")
    assert issues[0].level == "fail"
    assert not any(config.allow.values())
    assert config.parsed is False


def test_values_are_parsed_into_their_types() -> None:
    text = (
        "[agent]\nha-url = https://ha.example.test:8123/\nlog-level = debug\ntls-verify = no\n"
        "[core]\nremote-ports = 3389, 5900\n"
        "[busy]\nprocesses =\n    rsync .*backup\n    ^/usr/bin/blender\n"
        "[display:3f2a9c1b04de]\nbackend = none\n"
    )
    config, issues = conf.parse(text)
    assert issues == []
    assert config.ha_url == "https://ha.example.test:8123"
    assert config.log_level == "debug" and config.tls_verify is False
    assert config.remote_ports == (3389, 5900)
    assert config.processes == ("rsync .*backup", "^/usr/bin/blender")
    assert config.displays == {"3f2a9c1b04de": "none"}


@pytest.mark.parametrize(
    "url",
    [
        "ftp://ha",
        "homeassistant.local",
        "http://user:pw@ha:8123",
        "http://ha/?x=1",
        "http://[homeassistant",
        "http://ha.example.test:99999",
        "http://ha.example.test:0",
    ],
)
def test_ha_url_must_be_a_plain_http_url(url: str) -> None:
    with pytest.raises(conf.ConfigError):
        conf.parse_url(url)


@pytest.mark.parametrize(
    ("section", "key", "value", "attribute", "default"),
    [
        ("agent", "ha-url", "http://[homeassistant", "ha_url", ""),
        ("agent", "ha-url", "http://ha.example.test:99999", "ha_url", ""),
        ("busy", "lease-ttl", "\u00b3", "lease_ttl", 300),
        ("core", "remote-ports", "\u00b2", "remote_ports", ()),
    ],
)
def test_odd_values_fall_back_instead_of_crashing(
    section: str, key: str, value: str, attribute: str, default: object
) -> None:
    config, issues = conf.parse(f"[{section}]\n{key} = {value}\n")
    assert getattr(config, attribute) == default
    assert [i.level for i in issues] == ["error"]
    with pytest.raises(conf.ConfigError):
        conf.validate(f"{section}.{key}", value)


def test_set_edits_in_place_and_keeps_comments_and_order() -> None:
    text = conf.set_value(conf.DEFAULT_TEXT, "allow", "poweroff", "yes")
    old, new = conf.DEFAULT_TEXT.splitlines(), text.splitlines()
    assert len(old) == len(new)
    changed = [(a, b) for a, b in zip(old, new, strict=True) if a != b]
    assert changed == [("poweroff = no", "poweroff = yes")]


def test_set_keeps_an_inline_comment() -> None:
    text = conf.set_value(conf.DEFAULT_TEXT, "agent", "ha-url", "http://10.0.0.5:8123")
    line = next(line for line in text.splitlines() if line.startswith("ha-url"))
    assert line.startswith("ha-url = http://10.0.0.5:8123")
    assert line.endswith("; e.g. http://homeassistant.local:8123, written by torporctl pair")
    assert conf.parse(text)[0].ha_url == "http://10.0.0.5:8123"


def test_set_adds_a_missing_key_at_the_end_of_its_section() -> None:
    text = "[agent]\nobserve-only = no\n\n[allow]\nlock = yes\n"
    edited = conf.set_value(text, "agent", "log-level", "debug")
    assert edited == "[agent]\nobserve-only = no\nlog-level = debug\n\n[allow]\nlock = yes\n"


def test_set_adds_a_missing_section_at_the_end() -> None:
    edited = conf.set_value("[agent]\nobserve-only = no\n", "display:3f2a9c1b04de", "backend", "none")
    assert edited.endswith("\n\n[display:3f2a9c1b04de]\nbackend = none\n")
    assert conf.parse(edited)[0].displays == {"3f2a9c1b04de": "none"}


def test_set_writes_a_multi_line_value_and_replaces_the_old_continuation_lines() -> None:
    text = conf.set_value(conf.DEFAULT_TEXT, "busy", "processes", "rsync\nborg create")
    text = conf.set_value(text, "busy", "processes", "restic backup")
    config, issues = conf.parse(text)
    assert issues == [] and config.processes == ("restic backup",)


def test_unset_removes_the_key_and_its_continuation_lines() -> None:
    text = conf.set_value(conf.DEFAULT_TEXT, "busy", "processes", "rsync\nborg create")
    text = conf.unset_value(text, "busy", "processes")
    assert "rsync" not in text and "borg" not in text
    assert conf.parse(text)[0].lease_ttl == 300


def test_validate_knows_keys_values_and_the_ha_url_alias() -> None:
    assert conf.validate("ha-url", "http://ha.test:8123") == ("agent", "ha-url", "http://ha.test:8123")
    assert conf.validate("display:3f2a9c1b04de.backend", "ddcutil")[0] == "display:3f2a9c1b04de"
    with pytest.raises(conf.ConfigError):
        conf.validate("allow.everything", "yes")
    with pytest.raises(conf.ConfigError):
        conf.validate("allow.suspend", "perhaps")
    with pytest.raises(conf.ConfigError):
        conf.validate("display:not-an-id.backend", "none")
    with pytest.raises(conf.ConfigError):
        conf.validate("suspend", "yes")
