"""Data update coordinator for Marstek devices."""

from dataclasses import replace
from datetime import datetime, timedelta
import logging
from typing import override

from aiomarstek import MarstekDeviceInfo, MarstekDeviceStatus, MarstekUDPClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_MAC
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .cloud import MarstekCloudClient, MarstekCloudDevice, MarstekCloudError
from .const import (
    CLOUD_CT_TYPE_PREFIX,
    CONF_BLE_MAC,
    CONF_CLOUD_DEVID,
    CONF_DEVICE_TYPE,
    CONF_VERSION,
    CONF_WIFI_MAC,
    CONF_WIFI_NAME,
    DATA_SOURCE_CLOUD,
    DATA_SOURCE_LOCAL,
    DATA_SOURCE_NONE,
    DOMAIN,
    ERROR_INVALID_DATA,
    ERROR_NETWORK,
    ERROR_NO_RESPONSE,
    ERROR_NONE,
    SUPPORTED_DEVICE_TYPES,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)
# Missed polls in a row before entities become unavailable; the ones before
# keep showing the last values, so a single dropped packet goes unnoticed.
UNAVAILABLE_POLLS = 2
# Missed polls in a row before a repair issue is raised. The device's local
# API can hang until it is switched off and on again in the Marstek app.
UNREACHABLE_POLLS = 10

# Energy counters the device resets at midnight, despite their "total" names.
# Otherwise they only grow, so a lower reading is a device glitch and would
# show up in the Energy dashboard as a meter reset.
DAILY_ENERGY_KEYS = (
    "total_pv_energy",
    "total_grid_output_energy",
    "total_grid_input_energy",
    "total_load_energy",
)
# A lower reading this small is the midnight reset, even when the device's
# clock and Home Assistant's disagree about when midnight is.
DAILY_RESET_MAX_WH = 100
# A battery level this high cannot drop to 0 % within one poll.
SOC_GLITCH_FLOOR = 10
# Cloud readings that stayed the same this long are too stale to stand in for
# the device, for example because it is offline altogether. The cloud's
# report_time is no guide on its own: it can lag the live values by hours.
CLOUD_MAX_AGE = timedelta(minutes=15)

# Readings the cloud does not report; they are unknown, rather than stale,
# while the values come from the cloud.
CLOUD_UNKNOWN_KEYS = (
    "grid_phase_a_power",
    "grid_phase_b_power",
    "grid_phase_c_power",
    *(
        f"pv{pv_input}_{metric}"
        for pv_input in range(1, 5)
        for metric in ("power", "voltage", "current", "state")
    ),
)


type MarstekConfigEntry = ConfigEntry[MarstekDataUpdateCoordinator]

MARSTEK_UDP_CLIENT: HassKey[MarstekUDPClient] = HassKey(DOMAIN)


