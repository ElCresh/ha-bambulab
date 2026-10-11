"""Optional SD card entity checks: run in a Home Assistant Python environment.

The lightweight pybambu CI suite does not install Home Assistant. These tests
skip when it is absent and make no FTP, service call or MQTT connection. They
drive the coroutines with asyncio.run, so no async pytest plugin is needed.
"""
import asyncio
import ftplib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError
from custom_components.bambu_lab.const import Options
from custom_components.bambu_lab.coordinator import BambuDataUpdateCoordinator
from custom_components.bambu_lab.switch import BambuLabPrintOptionSwitch
from custom_components.bambu_lab.pybambu.media_sources import RemoteMediaFile
from custom_components.bambu_lab.pybambu.sd_card import PlateFilament, ProjectPlate, print_path
from custom_components.bambu_lab.sd_card import AUTOMATIC_SLOT, SdCardBrowser


def remote(path):
    return RemoteMediaFile(name=path.rsplit("/", 1)[-1], path=path, size=10, media_type="model",
                           source="ftps990", storage="external",
                           modified=datetime(2026, 10, 11, tzinfo=timezone.utc))


def tray(type_, color):
    return SimpleNamespace(type=type_, color=color, empty=False)


async def run_in_place(function, *args):
    """Executor stand-in: run the real function, without a thread."""
    return function(*args)


@pytest.fixture
def browser():
    coordinator = MagicMock()
    model = coordinator.get_model()
    model.print_job.gcode_state = "FINISH"
    model.ams.data = {0: SimpleNamespace(tray=[tray("PLA", "000000FF"), tray("PLA", "FFFFFFFF"),
                                               tray("PLA", "A4DAE6FF"), tray("PLA", "9B9EA0FF")])}
    coordinator.get_option_enabled.side_effect = lambda option: option.name != "PRINT_TIMELAPSE"
    coordinator.hass.async_add_executor_job = run_in_place
    coordinator.client.ftp_connection.return_value.size.return_value = 10
    coordinator._service_call_print_project_file.return_value = True
    sd_card = SdCardBrowser(coordinator)
    sd_card.files = [remote("/Poop-Deflector(2).gcode.3mf"), remote("/cache/model.3mf")]
    sd_card.selected = sd_card.files[0]
    sd_card.plates = [ProjectPlate(1, (PlateFilament(1, "PLA", "#9B9EA0"),))]
    sd_card.plate = 1
    return sd_card


def two_filament_plate(browser):
    browser.plates = [ProjectPlate(1, (PlateFilament(1, "PLA", "#000000"),
                                       PlateFilament(2, "PLA", "#FFFFFF")))]


def test_print_sends_the_exact_file_name_and_mapping(browser):
    asyncio.run(browser.async_print())
    data = browser._coordinator._service_call_print_project_file.call_args.args[0]
    assert data["filepath"] == "Poop-Deflector(2).gcode.3mf"
    assert data["plate"] == 1
    assert data["use_ams"] is True
    assert data["ams_mapping"] == "3"
    assert data["bed_leveling"] is True and data["flow_cali"] is True and data["timelapse"] is False


def test_print_refuses_while_printing(browser):
    browser._coordinator.get_model().print_job.gcode_state = "RUNNING"
    with pytest.raises(HomeAssistantError, match="busy"):
        asyncio.run(browser.async_print())
    browser._coordinator._service_call_print_project_file.assert_not_called()


def test_print_refuses_without_a_compatible_slot(browser):
    browser.plates = [ProjectPlate(1, (PlateFilament(1, "PETG", "#000000"),))]
    with pytest.raises(HomeAssistantError, match="PETG"):
        asyncio.run(browser.async_print())
    browser._coordinator._service_call_print_project_file.assert_not_called()


def test_print_refuses_a_file_gone_from_the_card(browser):
    ftp = browser._coordinator.client.ftp_connection.return_value
    ftp.size.side_effect = ftplib.error_perm("550 No such file")
    with pytest.raises(HomeAssistantError, match="no longer on the SD card"):
        asyncio.run(browser.async_print())
    browser._coordinator._service_call_print_project_file.assert_not_called()


def test_print_refuses_when_the_card_cannot_be_checked(browser):
    browser._coordinator.client.ftp_connection.side_effect = OSError("timed out")
    with pytest.raises(HomeAssistantError, match="SD card could not be checked: timed out"):
        asyncio.run(browser.async_print())
    browser._coordinator._service_call_print_project_file.assert_not_called()


