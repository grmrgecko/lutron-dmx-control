# lutron-dmx-control
#
# Copyright (c) 2019, Mr. Gecko's Media (James Coleman)
# All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products
#    derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
#    INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#    ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#    SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#    LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
#    STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
#    ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import argparse
import json
import logging
import os
import random
import signal
import socket
import sys
import threading
import time

import serial
import yaml

# OLA (DMX) and paho-mqtt are imported lazily in main()/_new_mqtt_client() so
#  the matching component can be disabled in config.yaml without the dependency
#  being installed. Serial control of the QSE is always required.

# Documentation
# This program is designed to use the Open Lighting Architecture (OLA) to receive a DMX signal
#  and translate to commands to control the 6 dimmable zones on the Lutron GRAFIK Eye QS Control panel
#  through the use of a QSE-CI-NWK-E. This program uses the serial port for reliability.


# === Configuration ===
# All runtime settings live in config.yaml (see config.example.yaml).
# These module-level names are populated by apply_config() at startup; the
#  values below are only fallbacks used if a key is omitted from the file.

# Search order for the config file when --config is not given:
#  1. $LUTRON_CONFIG (explicit override)
#  2. config.yaml next to this script
#  3. ~/.config/lutron-dmx-control/config.yaml ($XDG_CONFIG_HOME honored)
#  4. /etc/lutron-dmx-control/config.yaml
_XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
DEFAULT_CONFIG_PATHS = (
    os.environ.get("LUTRON_CONFIG"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
    os.path.join(_XDG_CONFIG_HOME, "lutron-dmx-control", "config.yaml"),
    "/etc/lutron-dmx-control/config.yaml",
)

# Serial / DMX / QSE.
QSE_NWK_DEVICE = "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller-if00-port0"
QSE_NWK_BAUD = 115200
QSE_ZONES = 6  # hardware constant for GRAFIK Eye QS (model dependent, max 24)
QSE_INTEGRATION_ID = 1
QSE_FADE = "00:00"  # fade sent with each zone-level command; OLA handles smoothing
DMX_ENABLED = True
DMX_UNIVERSE = 3
DMX_START_ADDRESS = 0

# QSE serial protocol constants (not user-configurable).
QSE_ACTION_ZONE_LEVEL = 14
QSE_BTN_DISABLE = 74
QSE_BTN_ENABLE = 75
QSE_BTN_ACTION = 3
# Derived from QSE_INTEGRATION_ID; rebuilt in apply_config().
QSE_DEVICE_PREFIX = f"~DEVICE,{QSE_INTEGRATION_ID}"
QSE_DISABLE_SIGNAL = f"~DEVICE,{QSE_INTEGRATION_ID},{QSE_BTN_DISABLE},{QSE_BTN_ACTION}"
QSE_ENABLE_SIGNAL = f"~DEVICE,{QSE_INTEGRATION_ID},{QSE_BTN_ENABLE},{QSE_BTN_ACTION}"

# Reliability tuning.
QSE_RX_TIMEOUT_SEC = 60
WATCHDOG_INTERVAL_SEC = 15
RECONNECT_BACKOFF_MIN_SEC = 1
RECONNECT_BACKOFF_MAX_SEC = 30
WRITE_INTERVAL_SEC = 0.1  # write loop tick (not user-configurable)
SEND_ALL_INTERVAL_SEC = 10

# How long after the last DMX universe update MQTT control stays locked out.
#  While a DMX signal is active, MQTT commands are ignored and the current zone
#  levels are mirrored back to MQTT instead.
DMX_LOCKOUT_SEC = 5

# Logging.
LOG_LEVEL = "INFO"

# MQTT.
MQTT_ENABLED = True
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC = "lutron/qse-nwk"
MQTT_TOPIC_SET = MQTT_TOPIC + "/set"
MQTT_CLIENT_ID = f"lutron-qse-nwk-{random.randint(0, 1000)}"
MQTT_USERNAME = "mqtt"
MQTT_PASSWORD = ""
MQTT_DISCOVERY = True
MQTT_DISCOVERY_PREFIX = "homeassistant"
MQTT_DEVICE_NAME = "Lutron QSE NWK"

# MQTT state values.
MQTT_LIGHT_ON = "ON"
MQTT_LIGHT_OFF = "OFF"

log = logging.getLogger("lutron-dmx-control")


def find_config_path(cli_path=None):
    """Return the first existing config path, or None."""
    for path in (cli_path,) + DEFAULT_CONFIG_PATHS:
        if path and os.path.isfile(path):
            return path
    return None


def load_config(cli_path=None):
    """Load and apply config.yaml. Exits with a clear message if not found."""
    path = find_config_path(cli_path)
    if path is None:
        searched = ", ".join(p for p in (cli_path,) + DEFAULT_CONFIG_PATHS if p)
        raise SystemExit(
            "No config file found (looked in: %s).\n"
            "Copy config.example.yaml to /etc/lutron-dmx-control/config.yaml "
            "(or pass --config PATH) and edit it." % searched
        )
    with open(path, "r") as fh:
        cfg = yaml.safe_load(fh) or {}
    apply_config(cfg)
    return path


def apply_config(cfg):
    """Populate module-level settings from a parsed YAML mapping."""
    global QSE_NWK_DEVICE, QSE_NWK_BAUD, QSE_ZONES, QSE_INTEGRATION_ID, QSE_FADE
    global DMX_ENABLED, DMX_UNIVERSE, DMX_START_ADDRESS
    global QSE_DEVICE_PREFIX, QSE_DISABLE_SIGNAL, QSE_ENABLE_SIGNAL
    global QSE_RX_TIMEOUT_SEC, WATCHDOG_INTERVAL_SEC
    global RECONNECT_BACKOFF_MIN_SEC, RECONNECT_BACKOFF_MAX_SEC, SEND_ALL_INTERVAL_SEC
    global DMX_LOCKOUT_SEC
    global LOG_LEVEL
    global MQTT_ENABLED, MQTT_BROKER, MQTT_PORT, MQTT_TOPIC, MQTT_TOPIC_SET
    global MQTT_CLIENT_ID, MQTT_USERNAME, MQTT_PASSWORD
    global MQTT_DISCOVERY, MQTT_DISCOVERY_PREFIX, MQTT_DEVICE_NAME
    global zoneValues, sentValues

    def section(name):
        s = cfg.get(name)
        return s if isinstance(s, dict) else {}

    serial_cfg = section("serial")
    QSE_NWK_DEVICE = serial_cfg.get("device", QSE_NWK_DEVICE)
    QSE_NWK_BAUD = int(serial_cfg.get("baud", QSE_NWK_BAUD))

    qse_cfg = section("qse")
    QSE_INTEGRATION_ID = int(qse_cfg.get("integration_id", QSE_INTEGRATION_ID))
    QSE_ZONES = int(qse_cfg.get("zones", QSE_ZONES))
    QSE_FADE = str(qse_cfg.get("fade", QSE_FADE))

    dmx_cfg = section("dmx")
    DMX_ENABLED = bool(dmx_cfg.get("enabled", DMX_ENABLED))
    DMX_UNIVERSE = int(dmx_cfg.get("universe", DMX_UNIVERSE))
    DMX_START_ADDRESS = int(dmx_cfg.get("start_address", DMX_START_ADDRESS))
    DMX_LOCKOUT_SEC = float(dmx_cfg.get("lockout_sec", DMX_LOCKOUT_SEC))

    rel = section("reliability")
    QSE_RX_TIMEOUT_SEC = int(rel.get("rx_timeout_sec", QSE_RX_TIMEOUT_SEC))
    WATCHDOG_INTERVAL_SEC = int(rel.get("watchdog_interval_sec", WATCHDOG_INTERVAL_SEC))
    RECONNECT_BACKOFF_MIN_SEC = int(
        rel.get("reconnect_backoff_min_sec", RECONNECT_BACKOFF_MIN_SEC))
    RECONNECT_BACKOFF_MAX_SEC = int(
        rel.get("reconnect_backoff_max_sec", RECONNECT_BACKOFF_MAX_SEC))
    SEND_ALL_INTERVAL_SEC = int(rel.get("send_all_interval_sec", SEND_ALL_INTERVAL_SEC))

    LOG_LEVEL = str(section("logging").get("level", LOG_LEVEL)).upper()

    mqtt = section("mqtt")
    MQTT_ENABLED = bool(mqtt.get("enabled", MQTT_ENABLED))
    MQTT_BROKER = mqtt.get("broker", MQTT_BROKER)
    MQTT_PORT = int(mqtt.get("port", MQTT_PORT))
    MQTT_TOPIC = mqtt.get("topic", MQTT_TOPIC)
    MQTT_TOPIC_SET = MQTT_TOPIC + "/set"
    MQTT_USERNAME = mqtt.get("username", MQTT_USERNAME)
    MQTT_PASSWORD = mqtt.get("password", MQTT_PASSWORD)
    client_id = mqtt.get("client_id")
    if client_id:
        MQTT_CLIENT_ID = client_id
    MQTT_DISCOVERY = bool(mqtt.get("discovery", MQTT_DISCOVERY))
    MQTT_DISCOVERY_PREFIX = mqtt.get("discovery_prefix", MQTT_DISCOVERY_PREFIX)
    MQTT_DEVICE_NAME = mqtt.get("device_name", MQTT_DEVICE_NAME)

    # Rebuild integration-ID-derived constants.
    QSE_DEVICE_PREFIX = f"~DEVICE,{QSE_INTEGRATION_ID}"
    QSE_DISABLE_SIGNAL = f"~DEVICE,{QSE_INTEGRATION_ID},{QSE_BTN_DISABLE},{QSE_BTN_ACTION}"
    QSE_ENABLE_SIGNAL = f"~DEVICE,{QSE_INTEGRATION_ID},{QSE_BTN_ENABLE},{QSE_BTN_ACTION}"

    # (Re)size the zone state to match the configured zone count.
    zoneValues = [0] * QSE_ZONES
    sentValues = [0] * QSE_ZONES


def configure_logging():
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
        stream=sys.stdout,
    )

