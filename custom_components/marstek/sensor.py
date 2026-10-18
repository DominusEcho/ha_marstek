"""Sensor platform for Marstek devices."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import override

from aiomarstek import MarstekDeviceStatus

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .cloud import MarstekCloudDevice
from .cloud_coordinator import MarstekCloudConfigEntry, MarstekCloudCoordinator
from .const import (
    BATTERY_STATUS_OPTIONS,
    CLOUD_PV_TYPE_PREFIX,
    CONF_MAILBOX,
    DATA_SOURCE_OPTIONS,
    DEVICE_MODE_OPTIONS,
    ERROR_STATE_OPTIONS,
    PV_DEVICE_TYPES,
    PV_STATE_OPTIONS,
)
from .coordinator import MarstekConfigEntry
from .entity import MarstekCloudEntity, MarstekEntity

_LOGGER = logging.getLogger(__name__)

# Polling gaps longer than this (e.g. Home Assistant downtime) are not counted
# towards the integrated energy, as the power during the gap is unknown.
MAX_ENERGY_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True, kw_only=True)
class MarstekSensorEntityDescription(SensorEntityDescription):
    """Describe a Marstek sensor entity."""

    requires_pv: bool = False
    # Derives the value from the status; reads the field named by key if unset.
    value_fn: Callable[[MarstekDeviceStatus], StateType] | None = None


@dataclass(frozen=True, kw_only=True)
class MarstekEnergySensorEntityDescription(SensorEntityDescription):
    """Describe an energy sensor integrated from a power reading."""

    # The power to count, in W, never negative; None while it is unknown.
    power_fn: Callable[[MarstekDeviceStatus], float | None]
    device_class: SensorDeviceClass = SensorDeviceClass.ENERGY
    native_unit_of_measurement: str = UnitOfEnergy.WATT_HOUR
    state_class: SensorStateClass = SensorStateClass.TOTAL_INCREASING
    suggested_display_precision: int = 0


def _grid_power(direction: int) -> Callable[[MarstekDeviceStatus], float | None]:
    """Return the grid power drawn from (1) or fed into (-1) the grid."""

    def power(status: MarstekDeviceStatus) -> float | None:
        if status.grid_power is None:
            return None
        return max(direction * float(status.grid_power), 0)

    return power


def _battery_power(
    battery_status: str,
) -> Callable[[MarstekDeviceStatus], float | None]:
    """Return the battery power while the battery has the given status."""

    def power(status: MarstekDeviceStatus) -> float | None:
        if status.battery_power is None or status.battery_status is None:
            return None
        if status.battery_status != battery_status:
            return 0
        return float(status.battery_power)

    return power


def _pv_input_sensor_descriptions() -> tuple[MarstekSensorEntityDescription, ...]:
    """Build sensors for each of the device's four PV inputs."""
    descriptions: list[MarstekSensorEntityDescription] = []
    for pv_input in range(1, 5):
        for metric, device_class, unit in (
            (
                "power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
            ),
            (
                "voltage",
                SensorDeviceClass.VOLTAGE,
                UnitOfElectricPotential.VOLT,
            ),
            (
                "current",
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
            ),
            (
                "state",
                SensorDeviceClass.ENUM,
                None,
            ),
        ):
            key = f"pv{pv_input}_{metric}"
            descriptions.append(
                MarstekSensorEntityDescription(
                    key=key,
                    translation_key=f"pv_{metric}",
                    translation_placeholders={"input": str(pv_input)},
                    device_class=device_class,
                    native_unit_of_measurement=unit,
                    state_class=(
                        SensorStateClass.MEASUREMENT if metric != "state" else None
                    ),
                    options=list(PV_STATE_OPTIONS) if metric == "state" else None,
                    requires_pv=True,
                )
            )
    return tuple(descriptions)