class MarstekDataUpdateCoordinator(DataUpdateCoordinator[MarstekDeviceStatus]):
    """Per-device data update coordinator."""

    config_entry: MarstekConfigEntry
    device_info: MarstekDeviceInfo
    # When the device last answered a status request; data from a poll the
    # device did not answer is the previous data, so this shows staleness.
    last_response: datetime | None = None
    failed_polls: int = 0
    error_state: str = ERROR_NONE
    data_source: str = DATA_SOURCE_NONE
    # Whether the latest data was just read, locally or from the cloud, rather
    # than held over from before a missed poll.
    fresh: bool = False
    # Why and when the cloud backup was last not used, kept until it is used
    # again, and when the device last reported to the cloud, for diagnostics.
    cloud_error: str | None = None
    cloud_error_time: datetime | None = None
    cloud_report_time: datetime | None = None
    # The reason already warned about during the current outage.
    _cloud_warned: str | None = None
    # The cloud readings last seen during the current outage, and when they
    # last changed.
    _cloud_readings: tuple[object, ...] | None = None
    _cloud_changed: datetime | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MarstekConfigEntry,
        udp_client: MarstekUDPClient,
        cloud_client: MarstekCloudClient | None = None,
    ) -> None:
        """Initialize the coordinator, with a cloud client to fall back on."""
        self.device_ip = config_entry.data[CONF_HOST]
        self.udp_client = udp_client
        self.cloud_client = cloud_client
        self.cloud_devid: str | None = config_entry.options.get(CONF_CLOUD_DEVID)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"Marstek {self.device_ip}",
            update_interval=SCAN_INTERVAL,
        )
        _LOGGER.debug(
            "Device %s polling coordinator started, interval: %ss",
            self.device_ip,
            SCAN_INTERVAL.total_seconds(),
        )

    @override
    async def _async_setup(self) -> None:
        """Validate device availability and cache its device information."""
        try:
            device_info = await self.udp_client.get_device_info(self.device_ip)
        except (TimeoutError, OSError, TypeError) as err:
            if self.has_cloud_backup and (stored := self._stored_device_info()):
                # Start from the cloud; the first poll the device answers
                # refreshes the firmware version.
                _LOGGER.warning(
                    "Device %s is not answering, starting with cloud data",
                    self.device_ip,
                )
                self.device_info = stored
                return
            if self.has_cloud_backup:
                _LOGGER.warning(
                    "Device %s is not answering and its saved device information "
                    "does not match the entry, so it cannot start from the cloud",
                    self.device_ip,
                )
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="device_connection_failed",
                translation_placeholders={"host": self.device_ip},
            ) from err

        if not device_info.stable_id:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="missing_stable_id",
                translation_placeholders={"host": self.device_ip},
            )

        if device_info.device_type not in SUPPORTED_DEVICE_TYPES:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="unsupported_device",
                translation_placeholders={"device_type": device_info.device_type},
            )

        self.device_info = device_info

    def _stored_device_info(self) -> MarstekDeviceInfo | None:
        """Return the device information saved when the entry was created."""
        data = self.config_entry.data
        if CONF_DEVICE_TYPE not in data:
            return None
        info = MarstekDeviceInfo.from_response(
            {
                "device": data[CONF_DEVICE_TYPE],
                "ver": data.get(CONF_VERSION),
                "wifi_name": data.get(CONF_WIFI_NAME),
                "ip": self.device_ip,
                "wifi_mac": data.get(CONF_WIFI_MAC),
                "ble_mac": data.get(CONF_BLE_MAC),
                "mac": data.get(CONF_MAC),
            }
        )
        return info if info.stable_id == self.config_entry.unique_id else None

    @property
    def has_cloud_backup(self) -> bool:
        """Return whether the cloud is used while the device does not answer."""
        return self.cloud_client is not None and self.cloud_devid is not None

    @property
    def issue_id(self) -> str:
        """Return the repair issue id for an unreachable device."""
        return f"open_api_unreachable_{self.config_entry.entry_id}"

    @override
    async def _async_update_data(self) -> MarstekDeviceStatus:
        """Fetch device data from the Marstek client library."""
        _LOGGER.debug("Start polling device: %s", self.device_ip)
        previous = self.data
        answered_before = self.udp_client.get_last_response_time(self.device_ip)

        try:
            status = await self.udp_client.get_device_status(
                self.device_ip,
                previous_data=previous,
            )
        except (TimeoutError, OSError, TypeError) as err:
            return await self._async_missed_poll(previous, err)

        last_response = self.udp_client.get_last_response_time(self.device_ip)
        if last_response is None or last_response == answered_before:
            # The library returns the previous data when nothing answered.
            return await self._async_missed_poll(
                previous, TimeoutError(f"No response from {self.device_ip}")
            )

        if self.failed_polls >= UNREACHABLE_POLLS:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        if self.failed_polls:
            _LOGGER.info(
                "Device %s answered again after %s missed polls",
                self.device_ip,
                self.failed_polls,
            )
            await self._async_refresh_firmware()
        new_day = (
            self.last_response is not None
            and dt_util.as_local(last_response).date()
            != dt_util.as_local(self.last_response).date()
        )
        self.failed_polls = 0
        self.error_state = ERROR_NONE
        self.data_source = DATA_SOURCE_LOCAL
        self.fresh = True
        self._cloud_warned = None
        self._cloud_readings = None
        self.last_response = last_response
        return hold_glitches(previous, status, new_day=new_day)

    async def _async_missed_poll(
        self, previous: MarstekDeviceStatus | None, err: Exception
    ) -> MarstekDeviceStatus:
        """Keep the last data through a missed poll, then use the cloud or fail."""
        self.failed_polls += 1
        self.fresh = False
        self.error_state = (
            ERROR_NO_RESPONSE
            if isinstance(err, TimeoutError)
            else ERROR_NETWORK
            if isinstance(err, OSError)
            else ERROR_INVALID_DATA
        )
        if self.failed_polls == UNREACHABLE_POLLS:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="open_api_unreachable",
                translation_placeholders={"host": self.device_ip},
            )
        if previous is not None and self.failed_polls < UNAVAILABLE_POLLS:
            _LOGGER.debug(
                "Device %s missed poll %s: %s", self.device_ip, self.failed_polls, err
            )
            return previous
        if self.has_cloud_backup and (
            cloud := await self._async_cloud_status(previous)
        ):
            self.data_source = DATA_SOURCE_CLOUD
            self.fresh = True
            return cloud
        self.data_source = DATA_SOURCE_NONE
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="device_update_failed",
            translation_placeholders={"host": self.device_ip},
        ) from err

    async def _async_cloud_status(
        self, previous: MarstekDeviceStatus | None
    ) -> MarstekDeviceStatus | None:
        """Return the status built from the cloud, or None if it has none fresh."""
        assert self.cloud_client is not None
        try:
            devices = await self.cloud_client.async_get_devices()
        except MarstekCloudError as err:
            return self._cloud_unusable(str(err))
        device = devices.get(str(self.cloud_devid))
        if device is None:
            return self._cloud_unusable(
                f"the cloud does not list device {self.cloud_devid}"
            )
        ct = cloud_ct_meter(devices)
        now = dt_util.utcnow()
        if device.report_time is not None:
            self.cloud_report_time = dt_util.utc_from_timestamp(device.report_time)
        # The cloud values count as live while they keep changing, or while
        # the device reported recently. The first values of an outage are
        # given the benefit of the doubt.
        readings = (device, ct)
        if readings != self._cloud_readings:
            self._cloud_readings = readings
            self._cloud_changed = now
        assert self._cloud_changed is not None
        reported_recently = (
            self.cloud_report_time is not None
            and now - self.cloud_report_time <= CLOUD_MAX_AGE
        )
        if not reported_recently and now - self._cloud_changed > CLOUD_MAX_AGE:
            return self._cloud_unusable(
                f"its cloud values have not changed since {self._cloud_changed}"
            )
        self.cloud_error = None
        if self.data_source != DATA_SOURCE_CLOUD:
            _LOGGER.info(
                "Device %s is not answering, using cloud data until it does",
                self.device_ip,
            )
        return status_from_cloud(
            previous or MarstekDeviceStatus(device_ip=self.device_ip), device, ct
        )

    def _cloud_unusable(self, reason: str) -> None:
        """Record why the cloud backup cannot be used, warning once per reason."""
        if reason != self._cloud_warned:
            _LOGGER.warning(
                "Device %s is not answering and its cloud backup cannot be used: %s",
                self.device_ip,
                reason,
            )
            self._cloud_warned = reason
        self.cloud_error = reason
        self.cloud_error_time = dt_util.utcnow()

    async def _async_refresh_firmware(self) -> None:
        """Re-read the firmware version, as a firmware update causes an outage."""
        try:
            info = await self.udp_client.get_device_info(self.device_ip)
        except TimeoutError, OSError, TypeError:
            return
        if info.version == self.device_info.version:
            return
        _LOGGER.info(
            "Device %s firmware changed from %s to %s",
            self.device_ip,
            self.device_info.version,
            info.version,
        )
        self.device_info = replace(self.device_info, version=info.version)
        registry = dr.async_get(self.hass)
        if device := registry.async_get_device_by_identifier(
            (DOMAIN, self.device_info.stable_id), self.config_entry.entry_id
        ):
            registry.async_update_device(device.id, sw_version=str(info.version))