# === Shared state ===
# Owned by the serial supervisor; readers may inspect .is_open without the lock,
#  but every write/close/replacement must hold serialLock.
serialSession = None
serialLock = threading.RLock()
# Protects shared zone/MQTT state.
dataLock = threading.RLock()

zoneValues = [0] * QSE_ZONES
sentValues = [0] * QSE_ZONES
sendAllDataThisTime = True
controlDisabled = False
lastDMXUniverseUpdate = 0

# Watchdog state.
lastQSEResponseTime = time.time()
lastQSEWriteTime = 0.0
# Rate-limit the #RESET,0 recovery: a wedged NWK floods ~Error,6, and we must not
#  answer every one with a reset.
lastQSEResetTime = 0.0
QSE_RESET_COOLDOWN_SEC = 10
reconnectRequested = threading.Event()
running = threading.Event()
running.set()

# MQTT state.
mqttLightState = MQTT_LIGHT_OFF
mqttLightBrightness = 0
mqttSentLightState = ""
mqttSentLightBrightness = 0
mqtt_conn = None

# OLA wrapper, set in main() so the signal handler can stop it.
ola_wrapper = None


# === sd_notify (no external dependency) ===
def sd_notify(message):
    """Send a notification to systemd via $NOTIFY_SOCKET, if set."""
    addr = os.environ.get('NOTIFY_SOCKET')
    if not addr:
        return
    if addr[0] == '@':
        addr = '\0' + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode("utf-8"))
    except OSError as e:
        log.warning("sd_notify(%r) failed: %s", message, e)


