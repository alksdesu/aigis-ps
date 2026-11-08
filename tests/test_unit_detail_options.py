import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.unit_detail_options import SMARTPHONE_IMAGE_COMMAND, smartphone_images_enabled
from test_image_aliases import command_table, encode
from test_quest_service import bundle


def read(commands):
    return lambda _: command_table([(SMARTPHONE_IMAGE_COMMAND, encode(command)) for command in commands])


def test_real_pc_command_enables_smartphone_images(bundle):
    assert smartphone_images_enabled(lambda name: bundle.native_cdn.assets.read(name)[0]) is True


@pytest.mark.parametrize("tag", ["DMM", "R18", "WIN", "DGP"])
def test_verified_platform_tags_match(tag):
    assert smartphone_images_enabled(read([f'IsPlatForm("{tag}");'])) is True


def test_missing_empty_and_other_platform_are_distinct():
    assert smartphone_images_enabled(read([])) is False
    assert smartphone_images_enabled(read([" "])) is True
    assert smartphone_images_enabled(read(['IsPlatForm("ANDROID");'])) is False


@pytest.mark.parametrize("commands", [["", ""], ['Unknown();'], ['IsPlatForm(GetPlatform());']])
def test_unknown_or_ambiguous_commands_fail_without_execution(commands):
    with pytest.raises(ProtocolError) as caught:
        smartphone_images_enabled(read(commands))
    assert caught.value.code == "unit_detail_options_invalid"
