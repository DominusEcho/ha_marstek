"""The Marstek integration."""

import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.hass_dict import HassKey

from .cloud import MarstekCloudClient
from .cloud_coordinator import MarstekCloudConfigEntry, MarstekCloudCoordinator
from .const import (
    CONF_DEVICES,
    CONF_MAILBOX,
    CONF_PASSWORD_HASH,
    CONF_TOKEN,
    DOMAIN,
)
from .coordinator import (
    MARSTEK_UDP_CLIENT,
    MarstekConfigEntry,
    MarstekDataUpdateCoordinator,
)
from .helpers import async_create_udp_client

# Shared cloud clients, one per account, so the devices of an account share a
# token and collapse their polls into a single cloud request.
CLOUD_CLIENTS: HassKey[dict[str, MarstekCloudClient]] = HassKey(f"{DOMAIN}_cloud")

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Marstek integration.

    The shared UDP client is created once and lives for the lifetime of
    the integration. If creation fails, integration setup fails and the
    config entries stay not loaded; reloading the integration retries.
    """
    udp_client = await async_create_udp_client(hass)
    hass.data[MARSTEK_UDP_CLIENT] = udp_client

    async def close_udp_client(_: Event) -> None:
        """Clean up the shared UDP client when Home Assistant stops."""
        await udp_client.async_cleanup()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_udp_client)

    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: MarstekConfigEntry | MarstekCloudConfigEntry
) -> bool:
    """Set up Marstek from a config entry."""
    if CONF_MAILBOX in entry.data:
        await _async_setup_cloud_entry(hass, entry)
    else:
        cloud_client = (
            _async_cloud_client(
                hass, entry.options[CONF_MAILBOX], entry.options[CONF_PASSWORD_HASH]
            )
            if CONF_MAILBOX in entry.options
            else None
        )
        coordinator = MarstekDataUpdateCoordinator(
            hass, entry, hass.data[MARSTEK_UDP_CLIENT], cloud_client
        )
        entry.runtime_data = coordinator
        await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_setup_cloud_entry(
    hass: HomeAssistant, entry: MarstekCloudConfigEntry
) -> None:
    """Set up a cloud account entry with a coordinator per selected device."""
    client = _async_cloud_client(
        hass,
        entry.data[CONF_MAILBOX],
        entry.data[CONF_PASSWORD_HASH],
        entry.data.get(CONF_TOKEN),
    )
    coordinators: dict[str, MarstekCloudCoordinator] = {}
    for devid in entry.data[CONF_DEVICES]:
        coordinator = MarstekCloudCoordinator(hass, entry, client, devid)
        await coordinator.async_config_entry_first_refresh()
        coordinators[devid] = coordinator
    entry.runtime_data = coordinators


def _async_cloud_client(
    hass: HomeAssistant, mailbox: str, password_hash: str, token: str | None = None
) -> MarstekCloudClient:
    """Return the shared cloud client for an account, creating it once.

    Cloud account entries and devices using the cloud as a backup share it.
    """
    clients = hass.data.setdefault(CLOUD_CLIENTS, {})
    client = clients.get(mailbox)
    # A different password hash means the password was changed since.
    if client is None or client.password_hash != password_hash:
        clients[mailbox] = MarstekCloudClient(
            async_get_clientsession(hass), mailbox, password_hash, token
        )
    return clients[mailbox]


async def async_unload_entry(hass: HomeAssistant, entry: MarstekConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
