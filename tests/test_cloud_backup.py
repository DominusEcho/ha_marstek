"""Tests for the cloud backup of a locally polled device."""

from collections.abc import Generator
from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch

from aiomarstek import MarstekDeviceInfo
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.marstek.cloud import (
    MarstekCloudClient,
    MarstekCloudDevice,
    MarstekCloudError,
)
from custom_components.marstek.const import (
    CONF_BLE_MAC,
    CONF_CLOUD_DEVID,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_MAILBOX,
    CONF_PASSWORD_HASH,
    CONF_VERSION,
    CONF_WIFI_MAC,
    DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PASSWORD, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import DEVICE_RESPONSE, HOST, FakeClient, setup_entry
from .test_cloud import BATTERY_ID, DEVICE_LIST, MAILBOX
from .test_sensor import poll

INFO = MarstekDeviceInfo.from_response(DEVICE_RESPONSE)
BACKUP_OPTIONS = {
    CONF_MAILBOX: MAILBOX,
    CONF_PASSWORD_HASH: "hash",
    CONF_CLOUD_DEVID: BATTERY_ID,
}


CT_ID = "f8d811e0ef14"
# The account's CT002 meter, listed as its own cloud device.
CT_METER = {
    "devid": CT_ID,
    "name": "TC Casa",
    "type": "HME-4",
    "mac": CT_ID,
    "version": "124",
    "soc": 0,
    "charge": 0,
    "discharge": 0,
    "load": 2045,
    "pv": 0,
    "grid": 2045,
    "battery_num": 0,
}
# Minutes the 15 minute staleness limit takes to run out, polled each minute.
STALE_POLLS = 17


def _devices(
    report_age: timedelta = timedelta(), ct_grid: int = 2045
) -> dict[str, MarstekCloudDevice]:
    """Return the cloud device list, as last reported report_age ago."""
    report_time = int((dt_util.utcnow() - report_age).timestamp())
    return {
        raw["devid"]: replace(
            MarstekCloudDevice.from_json(raw), report_time=report_time
        )
        for raw in (*DEVICE_LIST, {**CT_METER, "grid": ct_grid})
    }


@pytest.fixture
def cloud_client() -> Generator[AsyncMock]:
    """Patch the cloud client to return a freshly reported device list."""
    with (
        patch(
            "custom_components.marstek.cloud.MarstekCloudClient.async_get_devices",
            new=AsyncMock(side_effect=lambda **_: _devices()),
        ) as get_devices,
        patch(
            "custom_components.marstek.cloud.MarstekCloudClient.async_login",
            new=AsyncMock(return_value="token"),
        ),
    ):
        yield get_devices


def _local_entry(hass: HomeAssistant, options: dict[str, str]) -> MockConfigEntry:
    """Add a local device entry as the config flow creates it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: HOST,
            CONF_MAC: INFO.mac,
            CONF_DEVICE_TYPE: INFO.device_type,
            CONF_VERSION: INFO.version,
            CONF_WIFI_MAC: INFO.wifi_mac,
            CONF_BLE_MAC: INFO.ble_mac,
        },
        options=options,
        unique_id=INFO.stable_id,
    )
    entry.add_to_hass(hass)
    return entry


async def test_cloud_used_while_device_silent(
    hass: HomeAssistant,
    client: FakeClient,
    cloud_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Values come from the cloud while the device does not answer."""
    ids = await setup_entry(hass, _local_entry(hass, BACKUP_OPTIONS))

    def state(key: str) -> str:
        return hass.states.get(ids[key]).state

    assert state("data_source") == "local"
    cloud_client.assert_not_called()

    client.online = False
    await poll(hass, freezer)
    # One missed poll keeps the local values.
    assert state("battery_soc") == "9"
    assert state("data_source") == "local"

    await poll(hass, freezer)
    assert state("data_source") == "cloud"
    assert state("error_state") == "no_response"
    assert state("battery_soc") == "27.1"
    assert state("battery_power") == "855"
    assert state("battery_status") == "selling"
    # Stored energy follows the battery level: 200 Wh at 9 % -> 602 Wh at 27.1 %.
    assert state("stored_energy") == "602"
    assert state("total_grid_output_energy") == "695"
    # Grid power is the CT meter's, not the battery's own AC output (-909 W).
    assert state("grid_power") == "2045"
    assert state("grid_phase_a_power") == "unknown"

    # Energy counts from the cloud readings: 855 W discharging and 2045 W
    # imported for one minute.
    discharged = float(state("battery_discharge_energy"))
    imported = float(state("grid_import_energy"))
    await poll(hass, freezer)
    assert round(float(state("battery_discharge_energy")) - discharged, 2) == 14.25
    assert round(float(state("grid_import_energy")) - imported, 2) == 34.08

    client.online = True
    await poll(hass, freezer)
    assert state("data_source") == "local"
    assert state("battery_soc") == "9"
    assert state("grid_power") == "2152"


async def test_stale_cloud_data_not_used(
    hass: HomeAssistant,
    client: FakeClient,
    cloud_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Cloud values that stop changing for 15 minutes are not used."""
    ids = await setup_entry(hass, _local_entry(hass, BACKUP_OPTIONS))
    stale = _devices(timedelta(hours=1))
    cloud_client.side_effect = lambda **_: stale

    client.online = False
    await poll(hass, freezer)
    await poll(hass, freezer)
    # The first values of an outage are given the benefit of the doubt.
    assert hass.states.get(ids["data_source"]).state == "cloud"
    for _ in range(STALE_POLLS):
        await poll(hass, freezer)
    assert hass.states.get(ids["battery_soc"]).state == STATE_UNAVAILABLE
    assert hass.states.get(ids["data_source"]).state == "none"


async def test_changing_cloud_values_used_despite_old_report(
    hass: HomeAssistant,
    client: FakeClient,
    cloud_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Cloud values that keep changing are used, however old report_time is.

    The cloud's report_time can lag its live values by hours.
    """
    ids = await setup_entry(hass, _local_entry(hass, BACKUP_OPTIONS))
    old = (dt_util.utcnow() - timedelta(hours=16)).timestamp()
    readings = iter(range(1000, 2000, 10))
    cloud_client.side_effect = lambda **_: {
        devid: replace(device, report_time=int(old))
        for devid, device in _devices(ct_grid=next(readings)).items()
    }

    client.online = False
    for _ in range(STALE_POLLS + 2):
        await poll(hass, freezer)
    assert hass.states.get(ids["data_source"]).state == "cloud"
    assert hass.states.get(ids["battery_soc"]).state == "27.1"


async def test_setup_from_cloud_when_device_silent(
    hass: HomeAssistant, client: FakeClient, cloud_client: AsyncMock
) -> None:
    """With the backup, setup succeeds from the cloud while the device is silent."""
    client.online = False
    entry = _local_entry(hass, BACKUP_OPTIONS)
    ids = await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ids["data_source"]).state == "cloud"
    assert hass.states.get(ids["battery_soc"]).state == "27.1"
    # Without a local reading to scale from, stored energy is unknown.
    assert hass.states.get(ids["stored_energy"]).state == "unknown"


async def test_options_flow(
    hass: HomeAssistant, client: FakeClient, cloud_client: AsyncMock
) -> None:
    """The options flow finds the device on the account by its MAC address."""
    entry = _local_entry(hass, {})
    await setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAILBOX: MAILBOX}
    )
    assert result["errors"] == {CONF_PASSWORD: "password_required"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAILBOX: MAILBOX, CONF_PASSWORD: "secret"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CLOUD_DEVID] == BATTERY_ID
    assert entry.options[CONF_PASSWORD_HASH] != "secret"
    assert entry.runtime_data.has_cloud_backup

    # Leaving the e-mail empty turns the backup off.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options == {}
    assert not entry.runtime_data.has_cloud_backup