# === Serial supervision ===
def _close_serial_locked(ser):
    if ser is None:
        return
    try:
        ser.close()
    except Exception as e:
        log.debug("Ignoring error while closing serial: %s", e)


def ensure_serial_connected():
    """Block until serialSession is a usable, open port. Exponential backoff."""
    global serialSession, sendAllDataThisTime, lastQSEResponseTime
    backoff = RECONNECT_BACKOFF_MIN_SEC
    while running.is_set():
        with serialLock:
            if serialSession is not None and serialSession.is_open:
                reconnectRequested.clear()
                return
            _close_serial_locked(serialSession)
            serialSession = None
            try:
                log.info("Opening serial port %s @ %d", QSE_NWK_DEVICE, QSE_NWK_BAUD)
                # exclusive=True prevents another process (e.g. a stray OLA plugin)
                #  from grabbing the same /dev/ttyUSBx.
                serialSession = serial.Serial(
                    QSE_NWK_DEVICE, QSE_NWK_BAUD, timeout=2, exclusive=True
                )
                with dataLock:
                    sendAllDataThisTime = True
                lastQSEResponseTime = time.time()
                reconnectRequested.clear()
                log.info("Serial connection established.")
                return
            except (serial.SerialException, OSError) as e:
                log.error("Serial open failed: %s; retry in %ds", e, backoff)
                serialSession = None
        # Sleep outside the lock so other threads can observe the closed state.
        time.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SEC)


