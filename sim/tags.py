"""Tag definitions for the simulated 210 MW thermal unit.

Every tag carries engineering units and an EURange, because the tender requires
it (§436) and because a range is what a downstream range check is checked
against (§384). The EURange here is the *instrument span*, not the operating
band: a transmitter calibrated 0-250 MW reads 0-250 MW whatever the unit is
doing. Stage 6 range checks are against this span.

The operating values these tags are driven to are representative of an Indian
210 MW coal-fired unit. They are chosen to be plausible, not derived: this
module shapes signals, it does not model thermodynamics. No physical claim is
made by any number in this file. Plant physics enters the project at Stage 7,
through published equations, or it does not enter at all. (§486)
"""

from __future__ import annotations

from dataclasses import dataclass

from asyncua import ua


@dataclass(frozen=True)
class TagSpec:
    """One point in the simulated DCS."""

    name: str
    description: str
    unit: str                      # engineering unit, as displayed
    variant_type: ua.VariantType
    eu_low: float | None = None    # instrument span low  (analogues only)
    eu_high: float | None = None   # instrument span high (analogues only)

    @property
    def is_digital(self) -> bool:
        return self.variant_type == ua.VariantType.Boolean


ANALOGUES: tuple[TagSpec, ...] = (
    TagSpec("U1_MW", "Unit 1 gross generation", "MW",
            ua.VariantType.Double, 0.0, 250.0),
    TagSpec("U1_TURB_SPEED", "Turbine shaft speed", "rpm",
            ua.VariantType.Double, 0.0, 3600.0),
    TagSpec("U1_DRUM_PRESS", "Boiler drum pressure", "kg/cm2",
            ua.VariantType.Double, 0.0, 200.0),
    TagSpec("U1_MS_TEMP", "Main steam temperature", "degC",
            ua.VariantType.Double, 0.0, 600.0),
    TagSpec("U1_MS_PRESS", "Main steam pressure", "kg/cm2",
            ua.VariantType.Double, 0.0, 200.0),
    TagSpec("U1_FEEDWATER_FLOW", "Feedwater flow", "t/h",
            ua.VariantType.Double, 0.0, 800.0),
    TagSpec("U1_COAL_FLOW", "Total coal flow", "t/h",
            ua.VariantType.Double, 0.0, 200.0),
    TagSpec("U1_AUX_POWER", "Unit auxiliary power consumption", "MW",
            ua.VariantType.Double, 0.0, 30.0),
    TagSpec("U1_CONDENSER_VAC", "Condenser vacuum", "mmHg",
            ua.VariantType.Double, 0.0, 760.0),
    TagSpec("U1_GEN_STATOR_TEMP", "Generator stator winding temperature", "degC",
            ua.VariantType.Double, 0.0, 150.0),
    TagSpec("U1_BEARING_VIB", "Turbine bearing vibration velocity", "mm/s",
            ua.VariantType.Double, 0.0, 25.0),
)

# Digitals are state, not sampled analogues (§440). They are written when they
# change and at no other time; see sim/server.py.
DIGITALS: tuple[TagSpec, ...] = (
    TagSpec("U1_BOILER_LIGHTUP", "Boiler light-up permissive established", "",
            ua.VariantType.Boolean),
    TagSpec("U1_TURB_ROLLING", "Turbine rolling", "",
            ua.VariantType.Boolean),
    TagSpec("U1_BREAKER_CLOSED", "Generator breaker closed", "",
            ua.VariantType.Boolean),
)

ALL_TAGS: tuple[TagSpec, ...] = ANALOGUES + DIGITALS

BY_NAME: dict[str, TagSpec] = {t.name: t for t in ALL_TAGS}