async def test_options_flow_reuses_cloud_account(
    hass: HomeAssistant, client: FakeClient, cloud_client: AsyncMock
) -> None:
    """The password of an added cloud account is reused."""
    MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_MAILBOX: MAILBOX,
            CONF_PASSWORD_HASH: "account-hash",
            CONF_DEVICES: [],
        },
        unique_id=f"cloud_{MAILBOX}",
    ).add_to_hass(hass)
    entry = _local_entry(hass, {})
    await setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAILBOX: MAILBOX}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PASSWORD_HASH] == "account-hash"


async def test_options_flow_device_not_on_account(
    hass: HomeAssistant, client: FakeClient, cloud_client: AsyncMock
) -> None:
    """An account without this device is refused."""
    cloud_client.side_effect = lambda **_: {
        devid: device for devid, device in _devices().items() if devid != BATTERY_ID
    }
    entry = _local_entry(hass, {})
    await setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAILBOX: MAILBOX, CONF_PASSWORD: "secret"}
    )
    assert result["errors"] == {"base": "cloud_device_not_found"}


async def test_no_options_for_cloud_accounts(
    hass: HomeAssistant, cloud_client: AsyncMock
) -> None:
    """Cloud account entries have no options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MAILBOX: MAILBOX, CONF_PASSWORD_HASH: "hash", CONF_DEVICES: []},
    )
    entry.add_to_hass(hass)
    assert not entry.supports_options


async def test_diagnostics_redact_account(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    client: FakeClient,
    cloud_client: AsyncMock,
) -> None:
    """Diagnostics show the backup state without the account details."""
    assert await async_setup_component(hass, "diagnostics", {})
    entry = _local_entry(hass, BACKUP_OPTIONS)
    await setup_entry(hass, entry)

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert result["poll"]["cloud_backup"] is True
    assert result["poll"]["data_source"] == "local"
    dumped = json.dumps(result)
    for secret in (MAILBOX, BATTERY_ID, '"hash"'):
        assert secret not in dumped


async def test_stale_cloud_reason_logged(
    hass: HomeAssistant,
    client: FakeClient,
    cloud_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Why the cloud backup is not used is logged once and kept for diagnostics."""
    entry = _local_entry(hass, BACKUP_OPTIONS)
    await setup_entry(hass, entry)
    stale = _devices(timedelta(hours=1))
    cloud_client.side_effect = lambda **_: stale

    client.online = False
    for _ in range(STALE_POLLS + 3):
        await poll(hass, freezer)
    coordinator = entry.runtime_data
    assert "have not changed" in coordinator.cloud_error
    assert coordinator.cloud_report_time is not None
    assert caplog.text.count("cloud backup cannot be used") == 1

    # The reason is kept for diagnostics after the device answers again, and
    # warned about again in the next outage.
    client.online = True
    await poll(hass, freezer)
    assert "have not changed" in coordinator.cloud_error
    client.online = False
    for _ in range(STALE_POLLS + 2):
        await poll(hass, freezer)
    assert caplog.text.count("cloud backup cannot be used") == 2


async def test_cloud_timeout_is_cloud_error(hass: HomeAssistant) -> None:
    """A timed out cloud request is reported as a cloud error."""
    session = MagicMock()
    session.get.side_effect = TimeoutError
    cloud = MarstekCloudClient(session, MAILBOX, "hash", "token")
    with pytest.raises(MarstekCloudError):
        await cloud.async_get_devices()
