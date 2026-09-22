"""Forced quality overrides for the simulator.

This is how a fault is induced during a demonstration without touching the
simulator's logic: an operator forces a tag to BadDeviceFailure over the control
API, and every subsequent write of that tag carries that StatusCode.

Quality is the numeric OPC UA StatusCode throughout this project — never a
boolean, never a string (§318). The names below are a convenience for the HTTP
API only; what travels with the value is the number.

Note what is NOT here: there is no way to force a *value*. The control API
changes quality and nothing else. A simulator that let you write values would be
a control path, and this project does not have one anywhere. (§303, §315)
"""

from __future__ import annotations

from asyncua import ua

# The three states the build plan names for Stage 1.
FORCEABLE: dict[str, int] = {
    "Good": int(ua.StatusCodes.Good),
    "BadDeviceFailure": int(ua.StatusCodes.BadDeviceFailure),
    "UncertainSensorNotAccurate": int(ua.StatusCodes.UncertainSensorNotAccurate),
}


class QualityOverrides:
    """Which tags are currently forced, and to what."""

    def __init__(self) -> None:
        self._forced: dict[str, int] = {}

    def force(self, tag: str, quality_name: str) -> int:
        """Force `tag` to a named quality. Forcing to Good clears the override
        rather than pinning it, so a restored tag behaves exactly as an
        untouched one."""
        if quality_name not in FORCEABLE:
            raise ValueError(
                f"{quality_name!r} is not forceable; expected one of "
                f"{', '.join(FORCEABLE)}"
            )
        code = FORCEABLE[quality_name]
        if quality_name == "Good":
            self._forced.pop(tag, None)
        else:
            self._forced[tag] = code
        return code

    def clear(self, tag: str) -> None:
        self._forced.pop(tag, None)

    def clear_all(self) -> None:
        self._forced.clear()

    def status_for(self, tag: str) -> int:
        """The StatusCode this tag should be written with right now."""
        return self._forced.get(tag, int(ua.StatusCodes.Good))

    def is_forced(self, tag: str) -> bool:
        return tag in self._forced

    @property
    def forced(self) -> dict[str, int]:
        return dict(self._forced)