@pytest.mark.parametrize("change", ["file", "plate"])
def test_print_refuses_a_selection_changed_during_the_check(browser, change):
    async def check_while_the_user_changes_the_selection(function, *args):
        if change == "file":
            browser.selected = browser.files[1]
        else:
            browser.plate = 2
        return True

    browser._coordinator.hass.async_add_executor_job = check_while_the_user_changes_the_selection
    with pytest.raises(HomeAssistantError, match="selection changed"):
        asyncio.run(browser.async_print())
    browser._coordinator._service_call_print_project_file.assert_not_called()


def test_print_raises_when_the_command_is_not_published(browser):
    browser._coordinator._service_call_print_project_file.return_value = False
    with pytest.raises(HomeAssistantError, match="could not be published"):
        asyncio.run(browser.async_print())


def test_print_refuses_while_reading_or_without_selection(browser):
    browser.busy = True
    with pytest.raises(HomeAssistantError, match="still being read"):
        asyncio.run(browser.async_print())
    browser.busy = False
    browser.selected = None
    with pytest.raises(HomeAssistantError, match="No SD card file selected"):
        asyncio.run(browser.async_print())


def test_forced_slot_and_external_spool(browser):
    browser.select_slot("AMS 1 Tray 1")
    assert browser.mapping() == (True, "0")
    browser.select_slot("external_spool")
    assert browser.mapping() == (False, "0")


def test_selecting_another_file_resets_the_slot(browser):
    a, b = browser.files
    browser._read_plates = MagicMock(return_value=[ProjectPlate(1, (PlateFilament(1, "PLA", "#000000"),))])
    browser.select_slot("AMS 1 Tray 2")
    asyncio.run(browser.async_select_file(print_path(a)))
    assert browser.slot == "AMS 1 Tray 2"
    asyncio.run(browser.async_select_file(print_path(b)))
    assert browser.slot == AUTOMATIC_SLOT


def test_external_spool_refuses_a_multi_filament_plate(browser):
    two_filament_plate(browser)
    browser.select_slot("external_spool")
    with pytest.raises(HomeAssistantError, match="external spool"):
        asyncio.run(browser.async_print())
    assert "external spool" in browser.mapping_attributes()["mapping"][0]
    browser._coordinator._service_call_print_project_file.assert_not_called()


def test_forced_slot_no_longer_loaded_refuses(browser):
    browser.select_slot("AMS 1 Tray 4")
    browser._coordinator.get_model().ams.data[0].tray[3] = None
    assert "AMS 1 Tray 4" not in browser.slot_options()
    with pytest.raises(HomeAssistantError, match="no longer loaded"):
        asyncio.run(browser.async_print())
    assert "no longer loaded" in browser.mapping_attributes()["mapping"][0]
    browser._coordinator._service_call_print_project_file.assert_not_called()


def test_forced_slot_of_another_type_refuses(browser):
    browser._coordinator.get_model().ams.data[0].tray[3] = tray("PETG", "9B9EA0FF")
    browser.select_slot("AMS 1 Tray 4")
    with pytest.raises(HomeAssistantError, match="holds PETG, the plate needs PLA"):
        asyncio.run(browser.async_print())
    assert "holds PETG" in browser.mapping_attributes()["mapping"][0]
    browser._coordinator._service_call_print_project_file.assert_not_called()


def test_failed_refresh_keeps_the_old_list(browser):
    browser._coordinator.client.ftp_connection.side_effect = OSError("timed out")
    asyncio.run(browser.async_refresh())
    assert [f.path for f in browser.files] == ["/Poop-Deflector(2).gcode.3mf", "/cache/model.3mf"]
    assert "timed out" in browser.last_error


def gated_reads(browser):
    """Executor stand-in: each file's read waits for its own future."""
    gates = {}

    async def job(function, file):
        return await gates.setdefault(file.path, asyncio.get_running_loop().create_future())

    browser._coordinator.hass.async_add_executor_job = job
    return gates


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_stale_read_does_not_overwrite_a_newer_selection(browser):
    async def scenario():
        a, b = browser.files
        gates = gated_reads(browser)
        task_a = asyncio.create_task(browser.async_select_file(print_path(a)))
        await settle()
        task_b = asyncio.create_task(browser.async_select_file(print_path(b)))
        await settle()
        assert browser.selected is b
        gates[a.path].set_result([ProjectPlate(1, (PlateFilament(1, "PLA", "#000000"),))])
        await task_a
        assert browser.plates == [] and browser.busy
        await settle()
        gates[b.path].set_result([ProjectPlate(7, (PlateFilament(1, "PLA", "#FFFFFF"),))])
        await task_b
        assert browser.selected is b
        assert [p.index for p in browser.plates] == [7]
        assert browser.plate == 7
        assert not browser.busy and browser.read_error is None

    asyncio.run(scenario())


