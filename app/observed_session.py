"""Keep one operator-requested observation session alive until explicit stop.

Transport and Excel retries live in their respective owners. Unexpected
calculation/configuration faults permanently remove actuation authority from
this logical session; they never turn a reconnect into an implicit write retry.
"""
from dataclasses import replace

from session import run_ui_session
from resilient_adapter import ResilientUiAdapter


def _fault_diagnostic(error):
    parts = []
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        parts.append(type(error).__module__ + "." + type(error).__name__ + ": " + str(error))
        error = error.__cause__ or error.__context__
    return " <- ".join(parts)


def run_observed_session(options, stop_event, emit, *, adapter_factory=ResilientUiAdapter,
                         runner=run_ui_session, retry_delays=(2, 5, 15, 30, 60)):
    adapter = adapter_factory()
    set_fault_callback = getattr(adapter, "set_fault_callback", None)
    if set_fault_callback is not None:
        set_fault_callback(lambda error: emit("actuation_barrier", _fault_diagnostic(error)))
    failures = 0
    current = options
    try:
        while not stop_event.is_set():
            try:
                runner(current, stop_event, emit, lambda: adapter, preserve_adapter=True)
                if stop_event.is_set():
                    break
                raise RuntimeError("관측 작업이 예상치 않게 종료되었습니다.")
            except Exception as error:
                if stop_event.is_set():
                    break
                # There is no proof that an arbitrary algorithm/protocol fault
                # is safe to re-arm. Retain the evidence and reconnect reads.
                reason = "관측 복구 중 · IR 자동 적용 보류: " + str(error)
                adapter.freeze_control(error)
                emit("observer_hold", reason)
                current = replace(options, mode="monitor")
                # Requests and active/pending images belong to the logical
                # session, not to a transport or calculation attempt.
                object.__setattr__(current, "_target_temperature_setting", options._target_temperature_setting)
                object.__setattr__(current, "_slice_dataset_setting", options._slice_dataset_setting)
                delay = retry_delays[min(failures, len(retry_delays) - 1)]
                failures += 1
                if stop_event.wait(delay):
                    break
    finally:
        adapter.disconnect()
