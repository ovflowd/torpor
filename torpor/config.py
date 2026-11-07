"""torpor.conf: the facts and ceilings that live on the PC. Parsing, defaults, and edits that keep comments."""

from __future__ import annotations

import configparser
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_TEXT = """\
; Torpor agent settings. `torporctl config set SECTION.KEY VALUE` edits this file and restarts the
; agent. Everything is documented in docs/configuration.md.

[agent]
ha-url =                    ; e.g. http://homeassistant.local:8123, written by torporctl pair
observe-only = no
log-level = info            ; info | debug
tls-verify = yes

[modules]                   ; what runs on this PC at all
power = yes
keep-awake = yes
wake = yes
brightness = yes

[allow]                     ; what Home Assistant may trigger; it can't go beyond this
lock = yes
suspend = yes
hibernate = no
poweroff = no
reboot = no
keep-awake = yes
brightness = yes

[core]
ssh = yes                   ; SSH sessions count as remote
remote-ports =              ; e.g. 3389 5900: inbound connections to these ports count as remote

[busy]
processes =                 ; one regular expression per line, matched against full command lines
lease-ttl = 300             ; seconds an untimed lease file stays live after its last touch

[keep-awake]
backend = auto              ; auto | kde | gnome | portal

[brightness]
backend = auto              ; auto | kde | ddcutil | logind | none
ddcutil = ddcutil           ; path to the ddcutil binary

[games]                     ; what counts as a game running, for the brightness boost
gamemode = yes              ; a Feral GameMode client counts
performance-hold =          ; regular expression for a power-profiles-daemon performance hold's reason

; [display:3f2a9c1b04de]    ; one display, by the ID torporctl status shows
; backend = none            ; auto | kde | ddcutil | logind | none
"""

TRUE = frozenset({"yes", "true", "on", "1"})
FALSE = frozenset({"no", "false", "off", "0"})
MODULE_KEYS = ("power", "keep-awake", "wake", "brightness")
ALLOW_KEYS = ("lock", "suspend", "hibernate", "poweroff", "reboot", "keep-awake", "brightness")
KEEP_AWAKE_BACKENDS = ("auto", "kde", "gnome", "portal")
BRIGHTNESS_BACKENDS = ("auto", "kde", "ddcutil", "logind", "none")
DISPLAY_SECTION = re.compile(r"display:([0-9a-f]{12})")
ALIASES = {"ha-url": "agent.ha-url"}


class ConfigError(ValueError):
    """A value or key that torpor.conf does not accept."""


def wire(key: str) -> str:
    return key.replace("-", "_")


def parse_bool(text: str) -> bool:
    word = text.strip().lower()
    if word in TRUE:
        return True
    if word in FALSE:
        return False
    raise ConfigError(f"{text!r} is not yes or no")


def parse_url(text: str) -> str:
    value = text.strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ConfigError(f"{text!r} is not a URL: {error}") from error
    if parts.scheme not in ("http", "https") or not parts.hostname or port == 0:
        raise ConfigError(f"{text!r} is not an http:// or https:// URL")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"{text!r} must be a plain URL without user, query or fragment")
    return value.rstrip("/")


def parse_ports(text: str) -> tuple[int, ...]:
    ports: list[int] = []
    for word in re.split(r"[\s,]+", text.strip()):
        if not word:
            continue
        if not word.isdecimal() or not 0 < int(word) < 65536:
            raise ConfigError(f"{word!r} is not a TCP port")
        ports.append(int(word))
    return tuple(ports)


def parse_patterns(text: str) -> tuple[str, ...]:
    patterns: list[str] = []
    for line in text.splitlines():
        pattern = line.strip()
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as error:
            raise ConfigError(f"{pattern!r} is not a regular expression: {error}") from error
        patterns.append(pattern)
    return tuple(patterns)


def parse_pattern(text: str) -> str:
    pattern = text.strip()
    try:
        re.compile(pattern)
    except re.error as error:
        raise ConfigError(f"{pattern!r} is not a regular expression: {error}") from error
    return pattern


def parse_ttl(text: str) -> int:
    word = text.strip()
    if not word.isdecimal() or not 10 <= int(word) <= 86400:
        raise ConfigError(f"{text!r} is not a number of seconds from 10 to 86400")
    return int(word)


def choice(*options: str) -> Callable[[str], str]:
    def parse(text: str) -> str:
        word = text.strip().lower()
        if word not in options:
            raise ConfigError(f"{text!r} is not one of {', '.join(options)}")
        return word

    return parse


def text_value(text: str) -> str:
    value = text.strip()
    if not value:
        raise ConfigError("the value is empty")
    return value


@dataclass(frozen=True)
class Key:
    parse: Callable[[str], Any]
    default: Any
    ceiling: bool = False


