import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.startup_diagnostics import StartupDiagnostics, StartupLoad


def unavailable(code="bootstrap_state_unavailable", message="Injected unavailable state"):
    return ProtocolError(code, message, 503)


@pytest.fixture
def diagnostics():
    ticks = iter(f"2026-11-02T00:00:{second:02d}+00:00" for second in range(60))
    return StartupDiagnostics(clock=lambda: next(ticks))


def refuse(diagnostics, operation, error):
    with pytest.raises(type(error)):
        with diagnostics.load(operation):
            raise error


def test_clean_load_records_nothing(diagnostics):
    with diagnostics.load(34):
        pass
    assert diagnostics.status() == {"attention_required": False, "loads": {}}


def test_degraded_issues_are_deduplicated_per_load(diagnostics):
    with diagnostics.load(34):
        diagnostics.degrade(unavailable("invalid_player_rank", "Rank disagrees"))
        diagnostics.degrade(unavailable("invalid_player_rank", "Rank disagrees"))
        diagnostics.degrade(unavailable("energy_clock_invalid", "Clock marker"))
    entry = diagnostics.status()["loads"]["34"]
    assert entry == {"outcome": "degraded", "occurrences": 1,
                     "issues": [{"code": "invalid_player_rank", "message": "Rank disagrees"},
                                {"code": "energy_clock_invalid", "message": "Clock marker"}],
                     "first_seen": "2026-11-02T00:00:00+00:00", "last_seen": "2026-11-02T00:00:00+00:00"}


def test_refusal_keeps_issues_noticed_before_the_error(diagnostics):
    with pytest.raises(ProtocolError):
        with diagnostics.load(33):
            diagnostics.degrade(unavailable("login_cooking_invalid", "Ledger"))
            raise ProtocolError("profile_changed", "Raced", 409)
    entry = diagnostics.status()["loads"]["33"]
    assert entry["outcome"] == "refused"
    assert [issue["code"] for issue in entry["issues"]] == ["login_cooking_invalid", "profile_changed"]


def test_refusal_by_an_issue_already_degraded_records_it_once(diagnostics):
    with pytest.raises(ProtocolError):
        with diagnostics.load(137):
            diagnostics.degrade(unavailable("vote_catalog_unavailable", "Catalog"))
            diagnostics.degrade(unavailable("energy_clock_invalid", "Clock marker"))
            raise unavailable("vote_catalog_unavailable", "Catalog")
    entry = diagnostics.status()["loads"]["137"]
    assert entry["outcome"] == "refused"
    assert entry["issues"] == [{"code": "vote_catalog_unavailable", "message": "Catalog"},
                               {"code": "energy_clock_invalid", "message": "Clock marker"}]


def test_internal_errors_record_only_their_type(diagnostics):
    refuse(diagnostics, "heartbeat", KeyError("secret-user-key"))
    assert diagnostics.status()["loads"]["heartbeat"]["issues"] == [{"code": "internal_error", "message": "KeyError"}]


def test_repeats_count_and_changes_log_again(diagnostics, caplog):
    caplog.set_level(logging.INFO, logger="aigis_ps")
    for _ in range(3):
        refuse(diagnostics, 51, unavailable())
    entry = diagnostics.status()["loads"]["51"]
    assert (entry["occurrences"], entry["first_seen"], entry["last_seen"]) == (
        3, "2026-11-02T00:00:00+00:00", "2026-11-02T00:00:02+00:00")
    refuse(diagnostics, 51, unavailable("collection_rows_unavailable", "Other cause"))
    entry = diagnostics.status()["loads"]["51"]
    assert entry["occurrences"] == 1 and entry["issues"][0]["code"] == "collection_rows_unavailable"
    with diagnostics.load(51):
        pass
    assert diagnostics.status()["attention_required"] is False
    assert [record.levelno for record in caplog.records] == [logging.ERROR, logging.ERROR, logging.INFO]


