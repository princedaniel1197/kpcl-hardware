"""Cold start-up profile for the simulated 210 MW unit.

WHAT THIS IS. A signal generator shaped to look like a unit start-up: keyframed
values per phase, smoothly interpolated, with noise. It produces numbers for
everything downstream to acquire, archive, compress and calculate on.

WHAT THIS IS NOT. A physical model. Nothing here is derived from a steam table,
an energy balance or a turbine characteristic. A value being plausible is not
the same as a value being computed, and this module only claims the former.
Plant physics enters at Stage 7 through published equations (§486) — never by
reading a number out of this file and calling it thermodynamics.

The operating targets are representative of an Indian 210 MW coal-fired unit
(3000 rpm, ~147 kg/cm2 main steam, ~537 degC, ~8.5% auxiliary consumption).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    """Start-up phases, in the order a cold unit passes through them."""

    OFFLINE = "OFFLINE"
    LIGHT_UP = "LIGHT_UP"
    PRESSURE_RAISE = "PRESSURE_RAISE"
    TURBINE_ROLLING = "TURBINE_ROLLING"
    SYNCHRONISATION = "SYNCHRONISATION"
    LOADING = "LOADING"
    STEADY = "STEADY"


PHASE_ORDER: tuple[Phase, ...] = tuple(Phase)

# Share of the configured start-up duration spent in each phase. STEADY is the
# terminal state: once reached the unit stays there indefinitely, so its share
# describes only how long the approach to steady takes.
PHASE_FRACTION: dict[Phase, float] = {
    Phase.OFFLINE: 0.03,
    Phase.LIGHT_UP: 0.12,
    Phase.PRESSURE_RAISE: 0.30,
    Phase.TURBINE_ROLLING: 0.18,
    Phase.SYNCHRONISATION: 0.05,
    Phase.LOADING: 0.22,
    Phase.STEADY: 0.10,
}

# Value at t=0, then the value reached at the END of each phase, in PHASE_ORDER.
# Eight numbers per tag: one initial, seven phase ends.
KEYFRAMES: dict[str, tuple[float, ...]] = {
    #                    cold  OFFLINE  LIGHTUP  PRESS   ROLL    SYNC    LOAD   STEADY
    "U1_TURB_SPEED":     (0.0,    0.0,     0.0,    0.0, 3000.0, 3000.0, 3000.0, 3000.0),
    "U1_DRUM_PRESS":     (1.5,    1.8,    18.0,  155.0,  157.0,  157.0,  160.0,  160.0),
    "U1_MS_TEMP":        (38.0,   40.0,  180.0,  480.0,  520.0,  530.0,  537.0,  537.0),
    "U1_MS_PRESS":       (0.8,    1.0,    12.0,  140.0,  145.0,  145.0,  147.0,  147.0),
    "U1_FEEDWATER_FLOW": (0.0,    0.0,    60.0,  210.0,  240.0,  260.0,  670.0,  670.0),
    "U1_COAL_FLOW":      (0.0,    0.0,    12.0,   55.0,   65.0,   72.0,  142.0,  142.0),
    "U1_AUX_POWER":      (1.2,    1.5,     6.0,   11.0,   13.0,   14.0,   17.5,   17.5),
    "U1_CONDENSER_VAC":  (0.0,    0.0,    80.0,  600.0,  660.0,  665.0,  668.0,  668.0),
    "U1_GEN_STATOR_TEMP":(30.0,  30.0,    32.0,   35.0,   42.0,   48.0,   78.0,   78.0),
    "U1_BEARING_VIB":    (0.0,    0.0,     0.0,    0.2,    2.8,    2.2,    2.4,    2.4),
    # U1_MW is not keyframed: it is gated on the breaker. See mw_value().
}

# Standard deviation of the noise added to each tag, in engineering units.
# A transmitter is never perfectly steady; a tag that is makes Stage 6's frozen
# value detection untestable.
NOISE_SIGMA: dict[str, float] = {
    "U1_MW": 0.35,
    "U1_TURB_SPEED": 1.2,
    "U1_DRUM_PRESS": 0.30,
    "U1_MS_TEMP": 0.80,
    "U1_MS_PRESS": 0.25,
    "U1_FEEDWATER_FLOW": 2.50,
    "U1_COAL_FLOW": 0.60,
    "U1_AUX_POWER": 0.08,
    "U1_CONDENSER_VAC": 1.10,
    "U1_GEN_STATOR_TEMP": 0.25,
    "U1_BEARING_VIB": 0.18,
}

# Fraction into SYNCHRONISATION at which the breaker closes. Before this the
# machine is spinning at rated speed carrying no load; after it, load exists.
BREAKER_CLOSE_AT = 0.30

# Load carried the instant the breaker closes, and at the end of loading.
MW_AT_SYNC = 12.0
MW_AT_FULL_LOAD = 210.0


def _smoothstep(f: float) -> float:
    """Ease-in/ease-out on [0,1]. Plant variables do not change direction in
    steps; a linear ramp with a corner at each end is the giveaway of a
    generator that was written in five minutes."""
    f = min(max(f, 0.0), 1.0)
    return f * f * (3.0 - 2.0 * f)


@dataclass(frozen=True)
class PlantState:
    """Everything the simulator knows at one instant."""

    elapsed_s: float
    phase: Phase
    phase_fraction: float
    analogues: dict[str, float]
    digitals: dict[str, bool]


class Plant:
    """Drives the unit through a cold start-up and then holds it steady.

    `startup_seconds` scales the whole sequence: 90 for a demonstration, 10800
    for something closer to the real thing. Only the timings scale; the values
    reached and the order they are reached in do not.
    """

    def __init__(self, startup_seconds: float = 90.0, seed: int | None = None) -> None:
        if startup_seconds <= 0:
            raise ValueError("startup_seconds must be positive")
        self.startup_seconds = float(startup_seconds)
        self._rng = random.Random(seed)

        # Absolute end time of each phase, in seconds from t=0.
        self._phase_end: dict[Phase, float] = {}
        running = 0.0
        for phase in PHASE_ORDER:
            running += PHASE_FRACTION[phase] * self.startup_seconds
            self._phase_end[phase] = running
        self.startup_complete_s = running

    # -- phase -------------------------------------------------------------

    def phase_at(self, elapsed_s: float) -> tuple[Phase, float]:
        """Return the phase at `elapsed_s` and how far through it we are."""
        start = 0.0
        for phase in PHASE_ORDER:
            end = self._phase_end[phase]
            if elapsed_s < end:
                span = end - start
                return phase, (elapsed_s - start) / span if span > 0 else 1.0
            start = end
        return Phase.STEADY, 1.0

    def _phase_index(self, phase: Phase) -> int:
        return PHASE_ORDER.index(phase)

    # -- values ------------------------------------------------------------

    def _interpolate(self, tag: str, phase: Phase, f: float) -> float:
        frames = KEYFRAMES[tag]
        i = self._phase_index(phase)
        return frames[i] + (frames[i + 1] - frames[i]) * _smoothstep(f)

    def _bearing_vib(self, phase: Phase, f: float, base: float) -> float:
        """Vibration peaks as the shaft passes through a critical speed on the
        way to 3000 rpm, then settles. Shaped, not computed."""
        if phase is Phase.TURBINE_ROLLING:
            # A bump centred at roughly 40% of the run-up.
            return base + 2.2 * math.exp(-(((f - 0.40) / 0.11) ** 2))
        return base

    def _mw(self, phase: Phase, f: float, breaker_closed: bool) -> float:
        """Generation is gated on the breaker: an open breaker means zero MW,
        not a small number. The digital and the analogue must never disagree."""
        if not breaker_closed:
            return 0.0
        if phase is Phase.SYNCHRONISATION:
            g = (f - BREAKER_CLOSE_AT) / (1.0 - BREAKER_CLOSE_AT)
            return MW_AT_SYNC * _smoothstep(g)
        if phase is Phase.LOADING:
            return MW_AT_SYNC + (MW_AT_FULL_LOAD - MW_AT_SYNC) * _smoothstep(f)
        return MW_AT_FULL_LOAD

    def _digitals(self, phase: Phase, f: float) -> dict[str, bool]:
        i = self._phase_index(phase)
        lightup = i >= self._phase_index(Phase.LIGHT_UP)
        rolling = phase is Phase.TURBINE_ROLLING
        if phase is Phase.SYNCHRONISATION:
            breaker = f >= BREAKER_CLOSE_AT
        else:
            breaker = i > self._phase_index(Phase.SYNCHRONISATION)
        return {
            "U1_BOILER_LIGHTUP": lightup,
            "U1_TURB_ROLLING": rolling,
            "U1_BREAKER_CLOSED": breaker,
        }

    def state_at(self, elapsed_s: float, *, noise: bool = True) -> PlantState:
        """The full state of the unit at `elapsed_s` seconds after cold start."""
        phase, f = self.phase_at(elapsed_s)
        digitals = self._digitals(phase, f)

        analogues: dict[str, float] = {}
        for tag in KEYFRAMES:
            value = self._interpolate(tag, phase, f)
            if tag == "U1_BEARING_VIB":
                value = self._bearing_vib(phase, f, value)
            analogues[tag] = value
        analogues["U1_MW"] = self._mw(phase, f, digitals["U1_BREAKER_CLOSED"])

        if noise:
            for tag, value in analogues.items():
                sigma = NOISE_SIGMA.get(tag, 0.0)
                # A quantity that is genuinely zero stays exactly zero. A shaft
                # at standstill reads 0 rpm, and an open breaker means 0 MW, not
                # a small positive number: noise on a hard zero is a false
                # signal, and Stage 6 and Stage 8 would key off it.
                if sigma and value > 0.0:
                    analogues[tag] = max(0.0, value + self._rng.gauss(0.0, sigma))

        return PlantState(elapsed_s, phase, f, analogues, digitals)
