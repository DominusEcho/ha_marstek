"""Base entity for Marstek devices."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud_coordinator import MarstekCloudCoordinator
from .const import DOMAIN
from .coordinator import MarstekDataUpdateCoordinator


class MarstekEntity(CoordinatorEntity[MarstekDataUpdateCoordinator]):
    """Base class for Marstek entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MarstekDataUpdateCoordinator,
        entity_description: EntityDescription,
    ) -> None:
        """Initialize the Marstek entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description

        device_info = coordinator.device_info
        self._attr_unique_id = f"{device_info.stable_id}_{entity_description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_info.stable_id)},
            name=f"Marstek {device_info.device_type} v{device_info.version}",
            manufacturer="Marstek",
            model=device_info.device_type,
            sw_version=str(device_info.version),
        )


class MarstekCloudEntity(CoordinatorEntity[MarstekCloudCoordinator]):
    """Base class for cloud-polled Marstek entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MarstekCloudCoordinator,
        entity_description: EntityDescription,
    ) -> None:
        """Initialize the cloud entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description

        devid = coordinator.devid
        device = coordinator.data
        self._attr_unique_id = f"cloud_{devid}_{entity_description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"cloud_{devid}")},
            name=device.name,
            manufacturer="Marstek",
            model=device.device_type,
            sw_version=device.version or None,
            serial_number=devid,
        )
