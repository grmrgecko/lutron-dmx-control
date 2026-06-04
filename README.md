# Lutron GRAFIK Eye QS — DMX & Home Assistant control

This lets you control a **Lutron GRAFIK Eye QS** lighting unit from two places it
normally can't be reached from:

- **A lighting board / theatrical software** (so the GRAFIK Eye's zones can be run
  as part of a larger light show), and
- **Home Assistant** (so you can control and automate the lights like any other
  smart light in your house).

It runs as a small always-on program on a **Raspberry Pi** that you wire to the
GRAFIK Eye. The Pi listens for commands and translates them into the GRAFIK Eye's
own language. The two control methods are independent and both optional — use one,
the other, or both.

> **New to the jargon?** Here's the short version:
> - **GRAFIK Eye QS** — the Lutron lighting control unit this drives. It has up to
>   6 dimmable lighting *zones*.
> - **QSE-CI-NWK-E** — the add-on module on the GRAFIK Eye that gives it a serial
>   port we can talk to.
> - **DMX / sACN (E1.31)** — the standard "language" lighting boards and stage
>   software use. sACN is just DMX sent over your normal network instead of a cable.
> - **OLA (Open Lighting Architecture)** — free software that receives the network
>   DMX and hands it to this program. Only needed if you want lighting-board control.
> - **MQTT** — the messaging system Home Assistant uses to talk to devices. Only
>   needed if you want Home Assistant control.

## How it's set up here

I run this on a Raspberry Pi (a Pi Zero is plenty) on **Raspberry Pi OS / Raspbian
13 (Trixie)**. Talking to the GRAFIK Eye over serial is always on. DMX and MQTT are
separate optional pieces you can turn on or off in the config file.

