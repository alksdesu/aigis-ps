from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.request_fields import registered


IDENTITY = SimpleNamespace(player_id="local-player", user_key="local-key", method_key="local-method")


def native_request(profile, number, columns=(), *, tick=1000, platform_codes=(1, 1)):
    registry = ProtocolRegistry.load()
    wire = registry.by_registration[number]
    common = (Column("UI", "S", (profile.player_id,)), Column("UK", "S", (profile.user_key,)),
              Column("MK", "S", (profile.method_key,)), Column("CT", "I", (tick,)),
              Column("PFN", "S", ("dmm-r18",)), Column("PlatformCode", "I", tuple(platform_codes)),
              Column("GPVI", "S", ("unkown",)), Column("GPOT", "S", ("unkown",)))
    xml = prepare_request(dumps(Container(children=common + tuple(columns))), wire.st)
    return registry.decode(wire.route, encode_wire(xml, wire.request_key))


@pytest.mark.parametrize("number", [116, 137])
@pytest.mark.parametrize("codes", [(1, 1), (1,)])
def test_duplicating_writers_accept_native_and_single_platform_code(number, codes):
    fields = registered(native_request(IDENTITY, number, platform_codes=codes), {number}, ())
    assert len(fields["PlatformCode"]) == len(codes)


@pytest.mark.parametrize("number", [116, 137])
@pytest.mark.parametrize("codes", [(1, 2), (1, 1, 1)])
def test_duplicating_writers_reject_conflicting_or_extra_platform_codes(number, codes):
    with pytest.raises(ProtocolError) as error:
        registered(native_request(IDENTITY, number, platform_codes=codes), {number}, ())
    assert (error.value.code, error.value.status) == ("invalid_request_fields", 400)


@pytest.mark.parametrize("number", [54, 55])
def test_other_operations_keep_a_single_platform_code(number):
    assert len(registered(native_request(IDENTITY, number, platform_codes=(1,)), {number}, ())["PlatformCode"]) == 1
    with pytest.raises(ProtocolError) as error:
        registered(native_request(IDENTITY, number, platform_codes=(1, 1)), {number}, ())
    assert error.value.code == "invalid_request_fields"
