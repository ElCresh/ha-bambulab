"""Project files on the printer's SD card: sliced plates, filaments and AMS slot mapping."""
from __future__ import annotations

import ftplib

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree
from zipfile import ZipFile

from .media_sources import (
    Ftps990MediaSource,
    RemoteMediaFile,
    dedupe_remote_files,
    sort_newest_first,
)
from .models import AMS_HT_SLOT_BASE, AMS_HT_UNIT_BASE, AMS_TRAYS_PER_UNIT

SD_CARD_EXTENSIONS = [".3mf"]
SD_CARD_SEARCH_PATHS = ["/", "/cache/"]


class FilamentMappingError(Exception):
    """Raised when a plate filament has no loaded slot of the same type."""


@dataclass(frozen=True)
class PlateFilament:
    id: int
    type: str
    color: str


@dataclass(frozen=True)
class ProjectPlate:
    index: int
    filaments: tuple[PlateFilament, ...]


@dataclass(frozen=True)
class FilamentSlot:
    index: int
    type: str
    color: str


def parse_project_plates(archive_path: str | Path) -> list[ProjectPlate]:
    """Read the sliced plates and their filaments from a 3mf's slice_info.config."""
    with ZipFile(archive_path) as archive:
        root = ElementTree.fromstring(archive.read('Metadata/slice_info.config'))

    plates = []
    for plate in root.findall('plate'):
        index = None
        filaments = []
        for metadata in plate:
            if metadata.get('key') == 'index':
                index = int(metadata.get('value'))
            elif metadata.tag == 'filament':
                filaments.append(PlateFilament(id=int(metadata.get('id')),
                                               type=metadata.get('type', ''),
                                               color=metadata.get('color', '')))
        if index is not None:
            plates.append(ProjectPlate(index=index, filaments=tuple(filaments)))
    return plates


def loaded_slots(ams_data: dict) -> list[FilamentSlot]:
    """Every loaded AMS slot, by the flat index print.ams_mapping uses."""
    slots = []
    for unit, ams in ams_data.items():
        if ams is None:
            continue
        for tray_index, tray in enumerate(ams.tray):
            if tray is None or tray.empty or not tray.type:
                continue
            if unit >= AMS_HT_UNIT_BASE:
                index = AMS_HT_SLOT_BASE + unit - AMS_HT_UNIT_BASE
            else:
                index = unit * AMS_TRAYS_PER_UNIT + tray_index
            slots.append(FilamentSlot(index=index, type=tray.type, color=tray.color))
    return sorted(slots, key=lambda slot: slot.index)


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip('#')[:6].ljust(6, '0')
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _color_distance(left: str, right: str) -> int:
    return sum((a - b) ** 2 for a, b in zip(_rgb(left), _rgb(right)))


def map_filaments(
    filaments: Iterable[PlateFilament],
    slots: list[FilamentSlot],
    forced_slot: int | None = None,
) -> list[int]:
    """Map each plate filament to the closest loaded slot of the same type.

    The result is indexed by slicer filament id - 1, with -1 for ids the plate does not use,
    which is the shape print.ams_mapping expects. A forced slot only applies to a single
    filament plate.
    """
    filaments = list(filaments)
    if not filaments:
        return []

    mapping = [-1] * max(filament.id for filament in filaments)
    for filament in filaments:
        if forced_slot is not None and len(filaments) == 1:
            mapping[filament.id - 1] = forced_slot
            continue
        candidates = [slot for slot in slots if slot.type.upper() == filament.type.upper()]
        if not candidates:
            raise FilamentMappingError(
                f"No loaded {filament.type} slot for filament {filament.id} ({filament.color})"
            )
        best = min(candidates, key=lambda slot: (_color_distance(slot.color, filament.color), slot.index))
        mapping[filament.id - 1] = best.index
    return mapping


def print_path(remote_file: RemoteMediaFile) -> str:
    """The file path print_project_file expects: relative to the card root."""
    return remote_file.path.replace('\\', '/').lstrip('/')


def list_printable_files(client) -> list[RemoteMediaFile]:
    """The printable project files on the SD card, newest first.

    Lists over FTPS only, and raises when the printer cannot be reached so a failed
    refresh does not look like an empty card.
    """
    source = Ftps990MediaSource(client)
    try:
        files = source.list_files("model", SD_CARD_EXTENSIONS, SD_CARD_SEARCH_PATHS)
    finally:
        source.close()
    return sort_newest_first(dedupe_remote_files(files))


def file_on_card(client, remote_file: RemoteMediaFile) -> bool:
    """True if the file is still on the SD card with the listed size.

    A missing file returns False; a printer that cannot be reached raises.
    """
    ftp = client.ftp_connection()
    try:
        try:
            size = ftp.size(remote_file.path)
        except ftplib.error_perm:
            return False
        return size is not None and int(size) == remote_file.size
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
