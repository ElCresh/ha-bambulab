"""Tests for SD card project file helpers."""

import ftplib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from pybambu.media_sources import Ftps990MediaSource, RemoteMediaFile
from pybambu.sd_card import (
    FilamentMappingError,
    FilamentSlot,
    PlateFilament,
    file_on_card,
    list_printable_files,
    loaded_slots,
    map_filaments,
    parse_project_plates,
    print_path,
)

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="1080"/>
    <filament id="1" tray_info_idx="GFA01" type="PLA" color="#9B9EA0" used_m="1.2" used_g="3.6"/>
  </plate>
  <plate>
    <metadata key="index" value="3"/>
    <filament id="1" tray_info_idx="GFA00" type="PLA" color="#000000" used_m="0.5" used_g="1.5"/>
    <filament id="3" tray_info_idx="GFG00" type="PETG" color="#FFFFFF" used_m="0.2" used_g="0.6"/>
  </plate>
</config>
"""


def tray(type_: str, color: str, empty: bool = False):
    return SimpleNamespace(type=type_, color=color, empty=empty)


class TestSdCard(unittest.TestCase):
    def test_parse_project_plates_reads_every_sliced_plate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Poop-Deflector(2).gcode.3mf")
            with ZipFile(path, "w") as archive:
                archive.writestr("Metadata/slice_info.config", SLICE_INFO)

            plates = parse_project_plates(path)

        self.assertEqual([1, 3], [plate.index for plate in plates])
        self.assertEqual((PlateFilament(1, "PLA", "#9B9EA0"),), plates[0].filaments)
        self.assertEqual([1, 3], [filament.id for filament in plates[1].filaments])

    def test_loaded_slots_uses_flat_slot_indexes(self):
        ams_data = {
            0: SimpleNamespace(tray=[tray("PLA", "000000FF"), tray("", "00000000", empty=True),
                                     tray("PLA", "A4DAE6FF"), tray("PLA", "9B9EA0FF")]),
            128: SimpleNamespace(tray=[tray("PETG", "FFFFFFFF")]),
        }

        slots = loaded_slots(ams_data)

        self.assertEqual([0, 2, 3, 16], [slot.index for slot in slots])

    def test_map_filaments_picks_closest_colour_of_the_same_type(self):
        slots = [FilamentSlot(0, "PLA", "000000FF"), FilamentSlot(1, "PLA", "FFFFFFFF"),
                 FilamentSlot(3, "PLA", "9B9EA0FF")]

        mapping = map_filaments([PlateFilament(1, "PLA", "#9B9EA0")], slots)

        self.assertEqual([3], mapping)

    def test_map_filaments_fills_unused_ids_with_minus_one(self):
        slots = [FilamentSlot(0, "PLA", "000000FF"), FilamentSlot(16, "PETG", "FFFFFFFF")]

        mapping = map_filaments([PlateFilament(1, "PLA", "#000000"),
                                 PlateFilament(3, "PETG", "#FFFFFF")], slots)

        self.assertEqual([0, -1, 16], mapping)

    def test_map_filaments_allows_two_filaments_on_one_slot(self):
        slots = [FilamentSlot(2, "PLA", "FFFFFFFF")]

        mapping = map_filaments([PlateFilament(1, "PLA", "#FFFFFF"),
                                 PlateFilament(2, "PLA", "#EEEEEE")], slots)

        self.assertEqual([2, 2], mapping)

    def test_map_filaments_refuses_a_type_with_no_loaded_slot(self):
        slots = [FilamentSlot(0, "PLA", "000000FF")]

        with self.assertRaisesRegex(FilamentMappingError, "PETG"):
            map_filaments([PlateFilament(1, "PETG", "#000000")], slots)

    def test_forced_slot_wins_for_a_single_filament_plate(self):
        slots = [FilamentSlot(0, "PLA", "000000FF"), FilamentSlot(3, "PLA", "9B9EA0FF")]

        mapping = map_filaments([PlateFilament(1, "PLA", "#000000")], slots, forced_slot=3)

        self.assertEqual([3], mapping)

    def test_forced_slot_is_ignored_for_a_multi_filament_plate(self):
        slots = [FilamentSlot(0, "PLA", "000000FF"), FilamentSlot(3, "PLA", "FFFFFFFF")]

        mapping = map_filaments([PlateFilament(1, "PLA", "#000000"),
                                 PlateFilament(2, "PLA", "#FFFFFF")], slots, forced_slot=3)

        self.assertEqual([0, 3], mapping)

    def test_print_path_is_relative_to_the_card_root(self):
        def remote(path):
            return RemoteMediaFile(name=os.path.basename(path), path=path, size=1, media_type="model",
                                   source=Ftps990MediaSource.name, storage="external",
                                   modified=datetime(2026, 10, 11, tzinfo=timezone.utc))

        self.assertEqual("Poop-Deflector(2).gcode.3mf", print_path(remote("/Poop-Deflector(2).gcode.3mf")))
        self.assertEqual("cache/model.3mf", print_path(remote("/cache/model.3mf")))


class FakeFtp:
    def __init__(self, listings, files=None):
        self._listings = listings
        self._files = files or {}

    def retrlines(self, command, callback):
        for line in self._listings.get(command.removeprefix("LIST "), []):
            callback(line)

    def size(self, path):
        if path not in self._files:
            raise ftplib.error_perm(f"550 {path}: No such file")
        return len(self._files[path])

    def retrbinary(self, command, callback):
        callback(self._files[command.removeprefix("RETR ")])

    def quit(self):
        pass


class FakeFtpClient:
    ftp_enabled = True
    tcp6000_media_storages = []

    def __init__(self, listings, files=None, tcp6000=False, error=None):
        self._listings = listings
        self._files = files
        self._error = error
        # A printer that offers TCP 6000 too: the SD card code must not use it.
        self.tcp6000_media_supported = tcp6000
        self.probed = False

    def probe_remote_media_sources(self, force=False):
        self.probed = True
        return self.tcp6000_media_supported

    def ftp_connection(self):
        if self._error is not None:
            raise self._error
        return FakeFtp(self._listings, self._files)


def remote_file(path, size):
    return RemoteMediaFile(name=os.path.basename(path), path=path, size=size, media_type="model",
                           source=Ftps990MediaSource.name, storage="external",
                           modified=datetime(2026, 10, 11, tzinfo=timezone.utc))


class TestListPrintableFiles(unittest.TestCase):
    def test_lists_3mf_files_from_root_and_cache_newest_first(self):
        client = FakeFtpClient({
            "/": [
                "-rw-rw-rw- 1 user group 104857 Oct 11 15:01 Poop-Deflector(2).gcode.3mf",
                "-rw-rw-rw- 1 user group 512 Oct 11 15:02 notes.txt",
                "drwxrwxrwx 1 user group 0 Oct 01 10:00 cache",
            ],
            "/cache/": [
                "-rw-rw-rw- 1 user group 2048 Oct 10 09:00 model.3mf",
                "-rw-rw-rw- 1 user group 4096 Oct 11 16:30 newer.3mf",
            ],
        })

        files = list_printable_files(client)

        self.assertEqual(["/cache/newer.3mf", "/Poop-Deflector(2).gcode.3mf", "/cache/model.3mf"],
                         [file.path for file in files])

    def test_unreachable_printer_raises_instead_of_listing_nothing(self):
        client = FakeFtpClient({}, error=OSError("timed out"))

        with self.assertRaisesRegex(OSError, "timed out"):
            list_printable_files(client)

    def test_sd_card_lists_and_downloads_over_ftps_only(self):
        from pybambu.models import PrintJob

        client = FakeFtpClient(
            {"/": ["-rw-rw-rw- 1 user group 5 Oct 11 15:01 model.3mf"]},
            files={"/model.3mf": b"12345"},
            tcp6000=True,
        )

        files = list_printable_files(client)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sdcard" / "model.3mf"
            size = PrintJob(client).download_ftps_file(files[0], target)
            self.assertEqual(b"12345", target.read_bytes())
            self.assertFalse(Path(f"{target}.part").exists())

        self.assertEqual(5, size)
        self.assertEqual([Ftps990MediaSource.name], [file.source for file in files])
        self.assertFalse(client.probed)


class TestFileOnCard(unittest.TestCase):
    def test_file_with_the_listed_size_is_on_the_card(self):
        client = FakeFtpClient({}, files={"/model.3mf": b"12345"})

        self.assertTrue(file_on_card(client, remote_file("/model.3mf", 5)))

    def test_changed_or_missing_file_is_not_on_the_card(self):
        client = FakeFtpClient({}, files={"/model.3mf": b"12345"})

        self.assertFalse(file_on_card(client, remote_file("/model.3mf", 6)))
        self.assertFalse(file_on_card(client, remote_file("/gone.3mf", 5)))

    def test_unreachable_printer_raises(self):
        client = FakeFtpClient({}, error=OSError("timed out"))

        with self.assertRaisesRegex(OSError, "timed out"):
            file_on_card(client, remote_file("/model.3mf", 5))


if __name__ == "__main__":
    unittest.main()
