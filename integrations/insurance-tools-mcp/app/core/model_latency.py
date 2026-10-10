"""有界的进程内单次模型耗时窗口；冷启动由调用方回落单次读超时。"""
from collections import OrderedDict, deque
from math import ceil
from threading import Lock

_samples: OrderedDict[tuple[str, str, str], deque[float]] = OrderedDict()
_lock = Lock()


def observe(key: tuple[str, str, str], elapsed_s: float) -> None:
    with _lock:
        window = _samples.setdefault(key, deque(maxlen=128))
        window.append(max(0, elapsed_s))
        _samples.move_to_end(key)
        if len(_samples) > 128:
            _samples.popitem(last=False)


def p95(key: tuple[str, str, str]) -> float:
    with _lock:
        values = sorted(_samples.get(key, ()))
    return values[ceil(len(values) * .95) - 1] if len(values) >= 5 else 0


def observe_first_output(key: tuple[str, str, str], elapsed_s: float) -> None:
    observe((key[0] + ":first", key[1], key[2]), elapsed_s)


def first_output_p95(key: tuple[str, str, str]) -> float:
    return p95((key[0] + ":first", key[1], key[2]))