def request_reconnect(reason):
    """Close the current port and wake the supervisor to reopen it."""
    log.warning("Reconnect requested: %s", reason)
    with serialLock:
        _close_serial_locked(serialSession)
    reconnectRequested.set()


def serial_write(payload):
    """Write bytes to the serial port under lock. Returns True on success."""
    global lastQSEWriteTime
    with serialLock:
        ser = serialSession
        if ser is None or not ser.is_open:
            return False
        try:
            ser.write(payload)
            lastQSEWriteTime = time.time()
            return True
        except (serial.SerialException, OSError) as e:
            log.error("Serial write failed: %s", e)
    request_reconnect("write error")
    return False


def serial_supervisor():
    """Single owner of the reconnect path. Other threads request and wait."""
    while running.is_set():
        ensure_serial_connected()
        # Sync our view of zone levels with the panel after every (re)connect.
        qse_query_zone_levels()
        # Block until something requests a reconnect.
        reconnectRequested.wait()


# === QSE protocol ===
# Lutron integration commands are terminated with <CR><LF> per the protocol doc.
QSE_TERMINATOR = "\r\n"


def qse_send_zone_value(zone, value):
    pct = round((value / 255.00) * 100, 2)
    command = "#DEVICE,%d,%d,%d,%.2f,%s" % (
        QSE_INTEGRATION_ID, zone, QSE_ACTION_ZONE_LEVEL, pct, QSE_FADE
    )
    log.debug("TX %s", command)
    return serial_write((command + QSE_TERMINATOR).encode("utf-8"))


def qse_query_zone_levels():
    """Ask the QSE for each zone's current level so our view matches reality
    after a (re)connect. Responses are handled in qse_read()."""
    for zone in range(1, QSE_ZONES + 1):
        query = "?DEVICE,%d,%d,%d" % (
            QSE_INTEGRATION_ID, zone, QSE_ACTION_ZONE_LEVEL
        )
        log.debug("TX %s", query)
        serial_write((query + QSE_TERMINATOR).encode("utf-8"))


# ~ERROR,<n> codes from the Lutron integration protocol (doc 040249).
QSE_ERROR_DESCRIPTIONS = {
    "1": "parameter count mismatch",
    "2": "object does not exist (check integration ID)",
    "3": "invalid action number",
    "4": "parameter data out of range",
    "5": "parameter data malformed",
    "6": "unsupported command",
}


def _handle_qse_error(line):
    """Log a ~Error response and, for the known bad-state error, reset the NWK."""
    global lastQSEResetTime
    code = line.split(",", 1)[1].strip() if "," in line else ""
    desc = QSE_ERROR_DESCRIPTIONS.get(code, "unknown error")
    # Error 6 ("unsupported command") is also the symptom of the NWK lockup that
    #  only #RESET,0 clears, so we recover from it; other errors are logged only.
    if code == "6":
        now = time.time()
        # A wedged NWK errors every command; reset at most once per cooldown.
        if now - lastQSEResetTime >= QSE_RESET_COOLDOWN_SEC:
            lastQSEResetTime = now
            log.warning("QSE NWK returned %s (%s); sending #RESET,0", line, desc)
            serial_write(("#RESET,0" + QSE_TERMINATOR).encode("utf-8"))
    else:
        log.warning("QSE NWK returned %s (%s)", line, desc)


def dmx_universe_update(data):
    global lastDMXUniverseUpdate
    with dataLock:
        for zone in range(QSE_ZONES):
            zoneValues[zone] = data[DMX_START_ADDRESS + zone]
        lastDMXUniverseUpdate = time.time()


def qse_write_zone_values():
    """Periodically push any changed zone values out the serial port."""
    global sendAllDataThisTime
    while running.is_set():
        try:
            if controlDisabled:
                time.sleep(0.5)
                continue
            ser = serialSession
            if ser is None or not ser.is_open:
                time.sleep(0.2)
                continue

            with dataLock:
                thisZoneValues = list(zoneValues)
                resendAll = sendAllDataThisTime
                sendAllDataThisTime = False

            for zone in range(QSE_ZONES):
                if thisZoneValues[zone] == sentValues[zone] and not resendAll:
                    continue
                if not qse_send_zone_value(zone + 1, thisZoneValues[zone]):
                    # Write failed: mark for retry next cycle and let the
                    #  supervisor reopen the port.
                    with dataLock:
                        sendAllDataThisTime = True
                    break
                sentValues[zone] = thisZoneValues[zone]

            time.sleep(WRITE_INTERVAL_SEC)
        except Exception:
            log.exception("qse_write_zone_values loop error")
            time.sleep(1)