SENSOR_DESCRIPTIONS: tuple[MarstekSensorEntityDescription, ...] = (
    MarstekSensorEntityDescription(
        key="battery_soc",
        translation_key="battery_soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    MarstekSensorEntityDescription(
        key="battery_power",
        translation_key="battery_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    MarstekSensorEntityDescription(
        key="total_pv_energy",
        translation_key="total_pv_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        requires_pv=True,
    ),
    MarstekSensorEntityDescription(
        key="total_grid_output_energy",
        translation_key="total_grid_output_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    MarstekSensorEntityDescription(
        key="total_grid_input_energy",
        translation_key="total_grid_input_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    MarstekSensorEntityDescription(
        key="total_load_energy",
        translation_key="total_load_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    MarstekSensorEntityDescription(
        key="grid_power",
        translation_key="grid_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    *(
        MarstekSensorEntityDescription(
            key=f"grid_phase_{phase}_power",
            translation_key="grid_phase_power",
            translation_placeholders={"phase": phase.upper()},
            device_class=SensorDeviceClass.POWER,
            native_unit_of_measurement=UnitOfPower.WATT,
            state_class=SensorStateClass.MEASUREMENT,
        )
        for phase in ("a", "b", "c")
    ),
    MarstekSensorEntityDescription(
        key="stored_energy",
        translation_key="stored_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda status: status.battery_energy,
    ),
    MarstekSensorEntityDescription(
        key="device_mode",
        translation_key="device_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(DEVICE_MODE_OPTIONS),
    ),
    MarstekSensorEntityDescription(
        key="battery_status",
        translation_key="battery_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(BATTERY_STATUS_OPTIONS),
    ),
    *_pv_input_sensor_descriptions(),
)


ENERGY_SENSOR_DESCRIPTIONS: tuple[MarstekEnergySensorEntityDescription, ...] = (
    MarstekEnergySensorEntityDescription(
        key="grid_import_energy",
        translation_key="grid_import_energy",
        power_fn=_grid_power(1),
    ),
    MarstekEnergySensorEntityDescription(
        key="grid_export_energy",
        translation_key="grid_export_energy",
        power_fn=_grid_power(-1),
    ),
    # The battery's power on its AC side, so charging losses count as charged
    # energy, as the Energy dashboard expects.
    MarstekEnergySensorEntityDescription(
        key="battery_charge_energy",
        translation_key="battery_charge_energy",
        power_fn=_battery_power("charging"),
    ),
    MarstekEnergySensorEntityDescription(
        key="battery_discharge_energy",
        translation_key="battery_discharge_energy",
        power_fn=_battery_power("selling"),
    ),
)


LAST_RESPONSE_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="last_response",
    translation_key="last_response",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)

ERROR_STATE_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="error_state",
    translation_key="error_state",
    device_class=SensorDeviceClass.ENUM,
    options=list(ERROR_STATE_OPTIONS),
    entity_category=EntityCategory.DIAGNOSTIC,
)

