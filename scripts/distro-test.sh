#!/bin/sh
# Runs the agent's tests on this distro's own Python, PyGObject, libsoup and cryptography, then
# torpord --self-test and torporctl doctor, and checks that every package doctor names exists here.
# CI runs it in an official container with the checkout mounted read-only at /src:
#   docker run --rm -v "$PWD:/src:ro" debian:13 sh /src/scripts/distro-test.sh
set -eu

. /etc/os-release
case "$ID" in
arch)
    pacman -Syu --noconfirm --needed python python-gobject libsoup3 python-cryptography python-pytest dbus >/dev/null
    exists() { pacman -Si "$1" >/dev/null 2>&1; }
    ;;
debian | ubuntu)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends python3 python3-gi gir1.2-glib-2.0 gir1.2-soup-3.0 \
        python3-cryptography python3-pytest dbus-daemon >/dev/null
    exists() { apt-cache show "$1" >/dev/null 2>&1; }
    ;;
fedora)
    dnf -y -q install python3 python3-gobject-base libsoup3 python3-cryptography python3-pytest dbus-daemon >/dev/null
    exists() { dnf -q repoquery "$1" 2>/dev/null | grep -q .; }
    ;;
*)
    echo "no package list for $ID" >&2
    exit 1
    ;;
esac

echo "$PRETTY_NAME"
python3 -c 'import sys, gi, cryptography; print("Python", sys.version.split()[0], "PyGObject", gi.__version__, "cryptography", cryptography.__version__)'
cd /src
export PYTHONDONTWRITEBYTECODE=1
python3 -m pytest -p no:cacheprovider -q
python3 -B -m torpor agent --self-test

status=0
python3 -B -m torpor ctl doctor --json >/tmp/doctor.json || status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 4 ]; then
    echo "torporctl doctor exited $status" >&2
    exit 1
fi
packages=$(python3 -c 'import json; d = json.load(open("/tmp/doctor.json"))["distro"]; print(" ".join(sorted({p for ps in d["packages"].values() for p in ps})))')
for package in $packages; do
    if exists "$package"; then
        echo "package ok: $package"
    else
        echo "doctor names a package this distro doesn't have: $package" >&2
        exit 1
    fi
done
