"""请求内工具停止真源；会话中的标记只供桥接、审计与旧状态读取。"""
from dataclasses import dataclass, field
from enum import Enum


class StopKind(str, Enum):
    FINISH = "finish"
    TERMINAL = "terminal"
    WAIT_USER = "wait_user"
    CAPACITY = "capacity"
    OVERFLOW = "overflow"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class TurnStop:
    kind: StopKind
    reason: str


@dataclass
class TurnControl:
    stop: TurnStop | None = None
    _state: dict | None = field(default=None, repr=False, compare=False)

    def bind(self, state: dict) -> None:
        self._state = state
        self.publish()

    def publish(self) -> None:
        if self._state is not None:
            self._state.setdefault('route', {})['turn_stop'] = (
                {'kind': self.stop.kind.value, 'reason': self.stop.reason} if self.stop else None)

    def set(self, kind: StopKind, reason: str) -> None:
        priority = {StopKind.TERMINAL: 0, StopKind.FINISH: 1, StopKind.WAIT_USER: 2,
                    StopKind.CAPACITY: 3, StopKind.OVERFLOW: 4, StopKind.EXHAUSTED: 5}
        if (self.stop is None or priority[kind] > priority[self.stop.kind]
                or self.stop.reason in {"finish_only", "recovering"} and self.stop.kind is kind
                or self.stop.kind is kind is StopKind.CAPACITY and reason == 'model_capacity_exceeded'):
            self.stop = TurnStop(kind, reason)
            self.publish()

    def release_terminal(self) -> None:
        if self.stop is not None and self.stop.kind is StopKind.TERMINAL:
            self.stop = None
            self.publish()
