import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.alxml import loads
from aigis_ps.bootstrap import QUEST_LIMITS
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_wire, prepare_request
from aigis_ps.public_tables import MASTER_IDS, MASTER_LIMITS
from aigis_ps.storage import Store
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.masterdata import MasterDataStore


# serve builds every service from the real resources and refuses to listen until startup is ready.
STARTUP_TIMEOUT_SECONDS = 180
REQUEST_TIMEOUT_SECONDS = 30
MASTER_ROUTES = {"master_cards": "CardData", "master_quests": "QuestTable"}
REQUESTS_PER_CYCLE, SUCCESSFUL_PER_CYCLE, FORBIDDEN_PER_CYCLE = 25, 23, 2


def request_body(values, *, typed=True):
    document = ET.Element("DA")
    for name, value in values.items():
        node = ET.SubElement(document, name, {"T": "I" if type(value) is int else "S"} if typed else {})
        if typed:
            node = ET.SubElement(node, "V")
        node.text = str(value)
    return ET.tostring(document, encoding="utf-8", short_empty_elements=False)


def master_ids(masters, table):
    document = loads(masters.get_xml(table), limits=MASTER_LIMITS)
    return next(column.values for column in document.children if column.name == MASTER_IDS[table])


def test_process_login_and_restart_keep_the_independent_save(tmp_path):
    project = Path(__file__).resolve().parents[1]
    assets = project.parent / "aigisr_exe"
    imported = project / "runtime/server/masterdata"
    if not (assets / "Aigis_r.exe").is_file() or not (imported / "active.json").is_file():
        pytest.skip("Local client resources and imported public masters are required for the process integration test")
    resource_state = ResourceLibrary(project / "runtime/resources", client_hash=MasterDataStore(imported).expected_hash).status()
    if not resource_state["ready"]:
        pytest.skip("The captured native resource version is required for heartbeat integration")
    registry = ProtocolRegistry.load()
    opener = build_opener(ProxyHandler({}))
    data = tmp_path / "save"
    shutil.copytree(imported, data / "masterdata")
    masters = MasterDataStore(data / "masterdata")
    quest_ids = set(master_ids(masters, "QuestTable"))
    identities = []
    statuses = []
    for cycle in range(2):
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        command = [sys.executable, "-m", "aigis_ps.cli", "serve", "--port", str(port),
                   "--data-dir", str(data), "--asset-dir", str(assets)]
        with (tmp_path / f"server-{cycle}.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=project, env=os.environ.copy(), stdout=log,
                                       stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
                while True:
                    assert process.poll() is None, "Local server exited before readiness"
                    try:
                        with opener.open(base + "/health", timeout=REQUEST_TIMEOUT_SECONDS) as response:
                            health = json.load(response)
                        assert health["service"] == "aigis-ps"
                        break
                    except (URLError, TimeoutError):
                        assert time.monotonic() < deadline, "Local server startup timed out"
                        time.sleep(0.1)
                readiness = health["readiness"]
                assert readiness["startup_ready"] is True
                assert readiness["masterdata"]["ready"] is True
                assert readiness["masterdata"]["snapshot"] == masters.status()["snapshot"]
                served_version = health["native_cdn"]["version"]
                assert health["native_cdn"]["ready"] is True and health["native_cdn"]["base_version"] == resource_state["version"]

                with opener.open(base + "/assets/RankUp.atb", timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    resource = response.read()
                if resource.startswith(b"ALL4"):
                    resource = decompress_all4(resource)
                assert parse_table(resource)["rows"][0]["CharismaMax"] == 60

                def exchange(profile, body):
                    xml = prepare_request(body, profile.st) if profile.st is not None else body
                    request = Request(base + "/api/" + profile.route,
                                      data=encode_wire(xml, profile.request_key),
                                      headers={"Content-Type": "application/octet-stream" if profile.request_key else "text/xml"})
                    with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                        if profile.response_kind == "integer_pair":
                            assert response.headers.get_content_type() == "text/plain"
                        return decode_wire(response.read(), profile.response_key)

                def operation(profile, body, **options):
                    content = exchange(profile, body)
                    return content if profile.response_kind == "integer_pair" else loads(content, **options)

                def named(name):
                    return next(item for item in registry.profiles if item.name == name)

                def direct(name, body, **options):
                    return operation(named(name), body, **options)

                def post(number, user_key, method_key="", player_id="1", fields=None):
                    return operation(registry.by_registration[number], request_body({"UI": player_id, "UK": user_key,
                                                                                     "MK": method_key, **(fields or {})}))

                title = operation(registry.by_registration[142], request_body({
                    "CT": 0, "PFN": "dmm-r18", "PlatformCode": 1, "UI": "", "UK": "", "MK": "",
                    "GPVI": "unkown", "GPOT": "unkown",
                }))
                assert [child.name for child in title.children] == ["SYSTEM", "CONFIG"]
                assert not title.children[1].children
                platform_login = operation(registry.by_registration[152], request_body({
                    "VI": "1", "OT": "local-only", "GPVI": "1", "GPOT": "local-only",
                }))
                li = next(node for node in platform_login.children if node.name == "LI")
                assert [column.name for column in li.children] == ["UK", "UI", "NN"]
                assert all(column.type.value == "S" and len(column.values) == 1 for column in li.children)
                identity_columns = {column.name: column.values[0] for column in li.children}
                player_id, user_key = identity_columns["UI"], identity_columns["UK"]
                assert player_id == "1" and user_key != "local-only"

                exchange_key = post(31, user_key, player_id=player_id)
                system = next(node for node in exchange_key.children if node.name == "SYSTEM")
                system_values = dict(zip(system.children[0].values, system.children[1].values))
                method_key = system_values["ExchangeKey"]
                login = post(114, user_key, method_key, player_id)
                lgi = next(node for node in login.children if node.name == "LGI")
                assert lgi.children[6].values == (user_key,)
                identities.append((player_id, user_key, method_key))

                game_info = direct("game_info", request_body({"Platform": "Windows"}))
                assert dict(zip(game_info.children[0].values, game_info.children[1].values))["Major"] == 2
                session_body = ET.Element("UI")
                session_body.text = player_id
                assert direct("session_flags", ET.tostring(session_body)) == b"0,0"

                game_param = post(34, user_key, method_key, player_id)
                assert game_param.children[0].values == ((1000 if cycle == 0 else 900),)
                for number in (33, 35, 36, 38, 42, 43, 44, 51, 129, 137):
                    response = post(number, user_key, method_key, player_id)
                    if number == 36:
                        configuration = {column.name: column.values[0] for column in response.children}
                        assert configuration["A4"] == (0 if cycle == 0 else 1)
                        assert configuration["A2"] is (cycle == 0)
                        assert configuration["C2"] == (0 if cycle == 0 else 80)
                heartbeat = direct("heartbeat", request_body({"Platform": "Windows", "UI": player_id, "UK": user_key}))
                asset_path = next(node for node in heartbeat.children if node.name == "AssetPath")
                assert asset_path.children[0].values == (served_version,)

                saved_tutorial = post(22, user_key, method_key, player_id, {"A4": 1})
                config_delta = next(node for node in saved_tutorial.children if node.name == "PGC")
                assert dict(zip(config_delta.children[0].values, config_delta.children[1].values))["A4"] == "1"
                post(21, user_key, method_key, player_id, {"A2": 0, "A3": 1, "C2": 80, "C3": 70,
                                                        "F9": 1, "FA": 0, "FD": 1})

                with pytest.raises(HTTPError) as failure:
                    post(31, "local-only", player_id=player_id)
                assert failure.value.code == 403
                failure.value.close()
                with pytest.raises(HTTPError) as failure:
                    direct("session_flags", b"<UI>other</UI>")
                assert failure.value.code == 403
                assert failure.value.read() == b"0,0"
                failure.value.close()

                plain_identity = request_body({"UI": player_id, "UK": user_key}, typed=False)
                quest_records = direct("quest_record", plain_identity, limits=QUEST_LIMITS)
                assert set(next(column.values for column in quest_records.children if column.name == "QuestID")) == quest_ids
                for name, table in MASTER_ROUTES.items():
                    assert exchange(named(name), b"<Rev>0</Rev>") == masters.get_xml(table)
                with opener.open(base + "/local/requests?limit=200", timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    records = json.load(response)["requests"]
                assert len(records) == (cycle + 1) * REQUESTS_PER_CYCLE
                assert sum(row["status"] == 200 for row in records) == (cycle + 1) * SUCCESSFUL_PER_CYCLE
                assert sum(row["status"] == 403 for row in records) == (cycle + 1) * FORBIDDEN_PER_CYCLE
                assert all(secret not in json.dumps(records) for secret in (user_key, method_key, "local-only"))
                statuses.append({"cycle": cycle, "requests": len(records),
                                 "successful_requests": (cycle + 1) * SUCCESSFUL_PER_CYCLE,
                                 "ready": readiness["startup_ready"]})
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        server_log = (tmp_path / f"server-{cycle}.log").read_text(encoding="utf-8", errors="replace")
        assert all(secret not in server_log for secret in (*identities[-1][1:], "local-only"))
        if cycle == 0:
            store = Store(data / "aigis.sqlite3")
            profile = store.profile()
            profile.state["tables"]["game_param"][0]["Money"] = 900
            store.update_state(profile.state, profile.revision)
    assert identities[0] == identities[1]
    (tmp_path / "result.json").write_text(json.dumps({"cycles": statuses, "identity_preserved": True}, indent=2))
