#!/bin/bash

set -e

USER=$(whoami)
if [ "$USER" != "root" ]; then
    echo "Please use sudo with this install script to ensure right permissions for installation."
    exit 1
fi

# Service user (matches the systemd template instance: lutron-dmx-control@<TARGET_USER>).
TARGET_USER="${TARGET_USER:-pi}"
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
if [ -z "$TARGET_HOME" ]; then
    echo "Target user '$TARGET_USER' does not exist. Re-run with TARGET_USER=<name> sudo ./install.sh"
    exit 1
fi

# The systemd template (lutron-dmx-control@.service) runs
#  /home/%i/lutron-dmx-control.py, so it expects the script at /home/$TARGET_USER.
#  Warn if this user's home is elsewhere -- the service would fail to start.
if [ "$TARGET_HOME" != "/home/$TARGET_USER" ]; then
    echo "WARNING: $TARGET_USER's home is '$TARGET_HOME', but the systemd unit runs"
    echo "  /home/$TARGET_USER/lutron-dmx-control.py. Edit ExecStart in"
    echo "  lutron-dmx-control@.service (and re-copy it) to point at \$TARGET_HOME,"
    echo "  or the service will not start."
fi

# Get the script directory.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# Install Python/needed modules.
# Note: OLA's Python bindings come from the OLA install itself (built from
#  source per the README), not from PyPI. The PyPI 'ola' package is unrelated.
# Refresh the package lists first; on a fresh image the cache may be empty/stale
#  and the install would otherwise fail with "Unable to locate package".
apt-get update
apt-get install -y python3-pip python3-serial python3 python3-paho-mqtt python3-yaml

# Install the script, but don't clobber an existing one (it may have local edits
#  prior to the env-file migration).
if [ ! -f "$TARGET_HOME/lutron-dmx-control.py" ]; then
    cp lutron-dmx-control.py "$TARGET_HOME/lutron-dmx-control.py"
    chown "$TARGET_USER:" "$TARGET_HOME/lutron-dmx-control.py"
else
    echo "$TARGET_HOME/lutron-dmx-control.py already exists; not overwriting."
    echo "  Delete it and re-run to install the new version."
fi

# Install config file (only if not already present, to preserve secrets).
CONFIG_DIR=/etc/lutron-dmx-control
CONFIG_FILE="$CONFIG_DIR/config.yaml"
# Owned by the service user so the service (running as $TARGET_USER) can read it.
install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 700 "$CONFIG_DIR"
# Track whether we just laid down a fresh (unedited) config so we can prompt the
#  user to edit it before the first start instead of crash-looping on placeholders.
NEW_CONFIG=0
if [ ! -f "$CONFIG_FILE" ]; then
    cp config.example.yaml "$CONFIG_FILE"
    chown "$TARGET_USER:" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    NEW_CONFIG=1
    echo "Installed $CONFIG_FILE - edit before starting the service."
else
    echo "$CONFIG_FILE already exists; leaving in place."
fi

# Copy systemd units.
cp olad@.service /etc/systemd/system/
cp lutron-dmx-control@.service /etc/systemd/system/
systemctl daemon-reload