SCHEMA: dict[str, dict[str, Key]] = {
    "agent": {
        "ha-url": Key(parse_url, ""),
        "observe-only": Key(parse_bool, False),
        "log-level": Key(choice("info", "debug"), "info"),
        "tls-verify": Key(parse_bool, True),
    },
    "modules": {name: Key(parse_bool, True, ceiling=True) for name in MODULE_KEYS},
    "allow": {
        name: Key(parse_bool, name not in ("hibernate", "poweroff", "reboot"), ceiling=True) for name in ALLOW_KEYS
    },
    "core": {"ssh": Key(parse_bool, True), "remote-ports": Key(parse_ports, ())},
    "busy": {"processes": Key(parse_patterns, ()), "lease-ttl": Key(parse_ttl, 300)},
    "keep-awake": {"backend": Key(choice(*KEEP_AWAKE_BACKENDS), "auto")},
    "brightness": {"backend": Key(choice(*BRIGHTNESS_BACKENDS), "auto"), "ddcutil": Key(text_value, "ddcutil")},
    "games": {"gamemode": Key(parse_bool, True), "performance-hold": Key(parse_pattern, "")},
}
DISPLAY_KEYS: dict[str, Key] = {"backend": Key(choice(*BRIGHTNESS_BACKENDS), "auto")}


@dataclass(frozen=True)
class Issue:
    level: str
    message: str


@dataclass(frozen=True)
class Config:
    ha_url: str = ""
    observe_only: bool = False
    log_level: str = "info"
    tls_verify: bool = True
    modules: dict[str, bool] = field(default_factory=lambda: {wire(k): True for k in MODULE_KEYS})
    allow: dict[str, bool] = field(
        default_factory=lambda: {wire(k): SCHEMA["allow"][k].default for k in ALLOW_KEYS},
    )
    ssh: bool = True
    remote_ports: tuple[int, ...] = ()
    processes: tuple[str, ...] = ()
    lease_ttl: int = 300
    keep_awake_backend: str = "auto"
    brightness_backend: str = "auto"
    ddcutil: str = "ddcutil"
    gamemode: bool = True
    performance_hold: str = ""
    displays: dict[str, str] = field(default_factory=dict)
    parsed: bool = True


def schema_for(section: str) -> dict[str, Key] | None:
    if section in SCHEMA:
        return SCHEMA[section]
    if DISPLAY_SECTION.fullmatch(section):
        return DISPLAY_KEYS
    return None


def _parser() -> configparser.ConfigParser:
    return configparser.ConfigParser(
        interpolation=None,
        comment_prefixes=(";", "#"),
        inline_comment_prefixes=(";",),
        empty_lines_in_values=False,
        default_section="\0none",
    )


def parse(text: str) -> tuple[Config, list[Issue]]:
    """Parse torpor.conf. Bad values fall back to their default; a bad ceiling value means no."""
    issues: list[Issue] = []
    reader = _parser()
    try:
        reader.read_string(text)
    except configparser.Error as error:
        message = str(error).splitlines()[0]
        issues.append(Issue("fail", f"torpor.conf does not parse ({message}); nothing is allowed until it is fixed"))
        return Config(allow={wire(k): False for k in ALLOW_KEYS}, parsed=False), issues
    values: dict[tuple[str, str], Any] = {}
    displays: dict[str, str] = {}
    for section in reader.sections():
        keys = schema_for(section)
        if keys is None:
            issues.append(Issue("warn", f"unknown section [{section}]"))
            continue
        for key, raw in reader.items(section):
            spec = keys.get(key)
            if spec is None:
                issues.append(Issue("warn", f"unknown key {section}.{key}"))
                continue
            try:
                value = spec.parse(raw)
            except ValueError as error:
                fallback = False if spec.ceiling else spec.default
                issues.append(Issue("error", f"{section}.{key}: {error}; using {format_value(fallback)}"))
                value = fallback
            if section in SCHEMA:
                values[(section, key)] = value
            else:
                displays[section.removeprefix("display:")] = value

    def get(section: str, key: str) -> Any:
        return values.get((section, key), SCHEMA[section][key].default)

    config = Config(
        ha_url=get("agent", "ha-url"),
        observe_only=get("agent", "observe-only"),
        log_level=get("agent", "log-level"),
        tls_verify=get("agent", "tls-verify"),
        modules={wire(k): get("modules", k) for k in MODULE_KEYS},
        allow={wire(k): get("allow", k) for k in ALLOW_KEYS},
        ssh=get("core", "ssh"),
        remote_ports=get("core", "remote-ports"),
        processes=get("busy", "processes"),
        lease_ttl=get("busy", "lease-ttl"),
        keep_awake_backend=get("keep-awake", "backend"),
        brightness_backend=get("brightness", "backend"),
        ddcutil=get("brightness", "ddcutil"),
        gamemode=get("games", "gamemode"),
        performance_hold=get("games", "performance-hold"),
        displays=displays,
    )
    return config, issues