def qse_read():
    """Read responses from the QSE NWK and dispatch state updates."""
    global controlDisabled, sendAllDataThisTime, mqttLightBrightness, mqttLightState
    global lastQSEResponseTime
    while running.is_set():
        try:
            ser = serialSession
            if ser is None or not ser.is_open:
                time.sleep(0.2)
                continue
            try:
                # Use pyserial's native line reader, not ser.readline(): pyserial
                #  3.x has no readline() of its own, so the inherited io.IOBase one
                #  calls read() with a non-int size and raises TypeError. read_until
                #  reads up to the LF (or the 2s port timeout) via read(1).
                raw = ser.read_until(b"\n")
            except (serial.SerialException, OSError) as e:
                log.error("Serial read failed: %s", e)
                request_reconnect("read error")
                time.sleep(0.5)
                continue
            except TypeError:
                # The supervisor closed the port mid-read. pyserial's close() nulls
                #  the fd before clearing is_open, so a read can slip past the
                #  is_open check above and hit os.read(None, ...). Harmless: the
                #  supervisor is already reconnecting, so just re-check and retry.
                time.sleep(0.1)
                continue
            if not raw:
                continue

            line = raw.decode("utf-8", errors="replace").replace("QSE>", "").rstrip()
            if not line:
                continue

            lastQSEResponseTime = time.time()
            log.debug("RX %s", line)

            # The NWK sends "~Error,<n>" (mixed case), so match case-insensitively.
            if line.upper().startswith("~ERROR"):
                _handle_qse_error(line)
            elif line == QSE_DISABLE_SIGNAL:
                log.info("Received disable signal.")
                with dataLock:
                    controlDisabled = True
            elif line == QSE_ENABLE_SIGNAL:
                log.info("Received enable signal.")
                with dataLock:
                    controlDisabled = False
                    sendAllDataThisTime = True
            elif line.startswith(QSE_DEVICE_PREFIX):
                # Zone-level feedback: ~DEVICE,<intid>,<zone>,14,<level%>.
                fields = line.split(",")
                if (len(fields) >= 5
                        and fields[1] == str(QSE_INTEGRATION_ID)
                        and fields[3] == str(QSE_ACTION_ZONE_LEVEL)):
                    try:
                        zone = int(fields[2])
                        brightness = int(round((float(fields[4]) / 100.0) * 255))
                    except ValueError:
                        continue
                    # The MQTT light tracks zone 1 as the aggregate state.
                    if zone == 1:
                        with dataLock:
                            mqttLightBrightness = brightness
                            if mqttLightBrightness == 0:
                                if controlDisabled:
                                    controlDisabled = False
                                mqttLightState = MQTT_LIGHT_OFF
                            else:
                                mqttLightState = MQTT_LIGHT_ON
                            mqtt_publish_state()
        except Exception:
            log.exception("qse_read loop error")
            time.sleep(1)


def qse_reset_send_all():
    """Force a periodic full resend so the QSE NWK can't drift out of sync."""
    global sendAllDataThisTime
    while running.is_set():
        time.sleep(SEND_ALL_INTERVAL_SEC)
        # Skip resends when the link looks unhealthy. The watchdog will trigger
        #  a reconnect, and ensure_serial_connected() already sets
        #  sendAllDataThisTime on success — no need to spam the bus in the meantime.
        if (time.time() - lastQSEResponseTime) > QSE_RX_TIMEOUT_SEC:
            continue
        with dataLock:
            sendAllDataThisTime = True


