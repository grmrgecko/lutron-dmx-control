#!/bin/bash
#
# install-ola.sh - Build and install the Open Lighting Architecture (OLA) from
#  source on Debian/Raspbian. Verified on Raspberry Pi OS / Raspbian 13 (Trixie)
#  on a single-core ARMv6 Pi (Pi Zero / Pi 1).
#
# OLA is not packaged for recent Debian/Raspbian releases, so we build the
#  0.10.9 release tag from git. This installs the build dependencies, the C++
#  daemon (olad) and the Python client bindings (ola.ClientWrapper) that
#  lutron-dmx-control.py needs when DMX is enabled.
#
# Usage (run as a normal user that can sudo, or as root):
#   bash ./install-ola.sh
#
# Override the version, build location, or parallel-make jobs if needed:
#   OLA_VERSION=0.10.9 BUILD_DIR=~/ola-build JOBS=4 bash ./install-ola.sh
#
# This script was written based on the official OLA build guide; you do not need
#  to follow that guide as well -- running this script is enough. The guide is
#  linked only as a reference for what these steps are doing:
#  https://www.openlighting.org/ola/linuxinstall/

set -e

OLA_VERSION="${OLA_VERSION:-0.10.9}"
BUILD_DIR="${BUILD_DIR:-$HOME/ola-build}"
OLA_REPO="${OLA_REPO:-https://github.com/OpenLightingProject/ola.git}"

# Use sudo only when we are not already root.
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
    if ! command -v sudo >/dev/null 2>&1; then
        echo "This script needs root (apt + make install). Install sudo or run as root."
        exit 1
    fi
fi

echo "==> Installing build dependencies"
$SUDO apt-get update
# Dev packages pull in the matching runtime libraries automatically. The Python
#  client bindings need python3-protobuf and python3-dev.
$SUDO apt-get install -y \
    git build-essential libtool autoconf automake pkg-config \
    bison flex make g++ \
    libcppunit-dev uuid-dev zlib1g-dev libncurses5-dev \
    protobuf-compiler libprotobuf-dev libprotoc-dev \
    libmicrohttpd-dev libftdi1-dev libusb-1.0-0-dev \
    libavahi-client-dev \
    python3 python3-dev python3-protobuf python3-numpy

# On low-memory boards (e.g. the 512MB Pi Zero) the protobuf-heavy C++ files
#  exhaust RAM and the compiler is OOM-killed. Add temporary swap for the build
#  and remove it afterwards. Skipped when there is already plenty of RAM+swap.
SWAPFILE="/swapfile-ola-build"
ADDED_SWAP=0
mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
swap_kb=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
LOW_MEM=0
if [ "$((mem_kb + swap_kb))" -lt 2097152 ]; then
    LOW_MEM=1
    if [ ! -f "$SWAPFILE" ]; then
        echo "==> Low memory detected ($(((mem_kb + swap_kb) / 1024))MB RAM+swap); adding 2G temporary build swap"
        $SUDO fallocate -l 2G "$SWAPFILE" || $SUDO dd if=/dev/zero of="$SWAPFILE" bs=1M count=2048
        $SUDO chmod 600 "$SWAPFILE"
        $SUDO mkswap "$SWAPFILE" >/dev/null
        $SUDO swapon "$SWAPFILE"
        ADDED_SWAP=1
    fi
fi

# Pick the parallel-make job count. On a low-memory board, a full -j<cores> build
#  of the protobuf-heavy C++ thrashes swap and can still get OOM-killed, so cap it
#  (a single-core Pi is unaffected -- nproc is 1 there anyway). Override with JOBS=.
if [ -n "$JOBS" ]; then
    :
elif [ "$LOW_MEM" -eq 1 ]; then
    JOBS=$([ "$(nproc)" -gt 2 ] && echo 2 || nproc)
else
    JOBS=$(nproc)
fi

cleanup_swap() {
    if [ "$ADDED_SWAP" -eq 1 ]; then
        echo "==> Removing temporary build swap"
        $SUDO swapoff "$SWAPFILE" 2>/dev/null || true
        $SUDO rm -f "$SWAPFILE"
    fi
}
trap cleanup_swap EXIT

# If an already-configured tree exists, resume it instead of starting over -
#  make picks up where it left off. Re-running after an interrupted build (the
#  Pi is slow; the compile can take 1-2 hours) just continues.
if [ -f "$BUILD_DIR/Makefile" ] && [ -f "$BUILD_DIR/config.status" ]; then
    echo "==> Found a configured build in $BUILD_DIR; resuming"
    cd "$BUILD_DIR"
else
    echo "==> Fetching OLA $OLA_VERSION into $BUILD_DIR"
    if [ -d "$BUILD_DIR/.git" ]; then
        git -C "$BUILD_DIR" fetch --depth 1 origin "refs/tags/$OLA_VERSION:refs/tags/$OLA_VERSION"
        git -C "$BUILD_DIR" checkout -f "$OLA_VERSION"
    else
        rm -rf "$BUILD_DIR"
        git clone --depth 1 --branch "$OLA_VERSION" "$OLA_REPO" "$BUILD_DIR"
    fi
    cd "$BUILD_DIR"

    echo "==> Generating the build system (autoreconf)"
    autoreconf -i

    # --enable-python-libs:    build/install the Python client (ola.ClientWrapper).
    # --disable-fatal-warnings: OLA 0.10.9 predates GCC 14; without this the new
    #                           default warnings are treated as errors (-Werror).
    # --disable-osc:           OLA 0.10.9's OSC plugin uses an old liblo API that
    #                           no longer compiles against Trixie's liblo. We do
    #                           not use OSC (DMX/sACN only), so disable it.
    echo "==> Configuring"
    PYTHON=python3 ./configure --enable-python-libs --disable-fatal-warnings --disable-osc
fi

echo "==> Building with -j$JOBS (this is slow on a single-core Pi; ~1-2 hours on a Pi Zero)"
make -j"$JOBS"

echo "==> Installing"
$SUDO make install
$SUDO ldconfig

# OLA installs its Python module to .../site-packages, but Debian's python3 only
#  searches .../dist-packages, so 'import ola' fails out of the box. Link the
#  installed package into the dist-packages dir that is actually on sys.path.
SITE_OLA=$(find /usr/local/lib/python3*/site-packages -maxdepth 1 -name ola -type d 2>/dev/null | head -1)
if [ -n "$SITE_OLA" ] && ! python3 -c "import ola" 2>/dev/null; then
    PYDIR=$(dirname "$(dirname "$SITE_OLA")")
    DIST="$PYDIR/dist-packages"
    echo "==> Linking OLA Python module into $DIST (Debian dist-packages)"
    $SUDO mkdir -p "$DIST"
    $SUDO ln -sfn ../site-packages/ola "$DIST/ola"
fi

echo "==> Verifying"
olad --version || true
if python3 -c "import ola.ClientWrapper" 2>/dev/null; then
    echo "    Python bindings (ola.ClientWrapper) import OK"
else
    echo "    WARNING: 'import ola.ClientWrapper' failed - check the install log above."
fi

echo
echo "OLA $OLA_VERSION installed. olad lives at $(command -v olad 2>/dev/null || echo /usr/local/bin/olad)."
echo "Next: install.sh sets up the olad@<user> and lutron-dmx-control@<user> services."