def test_degrading_outside_a_load_only_logs(diagnostics, caplog):
    diagnostics.degrade(unavailable())
    assert diagnostics.status()["loads"] == {}
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_nested_loads_keep_their_own_issues(diagnostics):
    with diagnostics.load(34):
        with diagnostics.load(35):
            diagnostics.degrade(unavailable("inner", "Inner"))
        diagnostics.degrade(unavailable("outer", "Outer"))
    loads = diagnostics.status()["loads"]
    assert [issue["code"] for issue in loads["35"]["issues"]] == ["inner"]
    assert [issue["code"] for issue in loads["34"]["issues"]] == ["outer"]


def test_wrap_is_transparent_and_idempotent(diagnostics):
    def handler(envelope, store):
        diagnostics.degrade(unavailable())
        return envelope + store
    wrapped = diagnostics.wrap(35, handler)
    assert wrapped(b"a", b"b") == b"ab"
    assert diagnostics.wrap(35, wrapped) is wrapped
    assert diagnostics.wrap(36, wrapped) is not wrapped
    assert StartupDiagnostics().wrap(35, wrapped) is not wrapped
    assert diagnostics.status()["loads"]["35"]["outcome"] == "degraded"


def test_concurrent_loads_share_one_record():
    diagnostics = StartupDiagnostics(clock=lambda: "2026-11-02T00:00:00+00:00")
    workers = 16
    barrier = Barrier(workers)

    def load(_):
        barrier.wait()
        try:
            with diagnostics.load(137):
                raise unavailable()
        except ProtocolError:
            return True

    with ThreadPoolExecutor(workers) as pool:
        assert all(pool.map(load, range(workers)))
    assert diagnostics.status()["loads"]["137"]["occurrences"] == workers


def test_clock_must_be_callable():
    with pytest.raises(ValueError, match="clock"):
        StartupDiagnostics(clock="now")


def too_large(reply):
    raise ProtocolError("response_too_large", "Response exceeds the native receiver capacity", 500)


def test_serving_encodes_inside_the_load_so_reply_failures_stay_on_record(diagnostics):
    wrapped = diagnostics.wrap(51, lambda envelope, store: envelope + store)
    assert isinstance(wrapped, StartupLoad) and wrapped.operation == "51"
    refuse(diagnostics, 51, unavailable())
    for occurrences in (1, 2):
        with pytest.raises(ProtocolError):
            wrapped.serve(too_large, b"a", b"b")
        entry = diagnostics.status()["loads"]["51"]
        assert (entry["outcome"], entry["occurrences"]) == ("refused", occurrences)
        assert entry["issues"] == [{"code": "response_too_large", "message": "Response exceeds the native receiver capacity"}]
    assert wrapped.serve(lambda reply: (reply, "text/xml"), b"a", b"b") == (b"ab", "text/xml")
    assert diagnostics.status() == {"attention_required": False, "loads": {}}


def test_serving_keeps_degrades_noticed_before_the_reply_failed(diagnostics):
    def handler(envelope, store):
        diagnostics.degrade(unavailable("invalid_player_rank", "Rank disagrees"))
        return envelope

    wrapped = diagnostics.wrap(34, handler)
    assert wrapped.serve(len, b"abc", None) == 3
    assert diagnostics.status()["loads"]["34"]["outcome"] == "degraded"
    with pytest.raises(ProtocolError):
        wrapped.serve(too_large, b"abc", None)
    entry = diagnostics.status()["loads"]["34"]
    assert entry["outcome"] == "refused"
    assert [issue["code"] for issue in entry["issues"]] == ["invalid_player_rank", "response_too_large"]


def test_reply_failures_reach_every_registry_that_wraps_the_load():
    inner = StartupDiagnostics(clock=lambda: "2026-11-02T00:00:00+00:00")
    outer = StartupDiagnostics(clock=lambda: "2026-11-02T00:00:00+00:00")
    wrapped = outer.wrap(137, inner.wrap(137, lambda envelope, store: b"<DA/>"))
    with pytest.raises(ProtocolError):
        wrapped.serve(too_large, None, None)
    assert inner.status()["loads"]["137"]["outcome"] == outer.status()["loads"]["137"]["outcome"] == "refused"
    assert wrapped(None, None) == b"<DA/>"
    assert inner.status()["loads"] == outer.status()["loads"] == {}


def test_startup_loads_require_callable_handlers(diagnostics):
    with pytest.raises(ValueError, match="callable"):
        diagnostics.wrap(34, None)