DATA_SOURCE_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="data_source",
    translation_key="data_source",
    device_class=SensorDeviceClass.ENUM,
    options=list(DATA_SOURCE_OPTIONS),
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MarstekConfigEntry | MarstekCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Marstek sensors based on a config entry."""
    if CONF_MAILBOX in config_entry.data:
        _async_setup_cloud_entry(config_entry, async_add_entities)
        return

    coordinator = config_entry.runtime_data
    device_ip = coordinator.device_ip
    _LOGGER.debug("Setting up Marstek sensors: %s", device_ip)

    has_pv = coordinator.device_info.device_type in PV_DEVICE_TYPES
    sensors: list[SensorEntity] = [
        MarstekSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
        if has_pv or not description.requires_pv
    ]
    sensors.extend(
        MarstekEnergySensor(coordinator, description)
        for description in ENERGY_SENSOR_DESCRIPTIONS
    )
    sensors.append(
        MarstekLastResponseSensor(coordinator, LAST_RESPONSE_SENSOR_DESCRIPTION)
    )
    sensors.append(MarstekErrorStateSensor(coordinator, ERROR_STATE_SENSOR_DESCRIPTION))
    sensors.append(MarstekDataSourceSensor(coordinator, DATA_SOURCE_SENSOR_DESCRIPTION))

    _LOGGER.debug("Device %s sensors set up, total %d", device_ip, len(sensors))
    async_add_entities(sensors)


class MarstekSensor(MarstekEntity, SensorEntity):
    """Representation of a Marstek sensor."""

    entity_description: MarstekSensorEntityDescription

    @property
    @override
    def native_value(self) -> StateType | None:
        """Return the state of the sensor."""
        if value_fn := self.entity_description.value_fn:
            return value_fn(self.coordinator.data)
        return self.coordinator.data.get_value(self.entity_description.key)


class MarstekEnergySensor(MarstekEntity, RestoreSensor):
    """Energy integrated from a power reading, such as the CT meter's."""

    entity_description: MarstekEnergySensorEntityDescription
    _energy: float | None = None
    _last_power: float | None = None
    _last_update: datetime | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the energy total accumulated before a restart."""
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None and (
            isinstance(last_data.native_value, int | float)
        ):
            self._energy = float(last_data.native_value)
        self._record_power()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Add the energy flowed since the previous poll (left Riemann sum)."""
        if not self.coordinator.fresh:
            # Neither the device nor the cloud answered, so the power since the
            # last reading is unknown; resume counting from the next reading.
            self._last_power = None
            super()._handle_coordinator_update()
            return
        now = dt_util.utcnow()
        if self._last_power is not None and self._last_update is not None:
            elapsed = now - self._last_update
            if elapsed <= MAX_ENERGY_INTERVAL:
                self._energy = (self._energy or 0.0) + (
                    self._last_power * elapsed.total_seconds() / 3600
                )
        self._record_power(now)
        super()._handle_coordinator_update()

    def _record_power(self, now: datetime | None = None) -> None:
        """Remember the current power as the start of the next interval."""
        self._last_power = self.entity_description.power_fn(self.coordinator.data)
        self._last_update = now or dt_util.utcnow()
        if self._last_power is not None and self._energy is None:
            self._energy = 0.0

    @property
    @override
    def native_value(self) -> float | None:
        """Return the accumulated energy."""
        return self._energy


class MarstekLastResponseSensor(MarstekEntity, SensorEntity):
    """When the device last answered a status request."""

    @property
    @override
    def available(self) -> bool:
        """Stay available while the device is not answering, to show since when."""
        return True

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the time of the device's last response."""
        return self.coordinator.last_response


class MarstekErrorStateSensor(MarstekEntity, SensorEntity):
    """Outcome of the latest poll of the device."""

    @property
    @override
    def available(self) -> bool:
        """Stay available while the device is not answering, to show why."""
        return True

    @property
    @override
    def native_value(self) -> str:
        """Return the error state of the latest poll."""
        return self.coordinator.error_state


class MarstekDataSourceSensor(MarstekEntity, SensorEntity):
    """Where the latest values came from: the local API or the cloud."""

    @property
    @override
    def available(self) -> bool:
        """Stay available while there are no values, to show that."""
        return True

    @property
    @override
    def native_value(self) -> str:
        """Return the source of the latest values."""
        return self.coordinator.data_source


@dataclass(frozen=True, kw_only=True)
class MarstekCloudSensorEntityDescription(SensorEntityDescription):
    """Describe a cloud Marstek sensor entity."""

    value_fn: Callable[[MarstekCloudDevice], StateType]
    # Only create this sensor for devices that have PV / a battery.
    requires_pv: bool = False
    requires_battery: bool = False


CLOUD_SENSOR_DESCRIPTIONS: tuple[MarstekCloudSensorEntityDescription, ...] = (
    MarstekCloudSensorEntityDescription(
        key="cloud_pv_power",
        translation_key="solar_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.pv_power,
        requires_pv=True,
    ),
    MarstekCloudSensorEntityDescription(
        key="cloud_grid_power",
        translation_key="grid_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.grid_power,
    ),
    MarstekCloudSensorEntityDescription(
        key="cloud_load_power",
        translation_key="load_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.load_power,
    ),
    MarstekCloudSensorEntityDescription(
        key="cloud_charge_power",
        translation_key="charge_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.charge_power,
        requires_battery=True,
    ),
    MarstekCloudSensorEntityDescription(
        key="cloud_discharge_power",
        translation_key="discharge_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.discharge_power,
        requires_battery=True,
    ),
    MarstekCloudSensorEntityDescription(
        key="cloud_battery_soc",
        translation_key="battery_soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.battery_soc,
        requires_battery=True,
    ),
)