def load(path: Path) -> tuple[Config, list[Issue]]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return Config(), []
    except OSError as error:
        return Config(allow={wire(k): False for k in ALLOW_KEYS}, parsed=False), [
            Issue("fail", f"cannot read {path}: {error.strerror}")
        ]
    return parse(text)


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, tuple):
        return " ".join(str(item) for item in value)
    return str(value)


def split_key(name: str) -> tuple[str, str]:
    full = ALIASES.get(name, name)
    section, dot, key = full.rpartition(".")
    if not dot or not section or not key:
        raise ConfigError(f"{name!r} is not SECTION.KEY, for example allow.suspend")
    keys = schema_for(section)
    if keys is None:
        raise ConfigError(f"unknown section [{section}]")
    if key not in keys:
        raise ConfigError(f"unknown key {section}.{key}")
    return section, key


def validate(name: str, value: str) -> tuple[str, str, str]:
    """Check a value for `config set` and return the section, key and the text to write."""
    section, key = split_key(name)
    keys = schema_for(section)
    assert keys is not None
    keys[key].parse(value)
    return section, key, value.strip()


def current(config: Config, section: str, key: str) -> Any:
    if section.startswith("display:"):
        return config.displays.get(section.removeprefix("display:"), DISPLAY_KEYS[key].default)
    attribute = {
        ("agent", "ha-url"): "ha_url",
        ("agent", "observe-only"): "observe_only",
        ("agent", "log-level"): "log_level",
        ("agent", "tls-verify"): "tls_verify",
        ("core", "ssh"): "ssh",
        ("core", "remote-ports"): "remote_ports",
        ("busy", "processes"): "processes",
        ("busy", "lease-ttl"): "lease_ttl",
        ("keep-awake", "backend"): "keep_awake_backend",
        ("brightness", "backend"): "brightness_backend",
        ("brightness", "ddcutil"): "ddcutil",
        ("games", "gamemode"): "gamemode",
        ("games", "performance-hold"): "performance_hold",
    }.get((section, key))
    if attribute is not None:
        return getattr(config, attribute)
    if section == "modules":
        return config.modules[wire(key)]
    return config.allow[wire(key)]


def all_values(config: Config) -> list[tuple[str, Any]]:
    rows = [(f"{section}.{key}", current(config, section, key)) for section, keys in SCHEMA.items() for key in keys]
    rows += [(f"display:{display}.backend", backend) for display, backend in sorted(config.displays.items())]
    return rows


_SECTION_LINE = re.compile(r"^\s*\[([^\]]+)\]")
_COMMENT = re.compile(r"\s;")


def _key_line(key: str) -> re.Pattern[str]:
    return re.compile(rf"^(?P<key>{re.escape(key)})\s*[=:](?P<value>.*)$", re.IGNORECASE)


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    start = None
    for index, line in enumerate(lines):
        match = _SECTION_LINE.match(line)
        if match is None:
            continue
        if start is not None:
            return start, index
        if match.group(1).strip() == section:
            start = index
    return None if start is None else (start, len(lines))


def _continuation(line: str) -> bool:
    return bool(line.strip()) and line[:1] in (" ", "\t") and not line.lstrip().startswith((";", "#"))


def _value_lines(key: str, value: str, comment: str) -> list[str]:
    parts = [part.strip() for part in value.splitlines() if part.strip()] or [""]
    first = f"{key} = {parts[0]}".rstrip()
    if comment:
        first = f"{first.ljust(27)} {comment}" if len(first) < 27 else f"{first} {comment}"
    return [first, *(f"    {part}" for part in parts[1:])]


def set_value(text: str, section: str, key: str, value: str) -> str:
    """Set one key in place, keeping comments and order; add it, or its section, at the end when missing."""
    lines = text.splitlines()
    bounds = _section_bounds(lines, section)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{section}]", *_value_lines(key, value, "")]
        return "\n".join(lines) + "\n"
    start, end = bounds
    pattern = _key_line(key)
    for index in range(start + 1, end):
        match = pattern.match(lines[index])
        if match is None:
            continue
        found = _COMMENT.search(match.group("value"))
        comment = match.group("value")[found.start() :].strip() if found else ""
        stop = index + 1
        while stop < end and _continuation(lines[stop]):
            stop += 1
        lines[index:stop] = _value_lines(key, value, comment)
        return "\n".join(lines) + "\n"
    insert = end
    while insert > start + 1 and not lines[insert - 1].strip():
        insert -= 1
    lines[insert:insert] = _value_lines(key, value, "")
    return "\n".join(lines) + "\n"


def unset_value(text: str, section: str, key: str) -> str:
    lines = text.splitlines()
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return text
    start, end = bounds
    pattern = _key_line(key)
    for index in range(start + 1, end):
        if pattern.match(lines[index]):
            stop = index + 1
            while stop < end and _continuation(lines[stop]):
                stop += 1
            del lines[index:stop]
            return "\n".join(lines) + "\n"
    return text
