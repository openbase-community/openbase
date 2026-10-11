from types import SimpleNamespace

from openbase_coder_cli.services.cloud_registration_watch import (
    RETRY_INITIAL_SECONDS,
    CloudRegistrationWatch,
)

OK = SimpleNamespace(ok=True, supported=True)
FAIL = SimpleNamespace(ok=False, supported=True, error="boom", status_code=500)


def _watch(results, sub="1"):
    state = {"now": 0.0, "sub": sub, "calls": 0}

    def register():
        state["calls"] += 1
        return results.pop(0) if results else OK

    watch = CloudRegistrationWatch(
        owner_sub=lambda: state["sub"],
        register=register,
        clock=lambda: state["now"],
    )
    return watch, state


def test_first_tick_registers_then_idles_until_refresh():
    watch, state = _watch([])
    assert watch.tick() == "first_report"
    state["now"] = 60
    assert watch.tick() == "idle"
    state["now"] = 3600
    assert watch.tick() == "refresh"
    assert state["calls"] == 2


def test_account_switch_reregisters_immediately():
    watch, state = _watch([])
    watch.tick()
    state["sub"] = ""
    state["now"] = 30
    assert watch.tick() == "idle"
    state["sub"] = "2"
    state["now"] = 60
    assert watch.tick() == "first_report"
    state["sub"] = "3"
    assert watch.tick() == "account_changed"
    assert state["calls"] == 3


def test_logged_out_never_registers():
    watch, state = _watch([], sub="")
    assert watch.tick() == "idle"
    assert state["calls"] == 0


def test_failures_retry_with_backoff():
    watch, state = _watch([FAIL, FAIL])
    assert watch.tick() == "first_report"
    state["now"] = RETRY_INITIAL_SECONDS - 1
    assert watch.tick() == "idle"
    state["now"] = RETRY_INITIAL_SECONDS
    assert watch.tick() == "retry"
    state["now"] += RETRY_INITIAL_SECONDS
    assert watch.tick() == "idle"
    state["now"] += RETRY_INITIAL_SECONDS
    assert watch.tick() == "retry"
    state["now"] += 60
    assert watch.tick() == "idle"
    assert state["calls"] == 3