def test_stale_read_leaves_nothing_behind_when_the_newer_read_fails(browser):
    async def scenario():
        a, b = browser.files
        gates = gated_reads(browser)
        task_a = asyncio.create_task(browser.async_select_file(print_path(a)))
        await settle()
        task_b = asyncio.create_task(browser.async_select_file(print_path(b)))
        await settle()
        gates[a.path].set_result([ProjectPlate(1, (PlateFilament(1, "PLA", "#000000"),))])
        await task_a
        assert browser.plates == [] and browser.busy
        await settle()
        gates[b.path].set_exception(OSError("boom"))
        await task_b
        assert browser.selected is b
        assert browser.plates == [] and browser.plate is None
        assert "boom" in browser.read_error
        assert not browser.busy

    asyncio.run(scenario())


def test_failed_read_refuses_and_the_error_survives_a_refresh(browser):
    async def scenario():
        a = browser.files[0]
        gates = gated_reads(browser)
        task = asyncio.create_task(browser.async_select_file(print_path(a)))
        await settle()
        gates[a.path].set_exception(OSError("corrupt"))
        await task
        with pytest.raises(HomeAssistantError, match="could not be read"):
            await browser.async_print()
        browser._coordinator._service_call_print_project_file.assert_not_called()

        files = list(browser.files)

        async def listing(function, client):
            return files

        browser._coordinator.hass.async_add_executor_job = listing
        await browser.async_refresh()
        assert browser.last_error is None
        assert "corrupt" in browser.mapping_attributes()["error"]

    asyncio.run(scenario())


def test_plate_without_filaments_refuses(browser):
    browser.plates = [ProjectPlate(1, ())]
    with pytest.raises(HomeAssistantError, match="no filaments"):
        asyncio.run(browser.async_print())
    browser._coordinator._service_call_print_project_file.assert_not_called()


@pytest.mark.parametrize("option, reloads", [
    (Options.PRINT_BED_LEVELING, False),
    (Options.PRINT_FLOW_CALI, False),
    (Options.PRINT_TIMELAPSE, False),
    (Options.CAMERA, True),
])
def test_print_options_do_not_rebuild_the_entities(option, reloads):
    coordinator = MagicMock()
    coordinator.config_entry.options = {}
    coordinator.get_option_enabled.return_value = True
    asyncio.run(BambuDataUpdateCoordinator.set_option_enabled(coordinator, option, False))
    coordinator._hass.config_entries.async_update_entry.assert_called_once()
    assert coordinator._printer_ready.called is reloads


@pytest.mark.parametrize("published", [True, False])
def test_print_project_file_returns_the_publish_result(published):
    coordinator = MagicMock()
    coordinator.config_entry.data = {"device_type": "A1"}
    coordinator.client.publish.return_value = published
    result = BambuDataUpdateCoordinator._service_call_print_project_file(coordinator, {
        "filepath": "Poop-Deflector(2).gcode.3mf", "plate": 1, "use_ams": True, "ams_mapping": "3"})
    assert result is published
    command = coordinator.client.publish.call_args.args[0]
    assert command["print"]["url"] == "file:///sdcard/Poop-Deflector(2).gcode.3mf"
    assert command["print"]["ams_mapping"] == [3]


@pytest.mark.parametrize("option", [Options.PRINT_BED_LEVELING, Options.PRINT_FLOW_CALI, Options.PRINT_TIMELAPSE])
def test_print_option_switch_writes_state_after_toggle(option):
    coordinator = MagicMock()
    coordinator.get_option_enabled.return_value = False
    coordinator.set_option_enabled = AsyncMock()
    switch = BambuLabPrintOptionSwitch(coordinator, MagicMock(), MagicMock(), option)
    switch.async_write_ha_state = MagicMock()

    asyncio.run(switch.async_turn_on())
    assert switch.is_on is True
    coordinator.set_option_enabled.assert_awaited_with(option, True)
    assert switch.async_write_ha_state.call_count == 1

    asyncio.run(switch.async_turn_off())
    assert switch.is_on is False
    coordinator.set_option_enabled.assert_awaited_with(option, False)
    assert switch.async_write_ha_state.call_count == 2
