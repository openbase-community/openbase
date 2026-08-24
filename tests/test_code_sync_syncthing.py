from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from openbase_coder_cli.code_sync import syncthing
from openbase_coder_cli.services.definitions import SERVICES
from openbase_coder_cli.sync_config import SyncFolder

SELF_ID = "SELFAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
PEER_ID = "PEERAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"


def _render(tmp_path: Path) -> ET.Element:
    content = syncthing.render_config_xml(
        self_device_id=SELF_ID,
        self_name="laptop",
        api_key="test-api-key",
        peers=[
            syncthing.PeerDevice(
                device_id=PEER_ID,
                name="Mac mini",
                address=syncthing.peer_address("mini.tail1234.ts.net."),
            )
        ],
        folders=[SyncFolder(relpath="Projects/demo")],
        home=tmp_path,
        versions_dir=tmp_path / "sync-versions",
    )
    return ET.fromstring(content)


def test_render_config_pins_transport_and_disables_discovery(
    tmp_path: Path,
) -> None:
    root = _render(tmp_path)

    options = root.find("options")
    assert options.findtext("globalAnnounceEnabled") == "false"
    assert options.findtext("localAnnounceEnabled") == "false"
    assert options.findtext("relaysEnabled") == "false"
    assert options.findtext("natEnabled") == "false"
    assert options.findtext("crashReportingEnabled") == "false"
    assert options.findtext("urAccepted") == "-1"
    assert options.findtext("listenAddress") == "tcp://0.0.0.0:22000"

    gui = root.find("gui")
    assert gui.findtext("address") == "127.0.0.1:8385"  # not the user's 8384
    assert gui.findtext("apikey") == "test-api-key"

    devices = root.findall("device")
    by_id = {device.get("id"): device for device in devices}
    assert by_id[SELF_ID].findtext("address") == "dynamic"
    assert by_id[PEER_ID].findtext("address") == "tcp://mini.tail1234.ts.net:22000"


def test_render_config_folder_shape(tmp_path: Path) -> None:
    root = _render(tmp_path)
    folder = SyncFolder(relpath="Projects/demo")

    element = root.find("folder")
    assert element.get("id") == folder.folder_id
    assert element.get("label") == "Projects/demo"
    assert element.get("path") == str(tmp_path / "Projects" / "demo")
    assert element.get("type") == "sendreceive"
    assert element.get("fsWatcherEnabled") == "true"
    device_ids = [device.get("id") for device in element.findall("device")]
    assert device_ids == [SELF_ID, PEER_ID]

    versioning = element.find("versioning")
    assert versioning.get("type") == "staggered"
    params = {param.get("key"): param.get("val") for param in versioning}
    assert params["maxAge"] == str(30 * 24 * 3600)
    assert params["versionsPath"] == str(tmp_path / "sync-versions" / folder.folder_id)


def test_render_config_sets_absolute_disk_free_floors(tmp_path: Path) -> None:
    root = _render(tmp_path)

    folder_floor = root.find("folder").find("minDiskFree")
    assert folder_floor.get("unit") == "MB"
    assert folder_floor.text == str(syncthing.MIN_DISK_FREE_MB)

    home_floor = root.find("options").find("minHomeDiskFree")
    assert home_floor.get("unit") == "MB"
    assert home_floor.text == str(syncthing.MIN_DISK_FREE_MB)


def test_write_config_keeps_api_key_stable(tmp_path: Path) -> None:
    def _write() -> str | None:
        syncthing.write_config(
            self_device_id=SELF_ID,
            self_name="laptop",
            peers=[],
            folders=[],
            config_dir=tmp_path,
            home=tmp_path,
        )
        return syncthing.existing_api_key(tmp_path)

    first_key = _write()
    assert first_key
    assert _write() == first_key


def test_write_config_preserves_receiveonly_folder_type(tmp_path: Path) -> None:
    """A re-render must not stomp the lease's receive-only flip."""
    folder = SyncFolder(relpath="Projects/demo")

    def _write() -> None:
        syncthing.write_config(
            self_device_id=SELF_ID,
            self_name="laptop",
            peers=[],
            folders=[folder],
            config_dir=tmp_path,
            home=tmp_path,
        )

    _write()
    config_path = tmp_path / syncthing.CONFIG_XML_FILENAME
    content = config_path.read_text(encoding="utf-8").replace(
        'type="sendreceive"', 'type="receiveonly"', 1
    )
    config_path.write_text(content, encoding="utf-8")
    assert syncthing.existing_folder_types(tmp_path) == {
        folder.folder_id: "receiveonly"
    }

    _write()
    root = ET.parse(config_path).getroot()
    assert root.find("folder").get("type") == "receiveonly"


def test_device_id_parsing_and_storage(tmp_path: Path) -> None:
    output = f"Device ID: {SELF_ID}\n"
    assert syncthing._device_id_from_output(output) == SELF_ID
    assert syncthing._device_id_from_output("no id here") is None

    (tmp_path / "device-id").write_text(SELF_ID + "\n", encoding="utf-8")
    assert syncthing.stored_device_id(tmp_path) == SELF_ID
    assert syncthing.stored_device_id(tmp_path / "missing") is None


def test_code_sync_service_definition() -> None:
    from openbase_coder_cli.services import runners

    service = next(svc for svc in SERVICES if svc.name == "code-sync")
    assert service.install_by_default is False
    assert service.command_template == "code-sync"

    build, binary_keys = runners.RUNNERS["code-sync"]
    argv, _env = build({}, {"syncthing": "/opt/homebrew/bin/syncthing"})
    assert binary_keys == ("syncthing",)
    assert argv[:3] == ["/opt/homebrew/bin/syncthing", "serve", "--home"]
    assert "--no-browser" in argv
    assert "--no-restart" in argv


def test_device_id_parsing_v2_output() -> None:
    text = (
        "2026-07-10 16:08:01 INF Calculated device ID "
        f"(device={SELF_ID} log.pkg=github)"
    )
    assert syncthing._device_id_from_output(text) == SELF_ID
