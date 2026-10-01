from OpenBench.progress.domain import TimeClass
from OpenBench.progress.options import thread_count

LTC_BASE_SECONDS = 20.0
VLTC_BASE_SECONDS = 120.0


def base_seconds(time_control: str) -> float | None:
    # Imported here because OpenBench.utils loads the views, and the workload views load this module
    from OpenBench.utils import TimeControl

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
