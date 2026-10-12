# Marstek Home Assistant Integration

The Marstek integration communicates with supported Marstek energy storage devices locally over UDP and exposes their status as Home Assistant sensors.

> **About this fork:** This is a fork of [MarstekEnergy/ha_marstek](https://github.com/MarstekEnergy/ha_marstek) that can be installed through HACS. It uses a [fork of the `aiomarstek` library](https://github.com/DominusEcho/aiomarstek) that fixes a memory leak: the upstream 0.1.7 release keeps every UDP response it receives, growing Home Assistant's memory use by roughly 15 MB per device per day. The library is pinned to an exact commit. The fork also adds battery charge/discharge energy and CT meter grid sensors, and only creates PV sensors on devices with PV inputs.

## Requirements

- Home Assistant Core 2026.9.0 or newer. The integration uses the `probatio` validation library and Python 3.14 syntax, both of which are only available in Home Assistant 2026.9 and later. Older versions fail to load the integration.
- Keep Home Assistant OS, or the container image you run, up to date so it ships a supported Home Assistant Core version.
- Home Assistant and the Marstek device must be on the same local network.
- Open API must be enabled on the Marstek device.
- UDP port `30000` must be available between Home Assistant and the device. Discovery uses a local-network UDP broadcast.
- When running Home Assistant in Docker, the container needs host networking for discovery to work. Otherwise, add the device manually by IP address.

> **Important:** Venus E2.0 is not supported. Do not use this integration with Venus E2.0, as it may disconnect the device from CT003.

## Supported Devices

The integration currently supports these device types. A device may report either its model name or one of the protocol identifiers below:

| Device | Reported device type |
| --- | --- |
| Venus A | `VNSA-0`, `VenusA`, `Venus A` |
| Venus D | `VNSD-0`, `VenusD`, `Venus D` |
| Venus E 3.0 | `VNSE3-0`, `VenusE 3.0`, `Venus E 3.0` |
| Venus Mini | `VNSEM-0` |

Support depends on the device firmware exposing the Marstek Open API. Other device types are rejected during setup until they are explicitly supported.

## Available Entities

After setup, the integration creates sensors for:

- Battery level, power, and status.
- Device operating mode (`Auto`, `AI`, `Manual`, `Passive`, or `UPS`).
- Lifetime discharged and charged battery energy, for the battery in the Energy dashboard.
- Grid power, in total and per phase, when a CT meter is connected to the device.
- Grid import and export energy, integrated from the CT meter's grid power, for the grid in the Energy dashboard. They only count while Home Assistant is running.
- Last response: when the device last answered, to spot a device that stopped responding. Sensor values keep their last reading while the device does not answer.
- Venus A and Venus D only: lifetime PV energy, and for each of the four PV inputs, power, voltage, current, and state (`Standby` or `Working`).

### Energy dashboard

- **Grid connections**: use *Grid import energy* for energy consumed from the grid and *Grid export energy* for energy returned to the grid.
- **Home battery storage**: use *Lifetime discharged energy* for energy discharged from the battery and *Lifetime charged energy* for energy charged into the battery. Some firmware reports the charged energy as 0.

The device is polled locally every 60 seconds. No cloud account or external service is required.

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS**, select the menu in the top-right corner, and choose **Custom repositories**.
2. Enter `https://github.com/DominusEcho/ha_marstek`, select the **Integration** type, and select **Add**.
3. Search HACS for **Marstek**, open it, and select **Download**.
4. Restart Home Assistant.

HACS notifies you when a new release of this repository is published.

### Manual

1. Clone the repository and switch to the `main` branch:

   ```bash
   git clone https://github.com/DominusEcho/ha_marstek.git
   cd ha_marstek
   git checkout main
   ```

2. Copy the integration into Home Assistant's `custom_components` directory. Replace `/path/to/homeassistant/config` with your Home Assistant configuration directory:

   ```bash
   mkdir -p /path/to/homeassistant/config/custom_components/marstek
   cp -r custom_components/marstek/. /path/to/homeassistant/config/custom_components/marstek/
   ```

3. Restart Home Assistant.

To update a manual installation, pull the latest version, copy the integration over the installed version, and restart Home Assistant:

```bash
cd /path/to/ha_marstek
git pull origin main
mkdir -p /path/to/homeassistant/config/custom_components/marstek
cp -r custom_components/marstek/. /path/to/homeassistant/config/custom_components/marstek/
```

The trailing `/.` copies the contents of the integration folder, which overwrites the installed files instead of nesting a second copy inside them.

### Adding a device

After installing and restarting, open **Settings** > **Devices & services**, select **Add integration**, search for **Marstek**, and choose one of the following setup methods:

- **Search for devices on the local network** to use UDP discovery.
- **Enter device IP address** for manual setup.

The Marstek device must be powered on, reachable from Home Assistant, and have Open API enabled before setup.

On first start after installing, Home Assistant downloads the pinned `aiomarstek` library from GitHub, so it needs internet access once. If you previously used the upstream integration, the fixed library replaces the PyPI 0.1.7 release automatically.

## Troubleshooting

If **Marstek does not appear, or setup fails immediately with no connection attempt**, check the Home Assistant log:

- An error mentioning `version key in the manifest file` means you are running an older copy of this integration. Replace the `custom_components/marstek` folder with the latest version from this repository and restart.
- An error mentioning `Requirements for marstek not found` means Home Assistant could not download the `aiomarstek` library from GitHub. Check that Home Assistant has internet access and restart.
- An error mentioning `probatio` or `SyntaxError` means Home Assistant is too old. Upgrade Home Assistant Core to 2026.9.0 or newer, restart, and try again.

If no device is discovered:

- Confirm that Open API is enabled on the device.
- Confirm that Home Assistant and the device are on the same network segment.
- Allow UDP traffic on port `30000` in the network and host firewalls.
- When Home Assistant runs in Docker, use host networking, or skip discovery and add the device manually by IP address.
- Make sure client isolation (guest networks, IoT VLANs) is not blocking broadcasts between Home Assistant and the device.
- Try manual setup with the device's IP address.
- Check the Home Assistant log for connection or unsupported-device errors.

If the device is detected but setup fails, verify that its reported device type is listed above and that the device firmware supports the Open API used by this integration.