def qse_watchdog():
    """Detect a stale serial link and feed the systemd watchdog.

    The QSE only replies to a #DEVICE write when it actually changes a level, so a
    quiet RX while we write (e.g. periodic resends of unchanged values) does NOT
    mean the link is dead. Before reconnecting we actively probe with a ?DEVICE
    query, which always gets a ~DEVICE reply on a healthy link; only if that probe
    also goes unanswered do we treat the link as stale and reconnect."""
    probed = False
    while running.is_set():
        now = time.time()
        # Only meaningful if we've been writing recently.
        wrote_recently = (now - lastQSEWriteTime) < QSE_RX_TIMEOUT_SEC
        rx_stale = (now - lastQSEResponseTime) > QSE_RX_TIMEOUT_SEC
        if wrote_recently and rx_stale and not reconnectRequested.is_set():
            if not probed:
                # Actively probe; the reply (if any) lands in qse_read and
                #  refreshes lastQSEResponseTime before the next pass.
                log.info("QSE RX stale for %.0fs; probing with a zone query",
                         now - lastQSEResponseTime)
                qse_query_zone_levels()
                probed = True
            else:
                request_reconnect(
                    "no QSE RX for %.0fs (query probe unanswered)"
                    % (now - lastQSEResponseTime)
                )
                probed = False
        else:
            probed = False

        sd_notify("WATCHDOG=1")
        time.sleep(WATCHDOG_INTERVAL_SEC)


# === MQTT ===
def mqtt_publish_state():
    global mqttSentLightState, mqttSentLightBrightness
    if mqtt_conn is None:
        return
    if (mqttLightState == mqttSentLightState
            and mqttLightBrightness == mqttSentLightBrightness):
        return
    mqttSentLightState = mqttLightState
    mqttSentLightBrightness = mqttLightBrightness
    msg = json.dumps({"brightness": mqttLightBrightness, "state": mqttLightState})
    try:
        result = mqtt_conn.publish(MQTT_TOPIC, msg)
        if result[0] != 0:
            log.warning("Failed to publish to MQTT topic %s", MQTT_TOPIC)
        else:
            log.debug("Published %s to %s", msg, MQTT_TOPIC)
    except Exception:
        log.exception("MQTT publish error")


def mqtt_on_message(client, userdata, msg):
    global mqttLightState, mqttLightBrightness, mqttSentLightState, mqttSentLightBrightness
    if msg.topic != MQTT_TOPIC_SET:
        log.warning("Unknown MQTT topic %s payload %r", msg.topic, msg.payload)
        return
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        log.warning("Bad MQTT payload on %s: %s", msg.topic, e)
        return
    log.debug("MQTT RX %s", data)

    with dataLock:
        if "brightness" in data:
            mqttLightBrightness = data["brightness"]
        if "state" in data:
            if mqttLightState != data["state"]:
                mqttLightState = data["state"]
                # Turning on with brightness 0 -> default to ~50%.
                if mqttLightState == MQTT_LIGHT_ON and mqttLightBrightness == 0:
                    mqttLightBrightness = 127

        durationSinceLastDMXUniverseUpdate = time.time() - lastDMXUniverseUpdate
        if durationSinceLastDMXUniverseUpdate > DMX_LOCKOUT_SEC:
            target = mqttLightBrightness if mqttLightState == MQTT_LIGHT_ON else 0
            for zone in range(QSE_ZONES):
                zoneValues[zone] = target
        else:
            # DMX is in control; mirror current zone 1 back out to MQTT and
            #  force the next publish to actually go out.
            mqttLightBrightness = zoneValues[0]
            mqttLightState = MQTT_LIGHT_OFF if mqttLightBrightness == 0 else MQTT_LIGHT_ON
            mqttSentLightState = ""
            mqttSentLightBrightness = 0

        mqtt_publish_state()


def mqtt_slug():
    """Stable identifier derived from the base topic, for HA unique IDs."""
    return "".join(c if c.isalnum() else "_" for c in MQTT_TOPIC).strip("_")


def mqtt_publish_discovery(client):
    """Publish a Home Assistant MQTT discovery config so the light appears
    automatically. Retained so HA picks it up whenever it (re)connects."""
    slug = mqtt_slug()
    topic = "%s/light/%s/config" % (MQTT_DISCOVERY_PREFIX, slug)
    payload = json.dumps({
        "schema": "json",
        "name": MQTT_DEVICE_NAME,
        "unique_id": slug,
        "state_topic": MQTT_TOPIC,
        "command_topic": MQTT_TOPIC_SET,
        "brightness": True,
        "supported_color_modes": ["brightness"],
        "device": {
            "identifiers": [slug],
            "name": MQTT_DEVICE_NAME,
            "manufacturer": "Lutron",
            "model": "GRAFIK Eye QS (QSE-CI-NWK-E)",
        },
    })
    try:
        client.publish(topic, payload, retain=True)
        log.info("Published Home Assistant discovery to %s", topic)
    except Exception:
        log.exception("MQTT discovery publish error")


