from __future__ import annotations

"""Pure character-stat calculation.

This module deliberately knows nothing about SQLite, sockets, packets, shops,
or combat.  It accepts a base stat block plus explicit modifiers and returns an
immutable effective snapshot with a source breakdown.
"""

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping


class StatKey(str, Enum):
    STRENGTH = "strength"
    DEXTERITY = "dexterity"
    WISDOM = "wisdom"
    CONSTITUTION = "constitution"

    ATTACK_POWER = "attack_power"
    DEFENSE_POWER = "defense_power"
    MAGIC_ATTACK_POWER = "magic_attack_power"

    EARTH_RESISTANCE = "earth_resistance"
    WATER_RESISTANCE = "water_resistance"
    FIRE_RESISTANCE = "fire_resistance"
    WIND_RESISTANCE = "wind_resistance"
    LIGHT_RESISTANCE = "light_resistance"
    DARK_RESISTANCE = "dark_resistance"

    MAX_HP = "max_hp"
    MAX_MP = "max_mp"
    MAX_SP = "max_sp"

    HP_REGEN_PER_SECOND = "hp_regen_per_second"
    MP_REGEN_PER_SECOND = "mp_regen_per_second"
    SP_REGEN_PER_SECOND = "sp_regen_per_second"

    NORMAL_ATTACK_INTERVAL_MS = "normal_attack_interval_ms"


ALL_STAT_KEYS: tuple[StatKey, ...] = tuple(StatKey)
STAT_KEY_NAMES: frozenset[str] = frozenset(stat.value for stat in ALL_STAT_KEYS)

# All currently modelled stats must remain non-negative.  Attack interval has a
# practical lower floor so a malformed modifier cannot create a zero-time loop.
STAT_MINIMUMS: Mapping[StatKey, int] = MappingProxyType({
    **{stat: 0 for stat in ALL_STAT_KEYS},
    StatKey.NORMAL_ATTACK_INTERVAL_MS: 50,
})


@dataclass(frozen=True, slots=True)
class StatModifier:
    stat: StatKey
    flat: int = 0
    percent_bp: int = 0
    source_type: str = "unknown"
    source_key: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.source_type:
            raise ValueError("source_type must not be empty")


@dataclass(frozen=True, slots=True)
class StatContribution:
    source_type: str
    source_key: str
    flat: int
    percent_bp: int
    note: str = ""


@dataclass(frozen=True, slots=True)
class StatBreakdown:
    stat: StatKey
    base: int
    flat_total: int
    percent_bp_total: int
    before_percent: int
    effective: int
    contributions: tuple[StatContribution, ...]


@dataclass(frozen=True, slots=True)
class StatSnapshot:
    values: Mapping[str, int]
    breakdowns: Mapping[str, StatBreakdown]

    def get(self, stat: StatKey | str, default: int = 0) -> int:
        key = stat.value if isinstance(stat, StatKey) else str(stat)
        return int(self.values.get(key, default))

    def as_dict(self) -> dict[str, int]:
        return dict(self.values)

    def changed_keys(self, previous: "StatSnapshot | None") -> tuple[str, ...]:
        if previous is None:
            return tuple(self.values)
        return tuple(
            key
            for key, value in self.values.items()
            if int(previous.values.get(key, 0)) != int(value)
        )


def normalize_stat_key(value: StatKey | str) -> StatKey:
    if isinstance(value, StatKey):
        return value
    try:
        return StatKey(str(value))
    except ValueError as error:
        raise ValueError(f"Unknown stat key: {value!r}") from error


def _apply_basis_points(value: int, percent_bp: int) -> int:
    """Apply signed basis points with deterministic integer floor semantics."""
    numerator = int(value) * (10_000 + int(percent_bp))
    # Python // floors negative values.  Effective stats are clamped to a
    # non-negative floor afterwards, which is the desired deterministic rule.
    return numerator // 10_000


def calculate_stat_snapshot(
    base_values: Mapping[StatKey | str, int],
    modifiers: Iterable[StatModifier],
) -> StatSnapshot:
    normalized_base: dict[StatKey, int] = {
        stat: int(base_values.get(stat, base_values.get(stat.value, 0)))
        for stat in ALL_STAT_KEYS
    }
    grouped: dict[StatKey, list[StatModifier]] = {stat: [] for stat in ALL_STAT_KEYS}
    for modifier in modifiers:
        grouped[normalize_stat_key(modifier.stat)].append(modifier)

    values: dict[str, int] = {}
    breakdowns: dict[str, StatBreakdown] = {}
    for stat in ALL_STAT_KEYS:
        stat_modifiers = grouped[stat]
        flat_total = sum(int(item.flat) for item in stat_modifiers)
        percent_total = sum(int(item.percent_bp) for item in stat_modifiers)
        before_percent = int(normalized_base[stat]) + flat_total
        effective = _apply_basis_points(before_percent, percent_total)
        effective = max(int(STAT_MINIMUMS[stat]), effective)
        contributions = tuple(
            StatContribution(
                source_type=item.source_type,
                source_key=item.source_key,
                flat=int(item.flat),
                percent_bp=int(item.percent_bp),
                note=item.note,
            )
            for item in stat_modifiers
        )
        values[stat.value] = effective
        breakdowns[stat.value] = StatBreakdown(
            stat=stat,
            base=int(normalized_base[stat]),
            flat_total=flat_total,
            percent_bp_total=percent_total,
            before_percent=before_percent,
            effective=effective,
            contributions=contributions,
        )

    return StatSnapshot(
        values=MappingProxyType(values),
        breakdowns=MappingProxyType(breakdowns),
    )
