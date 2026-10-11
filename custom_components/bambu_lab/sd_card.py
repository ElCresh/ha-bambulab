"""Browse the printable files on the printer's SD card and start one."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError

from .const import LOGGER, Options
from .pybambu.media_sources import RemoteMediaFile
from .pybambu.models import ams_slot_name
from .pybambu.sd_card import (
    FilamentMappingError,
    ProjectPlate,
    file_on_card,
    list_printable_files,
    loaded_slots,
    map_filaments,
    parse_project_plates,
    print_path,
)

AUTOMATIC_SLOT = "automatic"
EXTERNAL_SPOOL = "external_spool"
STOPPED_STATES = ("IDLE", "FINISH", "FAILED")


class SdCardBrowser:
    """SD card file list, selection and print settings for one printer."""

    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator
        self._lock = asyncio.Lock()
        self.files: list[RemoteMediaFile] = []
        self.selected: RemoteMediaFile | None = None
        self.plates: list[ProjectPlate] = []
        self.plate: int | None = None
        self.slot = AUTOMATIC_SLOT
        self.last_error: str | None = None
        self.read_error: str | None = None
        self.busy = False

    @property
    def _model(self):
        return self._coordinator.get_model()

    @property
    def printer_stopped(self) -> bool:
        return self._model.print_job.gcode_state in STOPPED_STATES

    def file_options(self) -> list[str]:
        return [print_path(file) for file in self.files]

    def plate_options(self) -> list[str]:
        return [str(plate.index) for plate in self.plates]

    def slot_options(self) -> list[str]:
        return ([AUTOMATIC_SLOT]
                + [ams_slot_name(slot.index) for slot in loaded_slots(self._model.ams.data)]
                + [EXTERNAL_SPOOL])

    async def async_refresh(self, user: bool = False) -> None:
        """Re-read the file list, only while the printer is not working."""
        if not self.printer_stopped:
            if user:
                raise HomeAssistantError("The file list can only be refreshed while the printer is idle")
            return
        async with self._lock:
            try:
                self.files = await self._coordinator.hass.async_add_executor_job(
                    list_printable_files, self._coordinator.client)
                self.last_error = None
            except Exception as e:
                self.last_error = f"Reading the SD card failed: {e}"
                LOGGER.warning(self.last_error)
        if self.selected is not None and print_path(self.selected) not in self.file_options():
            self._clear_selection()
        self._coordinator._update_data()

    async def async_select_file(self, option: str) -> None:
        """Select a file and read its plates and filaments."""
        file = next((f for f in self.files if print_path(f) == option), None)
        if file is None:
            raise HomeAssistantError(f"{option} is not on the SD card")
        # A slot forced for another file must not carry over to this one.
        if self.selected is None or print_path(self.selected) != option:
            self.slot = AUTOMATIC_SLOT
        self._clear_selection()
        self.selected = file
        self.busy = True
        self._coordinator._update_data()
        async with self._lock:
            if self.selected is not file:
                return
            plates: list[ProjectPlate] = []
            error = None
            try:
                plates = await self._coordinator.hass.async_add_executor_job(self._read_plates, file)
            except Exception as e:
                error = f"Reading {option} failed: {e}"
                LOGGER.warning(error)
            # A newer selection owns the state now: a stale read must not touch it.
            if self.selected is not file:
                return
            self.plates = plates
            self.plate = plates[0].index if plates else None
            self.read_error = error
            self.busy = False
        self._coordinator._update_data()

    def _read_plates(self, file: RemoteMediaFile) -> list[ProjectPlate]:
        local = Path(self._coordinator.client.cache_path) / "sdcard" / print_path(file)
        modified = file.modified.timestamp() if file.modified else None
        if not (local.exists() and local.stat().st_size == file.size
                and (modified is None or int(local.stat().st_mtime) == int(modified))):
            self._model.print_job.download_ftps_file(file, local)
            if modified is not None:
                os.utime(local, (modified, modified))
        return parse_project_plates(local)

    def _clear_selection(self) -> None:
        self.selected = None
        self.plates = []
        self.plate = None
        self.read_error = None
        self.busy = False

    def select_plate(self, option: str) -> None:
        self.plate = int(option)
        self._coordinator._update_data()

    def select_slot(self, option: str) -> None:
        self.slot = option
        self._coordinator._update_data()

    def _plate_filaments(self):
        plate = next((p for p in self.plates if p.index == self.plate), None)
        return plate.filaments if plate else ()

    def mapping(self) -> tuple[bool, str]:
        """(use_ams, ams_mapping) for the selected plate."""
        filaments = self._plate_filaments()
        slots = loaded_slots(self._model.ams.data)
        if self.slot == EXTERNAL_SPOOL:
            if len(filaments) > 1:
                raise FilamentMappingError("The external spool can only print a single filament plate")
            return False, "0"
        forced = None
        if self.slot != AUTOMATIC_SLOT:
            forced = next((s for s in slots if ams_slot_name(s.index) == self.slot), None)
            if forced is None:
                raise FilamentMappingError(f"{self.slot} is no longer loaded")
        if not slots:
            return False, "0"
        if not filaments:
            raise FilamentMappingError("The selected plate has no filaments to map")
        if (forced is not None and len(filaments) == 1
                and forced.type.upper() != filaments[0].type.upper()):
            raise FilamentMappingError(
                f"{self.slot} holds {forced.type}, the plate needs {filaments[0].type}")
        forced_index = forced.index if forced is not None else None
        return True, ",".join(str(i) for i in map_filaments(filaments, slots, forced_index))

    def mapping_attributes(self) -> dict:
        attributes = {"error": self.read_error or self.last_error}
        if self.selected is not None:
            attributes.update({
                "size": self.selected.size,
                "modified": self.selected.modified.isoformat() if self.selected.modified else None,
                "plates": self.plate_options(),
                "filaments": [f"{f.id}: {f.type} {f.color}" for f in self._plate_filaments()],
            })
            try:
                use_ams, ams_mapping = self.mapping()
                attributes["mapping"] = (
                    [f"{f.id} -> {ams_slot_name(int(ams_mapping.split(',')[f.id - 1]))}"
                     for f in self._plate_filaments()] if use_ams else [EXTERNAL_SPOOL])
            except FilamentMappingError as e:
                attributes["mapping"] = [str(e)]
        return attributes

    async def async_print(self) -> None:
        """Start the selected plate with the configured options."""
        # Read everything the command needs before the first await.
        file = self.selected
        plate = self.plate
        if file is None:
            raise HomeAssistantError("No SD card file selected")
        if self.busy:
            raise HomeAssistantError("The selected file is still being read")
        if plate is None:
            raise HomeAssistantError(f"{print_path(file)} could not be read")
        if not self.printer_stopped:
            raise HomeAssistantError("The printer is busy")
        try:
            use_ams, ams_mapping = self.mapping()
        except FilamentMappingError as e:
            raise HomeAssistantError(str(e)) from e

        async with self._lock:
            try:
                found = await self._coordinator.hass.async_add_executor_job(
                    file_on_card, self._coordinator.client, file)
            except Exception as e:
                raise HomeAssistantError(f"The SD card could not be checked: {e}") from e
        if self.selected is not file or self.plate != plate:
            raise HomeAssistantError("The selection changed while the SD card was checked")
        if not found:
            raise HomeAssistantError(f"{print_path(file)} is no longer on the SD card")

        published = self._coordinator._service_call_print_project_file({
            "filepath": print_path(file),
            "plate": plate,
            "timelapse": self._coordinator.get_option_enabled(Options.PRINT_TIMELAPSE),
            "bed_leveling": self._coordinator.get_option_enabled(Options.PRINT_BED_LEVELING),
            "flow_cali": self._coordinator.get_option_enabled(Options.PRINT_FLOW_CALI),
            "vibration_cali": False,
            "layer_inspect": False,
            "use_ams": use_ams,
            "ams_mapping": ams_mapping,
        })
        if not published:
            raise HomeAssistantError("Print command could not be published")
