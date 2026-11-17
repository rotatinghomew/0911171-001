from dataclasses import dataclass


@dataclass(frozen=True)
class Conversion:
    event_id: str
    subject_id: str
    occurred_at: int
    retracted: bool = False


class EventLedger:
    def __init__(self) -> None:
        self._events: dict[str, Conversion] = {}

    def record(self, event: Conversion) -> None:
        self._events[event.event_id] = event

    def through(self, cutoff: int) -> list[Conversion]:
        return sorted(
            (event for event in self._events.values() if event.occurred_at <= cutoff),
            key=lambda event: (event.occurred_at, event.event_id),
        )
