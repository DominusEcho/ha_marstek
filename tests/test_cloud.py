"""Tests for the Marstek cloud account mode."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.marstek.cloud import MarstekCloudAuthError, MarstekCloudDevice
from custom_components.marstek.const import (
    CONF_DEVICES,
    CONF_MAILBOX,
    CONF_PASSWORD_HASH,
    CONF_TOKEN,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

MAILBOX = "someone@example.com"
INVERTER_ID = "0c587b17d5db"
BATTERY_ID = "3601115053573639310d2172"

# A getDeviceList response: a solar inverter, a battery and a CT meter.
DEVICE_LIST: list[dict[str, Any]] = [
    {
        "devid": INVERTER_ID,
        "name": "Solar Inverter 1",
        "type": "HMI-1",
        "mac": INVERTER_ID,
        "version": "118",
        "soc": 0,
        "charge": 0,
        "discharge": 0,
        "load": 0,
        "pv": 273,
        "grid": 256,
        "battery_num": 0,
        "report_time": 1791806953,
    },
    {
        "devid": BATTERY_ID,
        "name": "Venus E Mini Casa",
        "type": "VNSEMINI-0",
        "mac": "ccc837b39e61",
        "version": "301",
        "soc": 271,
        "charge": 0,
        "discharge": 855,
        "load": 0,
        "pv": 0,
        "grid": -909,
        "battery_num": 1,
        "report_time": 1791806998,
    },
]


def _devices() -> dict[str, MarstekCloudDevice]:
    """Return the parsed device list keyed by device id."""
    return {raw["devid"]: MarstekCloudDevice.from_json(raw) for raw in DEVICE_LIST}


@pytest.fixture
def cloud_client() -> Generator[AsyncMock]:
    """Patch the cloud client's network calls with canned responses."""
    with (
        patch(
            "custom_components.marstek.cloud.MarstekCloudClient.async_get_devices",
            new=AsyncMock(return_value=_devices()),
        ) as get_devices,
        patch(
            "custom_components.marstek.cloud.MarstekCloudClient.async_login",
            new=AsyncMock(return_value="token"),
        ),
    ):
        yield get_devices


def _cloud_entry(hass: HomeAssistant, devices: list[str]) -> MockConfigEntry:
    """Add a cloud account config entry selecting the given devices."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_MAILBOX: MAILBOX,
            CONF_PASSWORD_HASH: "hash",
            CONF_TOKEN: "token",
            CONF_DEVICES: devices,
        },
        unique_id=f"cloud_{MAILBOX}",
    )
    entry.add_to_hass(hass)
    return entry


async def test_cloud_sensors(hass: HomeAssistant, cloud_client: AsyncMock) -> None:
    """Selected cloud devices get the right sensors, with soc scaled."""
    entry = _cloud_entry(hass, [INVERTER_ID, BATTERY_ID])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ids = {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }

    def state(unique_id: str) -> str:
        return hass.states.get(ids[unique_id]).state

    # Inverter: solar power and derived solar energy, but no battery sensors.
    assert state(f"cloud_{INVERTER_ID}_cloud_pv_power") == "273"
    assert state(f"cloud_{INVERTER_ID}_cloud_grid_power") == "256"
    assert f"cloud_{INVERTER_ID}_cloud_solar_energy" in ids
    assert f"cloud_{INVERTER_ID}_cloud_battery_soc" not in ids

    # Battery: soc reported in tenths of a percent, 271 -> 27.1; no solar.
    assert state(f"cloud_{BATTERY_ID}_cloud_battery_soc") == "27.1"
    assert state(f"cloud_{BATTERY_ID}_cloud_discharge_power") == "855"
    assert f"cloud_{BATTERY_ID}_cloud_pv_power" not in ids
    assert f"cloud_{BATTERY_ID}_cloud_solar_energy" not in ids


async def test_cloud_flow(hass: HomeAssistant, cloud_client: AsyncMock) -> None:
    """The cloud menu option logs in and creates an account entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MAILBOX: MAILBOX, CONF_PASSWORD: "secret"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cloud_select"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICES: [INVERTER_ID]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAILBOX] == MAILBOX
    assert result["data"][CONF_DEVICES] == [INVERTER_ID]
    # The password is hashed, never stored in plain text.
    assert result["data"][CONF_PASSWORD_HASH] != "secret"
    assert CONF_PASSWORD not in result["data"]


async def test_cloud_flow_invalid_auth(hass: HomeAssistant) -> None:
    """A rejected login shows an error instead of creating an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    with patch(
        "custom_components.marstek.cloud.MarstekCloudClient.async_login",
        new=AsyncMock(side_effect=MarstekCloudAuthError),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_MAILBOX: MAILBOX, CONF_PASSWORD: "wrong"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cloud_reconfigure_adds_device(
    hass: HomeAssistant, cloud_client: AsyncMock
) -> None:
    """Reconfiguring a cloud entry adds a device without re-entering credentials."""
    entry = _cloud_entry(hass, [INVERTER_ID])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cloud_select"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICES: [INVERTER_ID, BATTERY_ID]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_DEVICES] == [INVERTER_ID, BATTERY_ID]
    assert entry.data[CONF_PASSWORD_HASH] == "hash"

    # The reload set up the newly selected battery alongside the inverter.
    unique_ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert f"cloud_{INVERTER_ID}_cloud_pv_power" in unique_ids
    assert f"cloud_{BATTERY_ID}_cloud_battery_soc" in unique_ids


async def test_cloud_reconfigure_rejected_login(hass: HomeAssistant) -> None:
    """Reconfiguring aborts when the saved credentials no longer work."""
    entry = _cloud_entry(hass, [INVERTER_ID])
    with patch(
        "custom_components.marstek.cloud.MarstekCloudClient.async_get_devices",
        new=AsyncMock(side_effect=MarstekCloudAuthError),
    ):
        result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"
    assert entry.data[CONF_DEVICES] == [INVERTER_ID]
