"""Data update coordinator for cloud-polled Marstek devices."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud import MarstekCloudClient, MarstekCloudDevice, MarstekCloudError
from .const import CONF_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)

# One config entry represents an account and owns a coordinator per selected
# device, keyed by device id.
type MarstekCloudConfigEntry = ConfigEntry[dict[str, "MarstekCloudCoordinator"]]


class MarstekCloudCoordinator(DataUpdateCoordinator[MarstekCloudDevice]):
    """Polls one cloud device through a shared account client."""

    config_entry: MarstekCloudConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MarstekCloudConfigEntry,
        client: MarstekCloudClient,
        devid: str,
    ) -> None:
        """Initialize the coordinator for one cloud device."""
        self.client = client
        self.devid = devid
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"Marstek cloud {devid}",
            update_interval=SCAN_INTERVAL,
        )

    @override
    async def _async_update_data(self) -> MarstekCloudDevice:
        """Fetch this device from the account's cloud device list."""
        try:
            devices = await self.client.async_get_devices()
        except MarstekCloudError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cloud_update_failed",
                translation_placeholders={"devid": self.devid},
            ) from err

        self._persist_token()

        device = devices.get(self.devid)
        if device is None:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cloud_device_missing",
                translation_placeholders={"devid": self.devid},
            )
        return device

    def _persist_token(self) -> None:
        """Save a refreshed token to the entry so restarts reuse it."""
        token = self.client.token
        if token and token != self.config_entry.data.get(CONF_TOKEN):
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_TOKEN: token},
            )