def mqtt_on_connect(client, userdata, flags, reason_code, properties=None):
    # paho-mqtt 1.x passes an int rc; 2.x (v2 callbacks) passes a ReasonCode and
    #  an extra properties arg. Normalize both to a success/failure check.
    failed = (reason_code.is_failure if hasattr(reason_code, "is_failure")
              else reason_code != 0)
    if not failed:
        log.info("Connected to MQTT broker.")
        client.subscribe(MQTT_TOPIC_SET)
        if MQTT_DISCOVERY:
            mqtt_publish_discovery(client)
        mqtt_publish_state()
    else:
        log.error("MQTT connect failed, rc=%s", reason_code)


def _new_mqtt_client():
    """Construct an MQTT client compatible with paho-mqtt 1.x and 2.x.

    paho-mqtt 2.0 made the callback API version a required argument. We use the
    v2 callbacks (avoiding the deprecation warning); mqtt_on_connect accepts both
    signatures and mqtt_on_message is unchanged between versions. On paho-mqtt 1.x
    (no CallbackAPIVersion) we fall back to the legacy constructor."""
    from paho.mqtt import client as mqtt_client
    try:
        from paho.mqtt.enums import CallbackAPIVersion
        return mqtt_client.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
        )
    except ImportError:
        return mqtt_client.Client(MQTT_CLIENT_ID)


def mqtt_loop():
    global mqtt_conn
    backoff = 1
    while running.is_set():
        try:
            mqtt_conn = _new_mqtt_client()
            if MQTT_USERNAME:
                mqtt_conn.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            mqtt_conn.on_connect = mqtt_on_connect
            mqtt_conn.on_message = mqtt_on_message
            mqtt_conn.connect(MQTT_BROKER, MQTT_PORT)
            backoff = 1
            mqtt_conn.loop_forever()
        except Exception:
            log.exception("MQTT loop error; reconnecting in %ds", backoff)
        finally:
            mqtt_conn = None
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# === Startup ===
def start_thread(target, name):
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


def _handle_shutdown(signum, frame):
    log.info("Received signal %d, shutting down", signum)
    sd_notify("STOPPING=1")
    running.clear()
    reconnectRequested.set()
    if ola_wrapper is not None:
        try:
            ola_wrapper.Stop()
        except Exception:
            log.exception("Error stopping OLA wrapper")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Lutron GRAFIK Eye QS DMX bridge")
    parser.add_argument(
        "-c", "--config",
        help="Path to config.yaml (default: $LUTRON_CONFIG, "
             "/etc/lutron-dmx-control/config.yaml, or ./config.yaml)",
    )
    return parser.parse_args(argv)


def main():
    global ola_wrapper

    args = parse_args()
    config_path = load_config(args.config)
    configure_logging()
    log.info("Lutron DMX Control starting (PID %d)", os.getpid())
    log.info("Loaded configuration from %s", config_path)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Block until the first connect succeeds (or we're shutting down).
    ensure_serial_connected()

    if not DMX_ENABLED and not MQTT_ENABLED:
        log.warning(
            "Both DMX and MQTT are disabled; nothing will drive the zones.")
    log.info("Components: DMX=%s, MQTT=%s",
             "on" if DMX_ENABLED else "off", "on" if MQTT_ENABLED else "off")

    start_thread(serial_supervisor, "serial-sup")
    start_thread(qse_read, "qse-read")
    start_thread(qse_write_zone_values, "qse-write")
    start_thread(qse_reset_send_all, "qse-resend")
    start_thread(qse_watchdog, "qse-watchdog")
    if MQTT_ENABLED:
        start_thread(mqtt_loop, "mqtt")

    sd_notify("READY=1")
    sd_notify("STATUS=Running")

    try:
        if DMX_ENABLED:
            # OLA owns the main thread; its callback feeds zone values.
            from ola.ClientWrapper import ClientWrapper
            ola_wrapper = ClientWrapper()
            client = ola_wrapper.Client()
            client.RegisterUniverse(
                DMX_UNIVERSE, client.REGISTER, dmx_universe_update)
            ola_wrapper.Run()
        else:
            # No DMX: keep the main thread alive until a signal clears running.
            while running.is_set():
                time.sleep(1)
    finally:
        running.clear()
        reconnectRequested.set()
        sd_notify("STOPPING=1")


if __name__ == "__main__":
    main()
