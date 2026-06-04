This project controls the GRAFIK Eye QS control panel via a QSE-CI-NWK-E over its
serial interface. It uses the [OLA](https://www.openlighting.org/) project to take
a DMX device or a network DMX protocol (e.g. sACN/E1.31) and drive the 6 available
zones. It also speaks MQTT for Home Assistant control, with MQTT auto discovery so
the light appears automatically.

I run this on a Raspberry Pi (a Pi Zero works) on **Raspberry Pi OS / Raspbian 13
(Trixie)**. OLA is no longer packaged for recent Debian/Raspbian releases, so it is
built from source at the `0.10.9` release tag. Just run the included `install-ola.sh`
(see below) — it handles the whole build for you. The script was written based on the
[OLA build guide](https://www.openlighting.org/ola/linuxinstall/), so you don't need
to follow that guide yourself; it's linked only as a reference for what the script does.

DMX and MQTT are independent, optional components. Serial control of the QSE is
always active; you can run with DMX only, MQTT only, or both.

# What you'll need

- A **Raspberry Pi** running Raspberry Pi OS (a Pi Zero is enough; a Pi Zero **W**
  or any model with networking is needed for sACN/MQTT). These instructions assume
  **Raspberry Pi OS / Raspbian 13 (Trixie)**.
- A **USB-to-serial adapter** wired to the QSE-CI-NWK-E's serial terminals (the
  config example uses a Prolific PL2303-style adapter; any 3.3 V / RS-232 adapter
  that matches your wiring works).
- A **GRAFIK Eye QS** with a QSE-CI-NWK-E network/serial interface.
- For DMX: a lighting console or software sending **sACN/E1.31** on your network.
- For MQTT / Home Assistant: a running **MQTT broker** (the Docker setup below
  includes one).

# Overview

The setup is three steps once the Pi is ready:

1. **Prepare the Pi and get the code** (step 0) — flash the OS, get a terminal, clone this repo.
2. **Install OLA** (step 1) — only if you use DMX. This is the slow part (~1–2 h on a Pi Zero).
3. **Install the control service** (step 2) and **configure it** (step 3).

# Installation

## 0. Prepare the Pi and get the code

If you're starting from scratch, flash **Raspberry Pi OS** with the
[Raspberry Pi Imager](https://www.raspberrypi.com/software/). In the imager's
settings (the gear / "Edit settings"), **set a username and password and enable
SSH** — remember the username you choose; you'll use it everywhere below as
`<user>`. Modern Raspberry Pi OS no longer defaults to the `pi` user, so don't
assume it; use whatever name you set here.

Boot the Pi, then open a terminal on it (directly, or over SSH:
`ssh <user>@<pi-address>`). Install git and download this project:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/GRMrGecko/lutron-dmx-control.git
cd lutron-dmx-control
```

All the commands below are run from inside this `lutron-dmx-control` directory.

> Throughout this guide, replace `<user>` with the username you created above. If
> that username is **not** `pi`, you must also pass it to the installer
> (`TARGET_USER=<user>`, shown in step 2) and substitute it in every
> `systemctl`/`journalctl` command (e.g. `lutron-dmx-control@<user>`, not `@pi`).

## 1. Install OLA (only if using DMX)

If you set `dmx.enabled: false`, skip this step — OLA does not need to be installed.

Otherwise build and install OLA (the daemon plus the Python client bindings the
control script uses). On a single-core Pi Zero this takes roughly 1–2 hours; the
script adds temporary swap on low-memory boards so the compile does not run out of
memory.

```bash
bash ./install-ola.sh
```

This installs the build dependencies, clones OLA at the `0.10.9` tag, and builds and
installs `olad` plus the `ola.ClientWrapper` Python module. Override the version or
build directory with `OLA_VERSION=` / `BUILD_DIR=` if needed.

## 2. Install the control service

`install.sh` installs the Python dependencies, the control script, the config file
and the `olad@<user>` / `lutron-dmx-control@<user>` systemd services. By default it
installs for the `pi` user; pass `TARGET_USER=<name>` for a different user.

```bash
sudo bash ./install.sh
# or, for a non-pi user:
sudo TARGET_USER=james bash ./install.sh
```

The service is **enabled** (starts on boot) but, on a first install, is **not
started immediately** — the freshly installed config still has placeholder values.
The installer prints the exact edit-then-start steps; see step 3 below. On a re-run
with an existing config it restarts the service to pick up the new version.

> Note: the systemd unit runs `/home/<user>/lutron-dmx-control.py`, so `<user>`'s
> home must be `/home/<user>`. If it lives elsewhere, the installer warns you to
> adjust `ExecStart` in `lutron-dmx-control@.service`.

## 3. Configure

Edit `/etc/lutron-dmx-control/config.yaml` (installed from `config.example.yaml`) and set:

- `serial.device` — your serial device (use `ls -lah /dev/serial/by-id/`).
- `qse.integration_id` and `qse.zones` — to match your GRAFIK Eye unit.
- `dmx.enabled` / `dmx.universe` / `dmx.start_address` — for your DMX layout.
  `dmx.lockout_sec` (default `5`) sets how long an active DMX signal locks out MQTT
  control. Set `dmx.enabled: false` to run without OLA/DMX.
- `mqtt.broker`, `mqtt.username`, `mqtt.password` — if using MQTT. Set
  `mqtt.enabled: false` to run without MQTT/Home Assistant; `paho-mqtt` is then not
  required.

The config is searched for at `--config PATH`, then `$LUTRON_CONFIG`, then `config.yaml`
next to the script, then `~/.config/lutron-dmx-control/config.yaml`, then
`/etc/lutron-dmx-control/config.yaml`. It holds the MQTT password, so it is `chmod 600`
and excluded from git (`config.yaml` in `.gitignore`); only `config.example.yaml` is
committed.

Then start (first install) or restart (after edits) the service:
`sudo systemctl start lutron-dmx-control@pi` (use `restart` if it is already running).
Check it came up cleanly with `journalctl -u lutron-dmx-control@pi -f`.

# OLA / DMX configuration

`install.sh` configures OLA for **network DMX only (E1.31/sACN)** by default: it
disables every OLA plugin except `e131`. This matters because olad's serial/USB
device plugins (e.g. `usbserial`) otherwise auto-probe and grab the QSE's serial
adapter (`/dev/ttyUSB*`), conflicting with this program. The plugin configs live in
`~/.ola/` if you want to change this later.

To enable a different/extra plugin, stop olad, flip its config, and restart:

```bash
sudo systemctl stop olad@pi
sed -i '/^enabled\s*=/c\enabled = true' ~/.ola/ola-artnet.conf   # example: also accept Art-Net
sudo systemctl start olad@pi
```

## Receiving sACN (patching the universe)

For olad to actually receive sACN, an **E1.31 input port must be patched to your OLA
universe** — the OLA universe number is the sACN universe (e.g. universe `3` =
multicast `239.255.0.3`). Registering the universe from the client is not enough;
without a patched input port olad never joins the sACN multicast group.

`install.sh` does this automatically: it patches the E1.31 input port to the
`dmx.universe` from your `config.yaml`. To do it (or change it) by hand:

```bash
# Find the E1.31 device id, then patch input port 0 to your universe (here 3):
ola_dev_info
ola_patch --device 1 --port 0 --input --universe 3
# Confirm the multicast join on your active interface (eth0 wired, wlan0 on a Pi Zero W):
ip maddr show dev eth0 | grep 239.255.0.3
curl -s http://localhost:9090/get_dmx?u=3      # confirm DMX values are arriving
```

You can also patch from the olad web UI at the Pi's IP, port `9090`. The patch is
saved in `~/.ola/` and survives restarts/reboots.

> Note: on the console/desktop sending sACN, a "changes only" / "send on change"
> option means it only transmits when levels change. Prefer a continuous stream so
> olad has data immediately after a restart.

# Home Assistant & MQTT (Docker)

I run Home Assistant and the Mosquitto MQTT broker in Docker via `docker compose`.
A minimal `compose.yaml`:

```yaml
services:
  homeassistant:
    container_name: home-assistant
    image: homeassistant/home-assistant:stable
    volumes:
      - ./hass:/config
    environment:
      - TZ=America/Chicago
    restart: always
    network_mode: host
  mqtt:
    container_name: mqtt
    image: eclipse-mosquitto
    volumes:
      - ./mosquitto:/mosquitto/config
    restart: always
    network_mode: host
```

`network_mode: host` lets Home Assistant discover the broker and the control script
publish to it on `127.0.0.1:1883`. Bring it up with `docker compose up -d`.

## Mosquitto config

Mosquitto needs a config and a password in the mounted `./mosquitto` directory.
`./mosquitto/mosquitto.conf`:

```
per_listener_settings true
allow_zero_length_clientid true
listener 1883 0.0.0.0
allow_anonymous false
password_file /mosquitto/config/pwfile
acl_file /mosquitto/config/aclfile
```

`./mosquitto/aclfile` (grant the `mqtt` user full access):

```
user mqtt
topic readwrite #
```

Create the password file (use the same `mqtt` user/password you put in
`config.yaml`):

```bash
docker compose run --rm mqtt mosquitto_passwd -c -b /mosquitto/config/pwfile mqtt 'your-password'
docker compose restart mqtt
```

## Home Assistant integration

In Home Assistant, add the **MQTT** integration (Settings → Devices & Services) and
point it at the broker (host `127.0.0.1`, port `1883`, the `mqtt` user/password).

With `mqtt.discovery: true` (the default in `config.yaml`), the light is published via
Home Assistant MQTT discovery and appears automatically — no YAML needed. To disable
discovery, set `mqtt.discovery: false` and add the light manually:

```yaml
light:
  - platform: mqtt
    schema: json
    name: lutron_qse_nwk
    state_topic: "lutron/qse-nwk"
    command_topic: "lutron/qse-nwk/set"
    brightness: true
    color_mode: true
    supported_color_modes: ["brightness"]
```

# Recommended: watchdog

Enable the hardware watchdog on the Pi to auto-reboot on a system crash.

Add to `/boot/firmware/config.txt` (or `/boot/config.txt` on older images) under the
`[all]` section:

```
watchdog=on
```

Uncomment `RuntimeWatchdogSec` in `/etc/systemd/system.conf` and set it:

```
RuntimeWatchdogSec=10s
```

Reboot to apply.