One catch with DMX: OLA is no longer pre-packaged for recent Raspberry Pi OS
releases, so it has to be *built from source* (compiled on the Pi). Don't worry —
the included `install-ola.sh` script does the entire build for you. It follows the
official [OLA build guide](https://www.openlighting.org/ola/linuxinstall/), so you
don't have to; that link is there only if you're curious what the script is doing.

# What you'll need

- A **Raspberry Pi** running Raspberry Pi OS. A Pi Zero is enough for serial-only
  control; for sACN or Home Assistant you need networking, so use a Pi Zero **W**
  (Wi-Fi) or any networked model. These instructions assume **Raspberry Pi OS /
  Raspbian 13 (Trixie)**.
- A **USB-to-serial adapter** wired to the QSE-CI-NWK-E's serial terminals. The
  example config uses a common Prolific PL2303-style adapter; any 3.3 V / RS-232
  adapter that matches your wiring will do.
- A **GRAFIK Eye QS** with the **QSE-CI-NWK-E** network/serial interface module.
- **For lighting-board control:** a lighting console or software that sends
  **sACN/E1.31** over your network.
- **For Home Assistant:** a running **MQTT broker** (the Docker setup near the end
  of this guide includes one).

# The big picture

Once your Pi is up and running, setup is just a few steps:

1. **Get the Pi ready and download the code** (step 0).
2. **Install OLA** (step 1) — *only if you want lighting-board/DMX control.* This is
   the slow part (~1–2 hours on a Pi Zero, because it compiles from source).
3. **Install the control program** (step 2) and **fill in the config** (step 3).

Everything below is typed into a terminal on the Pi. If you've never used one, the
commands are copy-paste — just swap in your own values where noted.

# Installation

## 0. Get the Pi ready and download the code

If you're starting from a blank SD card, flash **Raspberry Pi OS** with the
[Raspberry Pi Imager](https://www.raspberrypi.com/software/). Before you write the
card, open the imager's settings (the **gear** / **"Edit settings"** button) and:

- **Set a username and password**, and
- **Enable SSH** (so you can connect to the Pi from another computer).

**Write down the username you choose** — you'll use it all over this guide, shown as
`<user>`. Modern Raspberry Pi OS no longer uses `pi` as the default username, so use
whatever you set here.

Boot the Pi, then open a terminal on it — either directly with a keyboard and
monitor, or from another computer over SSH:

```bash
ssh <user>@<pi-address>
```

Now install git and download this project:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/GRMrGecko/lutron-dmx-control.git
cd lutron-dmx-control
```

Every command from here on is run from inside this `lutron-dmx-control` folder.

> Throughout this guide, replace `<user>` with the username you created above. For
> example, `lutron-dmx-control@<user>` becomes `lutron-dmx-control@john` if your
> username is `john`.

## 1. Install OLA (only if you want DMX / lighting-board control)

**Not using a lighting board?** Skip this step entirely. Set `dmx.enabled: false` in
the config (step 3) and OLA never has to be installed.

Otherwise, build and install OLA (the background service plus the Python add-on this
program uses):

```bash
bash ./install-ola.sh
```

This installs the build tools, downloads OLA at the tested `0.10.9` version, and
compiles and installs it. On a single-core Pi Zero the compile takes roughly **1–2
hours** — that's normal. The script temporarily adds extra memory (swap) on
low-memory boards so the build doesn't run out of memory partway through.

If you ever need to, you can override the version or build location with the
`OLA_VERSION=` / `BUILD_DIR=` environment variables.

## 2. Install the control program

Run the installer:

```bash
./install.sh
```

It installs the Python requirements, the control program itself, the config file,
and the background services that keep everything running and start it on boot.

On a **first install** the service is set to start on boot but is **not started
yet** — the config still has placeholder values you need to fill in. The installer
prints the exact "edit, then start" steps for you (covered in step 3). If you run
`install.sh` again later (to update), it restarts the service to pick up the new
version.

## 3. Fill in the config

Open the config file in a text editor (`nano` is beginner-friendly):

```bash
sudo nano /etc/lutron-dmx-control/config.yaml
```

This file was created from `config.example.yaml` and is heavily commented, so each
setting explains itself. The important ones:

- **`serial.device`** — which USB-serial adapter to use. Find yours by running
  `ls -lah /dev/serial/by-id/` and copying the matching path.
- **`qse.integration_id` and `qse.zones`** — set these to match your GRAFIK Eye
  (the integration ID is assigned in Lutron's programming; zones is how many
  dimmable zones your model has).
- **`dmx.*`** — your DMX layout (`universe`, `start_address`). `dmx.lockout_sec`
  (default `5`) is how long an active DMX signal keeps Home Assistant from changing
  the lights, so the lighting board stays in charge during a show. Set
  `dmx.enabled: false` to run without DMX/OLA.
- **`mqtt.*`** — your MQTT broker address and `username`/`password` for Home
  Assistant. Set `mqtt.enabled: false` to run without MQTT (then `paho-mqtt` isn't
  needed).

Save and exit (`Ctrl+O`, `Enter`, then `Ctrl+X` in nano).

> **Where the config lives:** the program looks for it in this order — `--config
> PATH`, then `$LUTRON_CONFIG`, then a `config.yaml` next to the program, then
> `~/.config/lutron-dmx-control/config.yaml`, then
> `/etc/lutron-dmx-control/config.yaml` (where the installer puts it). Because it
> holds your MQTT password, it's locked down (`chmod 600`) and kept out of git; only
> the `config.example.yaml` template is committed.

Now start the service (use `restart` instead of `start` if it's already running, e.g.
after editing the config). **Remember to replace `<user>` with your username:**

```bash
sudo systemctl start lutron-dmx-control@<user>
```

Check that it started cleanly (press `Ctrl+C` to stop watching the log):

```bash
journalctl -u lutron-dmx-control@<user> -f
```

# OLA / DMX configuration

By default, `install.sh` sets OLA up for **network DMX only (E1.31/sACN)**: it turns
off every OLA plugin except `e131`. This matters because OLA's serial/USB plugins
would otherwise grab your USB-serial adapter (`/dev/ttyUSB*`) out from under this
program. The plugin settings live in `~/.ola/` if you want to change them later.

To turn on a different or extra plugin, stop OLA, change its setting, and start it
again (replace `<user>` with your username):

```bash
sudo systemctl stop olad@<user>
sed -i '/^enabled\s*=/c\enabled = true' ~/.ola/ola-artnet.conf   # example: also accept Art-Net
sudo systemctl start olad@<user>
```

## Receiving sACN (patching the universe)

For OLA to actually *receive* sACN, an **E1.31 input port has to be "patched" to your
OLA universe**. The OLA universe number is the same as the sACN universe (e.g.
universe `3` = multicast address `239.255.0.3`). Just registering the universe isn't
enough — without a patched input port, OLA never joins the network group that carries
the DMX data.

`install.sh` does this for you automatically, patching the E1.31 input port to the
`dmx.universe` from your `config.yaml`. To do it (or change it) by hand:

```bash
# Find the E1.31 device id, then patch input port 0 to your universe (here 3):
ola_dev_info
ola_patch --device 1 --port 0 --input --universe 3
# Confirm it joined the multicast group on your network interface
#  (eth0 = wired, wlan0 = Wi-Fi on a Pi Zero W):
ip maddr show dev eth0 | grep 239.255.0.3
curl -s http://localhost:9090/get_dmx?u=3      # confirm DMX values are arriving
```

You can also do this from OLA's web page — browse to the Pi's IP address on port
`9090`. The patch is saved in `~/.ola/` and survives restarts and reboots.

> **Tip:** on the console or software sending sACN, a "changes only" / "send on
> change" option means it only transmits when levels change. Prefer a continuous
> stream so OLA has data to work with immediately after a restart.

# Home Assistant & MQTT (Docker)

I run Home Assistant and the Mosquitto MQTT broker in Docker using `docker compose`.
Here's a minimal `compose.yaml`:

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

`network_mode: host` lets Home Assistant find the broker and lets this control
program publish to it at `127.0.0.1:1883`. Start it all with `docker compose up -d`.

## Mosquitto config

Mosquitto (the MQTT broker) needs a config file and a password in the mounted
`./mosquitto` folder.

`./mosquitto/mosquitto.conf`:

```
per_listener_settings true
allow_zero_length_clientid true
listener 1883 0.0.0.0
allow_anonymous false
password_file /mosquitto/config/pwfile
acl_file /mosquitto/config/aclfile
```

`./mosquitto/aclfile` (gives the `mqtt` user full access):

```
user mqtt
topic readwrite #
```

Create the password file — use the **same `mqtt` user and password you put in
`config.yaml`**:

```bash
docker compose run --rm mqtt mosquitto_passwd -c -b /mosquitto/config/pwfile mqtt 'your-password'
docker compose restart mqtt
```

## Home Assistant integration

In Home Assistant, add the **MQTT** integration (**Settings → Devices & Services**)
and point it at the broker: host `127.0.0.1`, port `1883`, and the `mqtt`
user/password.

With `mqtt.discovery: true` (the default in `config.yaml`), the light is announced to
Home Assistant automatically and shows up on its own — no YAML editing required. If
you'd rather add it manually, set `mqtt.discovery: false` and add this to your Home
Assistant config:

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

# Recommended: hardware watchdog

The Raspberry Pi has a built-in hardware watchdog that can automatically reboot the
Pi if it ever locks up. It's worth enabling for an always-on device like this.

Add this to `/boot/firmware/config.txt` (or `/boot/config.txt` on older images) under
the `[all]` section:

```
watchdog=on
```

Then uncomment `RuntimeWatchdogSec` in `/etc/systemd/system.conf` and set it:

```
RuntimeWatchdogSec=10s
```

Reboot to apply.
