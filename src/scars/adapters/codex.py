from scars.model import HookEvent


def parse(event_name: str, payload: dict[str, object]) -> HookEvent:
    raise NotImplementedError("B4 adapters")
