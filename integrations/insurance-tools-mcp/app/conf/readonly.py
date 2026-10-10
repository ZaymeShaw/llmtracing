"""Snapshot containers retain list/dict serialization while rejecting mutation.

Explicit deepcopy produces editable containers for model_copy(deep=True). These
are application read-only values, not a security boundary against base-class IO.
"""
from copy import deepcopy

from pydantic import BaseModel


def _readonly(*args, **kwargs):
    raise TypeError("Configuration snapshot is read-only; copy before modifying")


class ReadonlyDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __deepcopy__(self, memo):
        return {deepcopy(key, memo): deepcopy(value, memo) for key, value in self.items()}


class ReadonlyList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _readonly

    def __deepcopy__(self, memo):
        return [deepcopy(value, memo) for value in self]


def freeze(value):
    """Freeze a validated configuration once, without altering its source."""
    if isinstance(value, BaseModel):
        if not value.model_config.get("frozen"):
            raise TypeError("Snapshot configuration models must be frozen")
        return value.model_copy(update={key: freeze(getattr(value, key)) for key in type(value).model_fields})
    if isinstance(value, dict):
        return ReadonlyDict((key, freeze(item)) for key, item in value.items())
    if isinstance(value, list):
        return ReadonlyList(freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(value)
    return value