# olad (OLA) is built from source by install-ola.sh and is only needed for DMX.
#  Set it up when present; MQTT-only setups can run without it.
if command -v olad >/dev/null 2>&1; then
    systemctl enable "olad@$TARGET_USER"
    systemctl start "olad@$TARGET_USER"

    # Default OLA to network DMX only (E1.31/sACN). Out of the box olad also loads
    #  its serial/USB device plugins, which grab the QSE's serial adapter
    #  (/dev/ttyUSB*) and conflict with this program. Wait for olad to generate its
    #  per-plugin configs, then disable every plugin and re-enable only e131.
    OLA_DIR="$TARGET_HOME/.ola"
    for _ in $(seq 1 15); do
        [ -f "$OLA_DIR/ola-e131.conf" ] && break
        sleep 1
    done
    if [ -f "$OLA_DIR/ola-e131.conf" ]; then
        systemctl stop "olad@$TARGET_USER"
        for f in "$OLA_DIR"/ola-*.conf; do
            # ola-server.conf / ola-universe.conf are not plugin configs.
            case "$(basename "$f")" in
                ola-server.conf|ola-universe.conf) continue ;;
            esac
            if grep -q '^enabled' "$f"; then
                sed -i '/^enabled[[:space:]]*=/c\enabled = false' "$f"
            else
                printf '\nenabled = false\n' >> "$f"
            fi
        done
        sed -i '/^enabled[[:space:]]*=/c\enabled = true' "$OLA_DIR/ola-e131.conf"
        chown -R "$TARGET_USER:" "$OLA_DIR"
        systemctl start "olad@$TARGET_USER"
        echo "Configured OLA for network DMX only (E1.31/sACN); serial/USB plugins disabled."

        # Patch the E1.31 input port to the DMX universe from config.yaml so olad
        #  actually receives sACN. Registering the universe from the client is not
        #  enough -- without a patched input port olad never joins the sACN
        #  multicast group (sACN universe == OLA universe number). Skipped when DMX
        #  is disabled in the config.
        UNIVERSE=$(python3 -c "import yaml; d=(yaml.safe_load(open('$CONFIG_FILE')) or {}).get('dmx',{}); print(d.get('universe','') if d.get('enabled', True) else '')" 2>/dev/null)
        if [ -n "$UNIVERSE" ] && command -v ola_patch >/dev/null 2>&1; then
            # Wait for olad's RPC + the E1.31 device, then resolve its device id.
            DEV=""
            for _ in $(seq 1 10); do
                DEV=$(ola_dev_info 2>/dev/null | sed -n 's/^Device \([0-9]*\): E1\.31.*/\1/p' | head -1)
                [ -n "$DEV" ] && break
                sleep 1
            done
            if [ -n "$DEV" ]; then
                ola_patch --device "$DEV" --port 0 --input --universe "$UNIVERSE" \
                    && echo "Patched E1.31 input port 0 -> universe $UNIVERSE (sACN reception)."
            else
                echo "WARNING: E1.31 device not found; patch universe $UNIVERSE to an E1.31 input port manually (olad web UI :9090)."
            fi
        fi
    else
        echo "WARNING: $OLA_DIR/ola-e131.conf not generated; configure OLA plugins manually (see README)."
    fi
else
    echo "WARNING: 'olad' not found. If you use DMX, run ./install-ola.sh first, then re-run this script."
fi

# Always enable so the service starts on boot. Whether we start it now depends on
#  whether the config is freshly installed (still has placeholder values).
systemctl enable "lutron-dmx-control@$TARGET_USER"

if [ "$NEW_CONFIG" -eq 1 ]; then
    # Fresh config: serial.device, MQTT password, etc. are still placeholders, so
    #  starting now would just crash-loop. Walk the user through editing + starting.
    SVC="lutron-dmx-control@$TARGET_USER"
    echo
    echo "============================================================"
    echo " Almost done. The service is enabled but NOT started yet."
    echo
    echo " 1. Edit your config (at minimum: serial.device,"
    echo "    qse.integration_id/zones, and -- if used -- the dmx.* and"
    echo "    mqtt.broker/username/password settings):"
    echo
    echo "      sudo nano $CONFIG_FILE"
    echo
    echo " 2. Start the service:"
    echo
    echo "      sudo systemctl start $SVC"
    echo
    echo " 3. Check it came up cleanly:"
    echo
    echo "      systemctl status $SVC"
    echo "      journalctl -u $SVC -f"
    echo "============================================================"
else
    # Existing config: (re)start to pick up the new script/units.
    systemctl restart "lutron-dmx-control@$TARGET_USER"
    echo "Restarted lutron-dmx-control@$TARGET_USER."
fi
