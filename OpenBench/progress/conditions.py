from OpenBench.progress.domain import TimeClass
from OpenBench.utils import TimeControl, extract_option

LTC_BASE_SECONDS = 20.0
VLTC_BASE_SECONDS = 120.0


def thread_count(options: str) -> int | None:
    raw = extract_option(options, 'Threads')
    try:
        return int(raw)
    except TypeError, ValueError:
        return None


def base_seconds(time_control: str) -> float | None:
    if TimeControl.control_type(time_control) != TimeControl.FISCHER:
        return None
    try:
        return float(TimeControl.control_base(time_control))
    except ValueError:
        return None


def time_class(dev_control: str, base_control: str, dev_options: str, base_options: str) -> TimeClass:
    threads = thread_count(dev_options)
    seconds = base_seconds(dev_control)
    if dev_control != base_control or threads is None or threads != thread_count(base_options) or seconds is None:
        return TimeClass.OTHER
    if threads > 1:
        return TimeClass.SMP
    if seconds < LTC_BASE_SECONDS:
        return TimeClass.STC
    if seconds < VLTC_BASE_SECONDS:
        return TimeClass.LTC
    return TimeClass.VLTC