def cloud_ct_meter(
    devices: dict[str, MarstekCloudDevice],
) -> MarstekCloudDevice | None:
    """Return the account's CT meter, if it has exactly one."""
    meters = [
        device
        for device in devices.values()
        if device.device_type.startswith(CLOUD_CT_TYPE_PREFIX)
    ]
    return meters[0] if len(meters) == 1 else None


def status_from_cloud(
    previous: MarstekDeviceStatus,
    device: MarstekCloudDevice,
    ct: MarstekCloudDevice | None = None,
) -> MarstekDeviceStatus:
    """Return the previous status updated with what the cloud reports.

    The grid power is the CT meter's, positive when importing, like the local
    one; the battery's own grid value is its AC output, so it is not used.
    The daily energy counters and the device mode keep their last local
    values; readings the cloud does not have become unknown.
    """
    net_power = (
        None
        if device.charge_power is None and device.discharge_power is None
        else (device.discharge_power or 0) - (device.charge_power or 0)
    )
    battery_status = (
        None
        if net_power is None
        else "selling"
        if net_power > 0
        else "charging"
        if net_power < 0
        else "idle"
    )
    # The cloud has no stored energy, but it scales with the battery level.
    battery_energy = None
    if (
        previous.battery_energy
        and previous.battery_soc
        and device.battery_soc is not None
    ):
        battery_energy = round(
            previous.battery_energy * device.battery_soc / previous.battery_soc
        )
    return replace(
        previous,
        battery_soc=device.battery_soc,
        battery_power=abs(net_power) if net_power is not None else None,
        battery_status=battery_status,
        battery_energy=battery_energy,
        grid_power=ct.grid_power if ct is not None else None,
        **dict.fromkeys(CLOUD_UNKNOWN_KEYS),
    )


def hold_glitches(
    previous: MarstekDeviceStatus | None,
    current: MarstekDeviceStatus,
    *,
    new_day: bool = False,
) -> MarstekDeviceStatus:
    """Keep previous values where the device reports implausible readings.

    new_day is set when the previous reading was from an earlier day, so the
    daily energy counters may have been reset in between.
    """
    if previous is None:
        return current
    held: dict[str, object] = {}
    for key in DAILY_ENERGY_KEYS:
        old, new = getattr(previous, key), getattr(current, key)
        if old is None:
            continue
        if new is None or (new < old and not new_day and new > DAILY_RESET_MAX_WH):
            held[key] = old
    soc_glitch = (previous.battery_soc or 0) >= SOC_GLITCH_FLOOR
    if current.battery_soc == 0 and soc_glitch:
        held["battery_soc"] = previous.battery_soc
    if current.battery_energy == 0 and previous.battery_energy and soc_glitch:
        held["battery_energy"] = previous.battery_energy
    if held:
        _LOGGER.debug(
            "Device %s implausible readings held: %s", current.device_ip, held
        )
        return replace(current, **held)
    return current