def _async_setup_cloud_entry(
    config_entry: MarstekCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for every device of a cloud account entry."""
    sensors: list[SensorEntity] = []
    for coordinator in config_entry.runtime_data.values():
        device = coordinator.data
        has_pv = device.device_type.startswith(CLOUD_PV_TYPE_PREFIX)
        has_battery = device.battery_num >= 1

        sensors.extend(
            MarstekCloudSensor(coordinator, description)
            for description in CLOUD_SENSOR_DESCRIPTIONS
            if (not description.requires_pv or has_pv)
            and (not description.requires_battery or has_battery)
        )
        if has_pv:
            sensors.append(MarstekCloudSolarEnergySensor(coordinator))
        sensors.append(MarstekCloudLastReportSensor(coordinator))

    _LOGGER.debug("Cloud account sensors set up, total %d", len(sensors))
    async_add_entities(sensors)


class MarstekCloudSensor(MarstekCloudEntity, SensorEntity):
    """Representation of a cloud Marstek sensor."""

    entity_description: MarstekCloudSensorEntityDescription

    @property
    @override
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)


SOLAR_ENERGY_DESCRIPTION = SensorEntityDescription(
    key="cloud_solar_energy",
    translation_key="solar_energy",
    device_class=SensorDeviceClass.ENERGY,
    native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
    state_class=SensorStateClass.TOTAL_INCREASING,
    suggested_display_precision=0,
)


class MarstekCloudSolarEnergySensor(MarstekCloudEntity, RestoreSensor):
    """Solar energy integrated from the cloud PV power readings.

    The cloud only reports instantaneous PV power, so energy for the Energy
    dashboard is accumulated here with a left Riemann sum, the same way the
    local CT-meter grid energy is derived.
    """

    _energy: float | None = None
    _last_power: float | None = None
    _last_update: datetime | None = None

    def __init__(self, coordinator: MarstekCloudCoordinator) -> None:
        """Initialize the solar energy sensor."""
        super().__init__(coordinator, SOLAR_ENERGY_DESCRIPTION)

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the energy total accumulated before a restart."""
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None and (
            isinstance(last_data.native_value, int | float)
        ):
            self._energy = float(last_data.native_value)
        self._record_power()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Add the energy produced since the previous poll (left Riemann sum)."""
        now = dt_util.utcnow()
        if self._last_power is not None and self._last_update is not None:
            elapsed = now - self._last_update
            if elapsed <= MAX_ENERGY_INTERVAL:
                power = max(self._last_power, 0)
                self._energy = (self._energy or 0.0) + (
                    power * elapsed.total_seconds() / 3600
                )
        self._record_power(now)
        super()._handle_coordinator_update()

    def _record_power(self, now: datetime | None = None) -> None:
        """Remember the current PV power as the start of the next interval."""
        power = self.coordinator.data.pv_power
        self._last_power = float(power) if power is not None else None
        self._last_update = now or dt_util.utcnow()
        if self._last_power is not None and self._energy is None:
            self._energy = 0.0

    @property
    @override
    def native_value(self) -> float | None:
        """Return the accumulated solar energy."""
        return self._energy


LAST_REPORT_DESCRIPTION = SensorEntityDescription(
    key="cloud_last_report",
    translation_key="last_report",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)


class MarstekCloudLastReportSensor(MarstekCloudEntity, SensorEntity):
    """When the device last reported to the cloud."""

    def __init__(self, coordinator: MarstekCloudCoordinator) -> None:
        """Initialize the last-report sensor."""
        super().__init__(coordinator, LAST_REPORT_DESCRIPTION)

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the device's last cloud report time."""
        report_time = self.coordinator.data.report_time
        if not report_time:
            return None
        return dt_util.utc_from_timestamp(report_time)
