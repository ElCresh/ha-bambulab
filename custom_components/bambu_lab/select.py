"""Support for Bambu Lab through MQTT."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.select import SelectEntity

from .const import DOMAIN, LOGGER
from .pybambu.const import Features, SPEED_PROFILE, AIRDUCT_MODES
from .coordinator import BambuDataUpdateCoordinator
from .models import BambuLabEntity
from .pybambu.sd_card import print_path


async def async_setup_entry(
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BambuLab sensor based on a config entry."""

    coordinator: BambuDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.get_model().has_full_printer_data:
        return

    LOGGER.debug("SELECT::async_setup_entry")
    # Unsure if hybrid mode also blocks speed control.
    if not coordinator.get_model().print_fun.mqtt_signature_required:
        async_add_entities( [ BambuLabSpeedSelect(coordinator) ] )

        if coordinator.get_model().supports_feature(Features.AIRDUCT_MODE):
            async_add_entities([BambuLabAirductModeSelect(coordinator)])

    if coordinator.client.ftp_enabled:
        async_add_entities([
            BambuLabSdCardFileSelect(coordinator),
            BambuLabSdCardPlateSelect(coordinator),
            BambuLabSdCardSlotSelect(coordinator),
        ])


class BambuLabSpeedSelect(BambuLabEntity, SelectEntity):
    """Speed select options."""

    _attr_icon = "mdi:speedometer"
    _attr_translation_key = "printing_speed"

    def __init__(self, coordinator: BambuDataUpdateCoordinator) -> None:
        """Initialize Speed Select."""
        super().__init__(coordinator=coordinator)
        printer = self.coordinator.get_model().info
        self._attr_unique_id = f"{printer.serial}_Speed"
        self._attr_options = [
            speed for i, speed in SPEED_PROFILE.items()
        ]

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.get_model().print_job.gcode_state == 'RUNNING'

    @property
    def current_option(self) -> str:
        """Return the current selected live override."""
        return self.coordinator.get_model().speed.name

    async def async_select_option(self, option: str) -> None:
        """Set print speed."""
        self.coordinator.get_model().speed.SetSpeed(option)


class BambuLabAirductModeSelect(BambuLabEntity, SelectEntity):
    """Airduct mode select options."""

    _attr_icon = "mdi:air-filter"
    _attr_translation_key = "airduct_mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BambuDataUpdateCoordinator) -> None:
        """Initialize Airduct Mode Select."""
        super().__init__(coordinator=coordinator)
        printer = self.coordinator.get_model().info
        self._attr_unique_id = f"{printer.serial}_AirductMode"
        self._attr_options = [
            mode for mode in self.coordinator.get_model().info.airduct_modes_available
            if mode != "laser"
        ] or ["cooling", "heating"]

    @property
    def available(self) -> bool:
        """Return False if airduct is in a mode not controllable by user (e.g. laser)."""
        return self.coordinator.get_model().info.airduct_mode in (0, 1)

    @property
    def current_option(self) -> str:
        """Return the current selected airduct mode."""
        mode_id = self.coordinator.get_model().info.airduct_mode
        return AIRDUCT_MODES.get(mode_id, AIRDUCT_MODES[0])

    async def async_select_option(self, option: str) -> None:
        """Set airduct mode."""
        self.coordinator.get_model().info.set_airduct_mode(option)


class BambuLabSdCardFileSelect(BambuLabEntity, SelectEntity):
    """SD card file select."""

    _attr_icon = "mdi:folder-file"
    _attr_translation_key = "sd_card_file"

    def __init__(self, coordinator: BambuDataUpdateCoordinator) -> None:
        """Initialize SD card file select."""
        super().__init__(coordinator=coordinator)
        printer = self.coordinator.get_model().info
        self._attr_unique_id = f"{printer.serial}_SdCardFile"

    @property
    def options(self) -> list[str]:
        """Return the printable files on the SD card."""
        return self.coordinator.sd_card.file_options()

    @property
    def current_option(self) -> str | None:
        """Return the selected file."""
        selected = self.coordinator.sd_card.selected
        return print_path(selected) if selected else None

    @property
    def extra_state_attributes(self) -> dict:
        """Return the selected file details and filament mapping."""
        return self.coordinator.sd_card.mapping_attributes()

    async def async_select_option(self, option: str) -> None:
        """Select a file and read its plates."""
        await self.coordinator.sd_card.async_select_file(option)


class BambuLabSdCardPlateSelect(BambuLabEntity, SelectEntity):
    """SD card file plate select."""

    _attr_icon = "mdi:layers-outline"
    _attr_translation_key = "sd_card_plate"

    def __init__(self, coordinator: BambuDataUpdateCoordinator) -> None:
        """Initialize SD card plate select."""
        super().__init__(coordinator=coordinator)
        printer = self.coordinator.get_model().info
        self._attr_unique_id = f"{printer.serial}_SdCardPlate"

    @property
    def available(self) -> bool:
        """Return True if the selected file has plates."""
        return len(self.coordinator.sd_card.plates) > 0

    @property
    def options(self) -> list[str]:
        """Return the plates of the selected file."""
        return self.coordinator.sd_card.plate_options()

    @property
    def current_option(self) -> str | None:
        """Return the plate to print."""
        plate = self.coordinator.sd_card.plate
        return str(plate) if plate is not None else None

    async def async_select_option(self, option: str) -> None:
        """Select the plate to print."""
        self.coordinator.sd_card.select_plate(option)


class BambuLabSdCardSlotSelect(BambuLabEntity, SelectEntity):
    """AMS slot override for single filament SD card prints."""

    _attr_icon = "mdi:tray"
    _attr_translation_key = "sd_card_slot"

    def __init__(self, coordinator: BambuDataUpdateCoordinator) -> None:
        """Initialize AMS slot override select."""
        super().__init__(coordinator=coordinator)
        printer = self.coordinator.get_model().info
        self._attr_unique_id = f"{printer.serial}_SdCardSlot"

    @property
    def options(self) -> list[str]:
        """Return the slots that can be forced."""
        return self.coordinator.sd_card.slot_options()

    @property
    def current_option(self) -> str:
        """Return the slot override."""
        return self.coordinator.sd_card.slot

    async def async_select_option(self, option: str) -> None:
        """Select the AMS slot override."""
        self.coordinator.sd_card.select_slot(option)
