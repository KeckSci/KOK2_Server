from __future__ import annotations

"""Runtime orchestration for one character's derived statistics."""

import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from shop_protocol import (
    EQUIPMENT_SLOT_WEAPON,
    find_shop_item_by_item_id,
    parse_attribute_field,
)
from stat_engine import (
    StatKey,
    StatModifier,
    StatSnapshot,
    calculate_stat_snapshot,
    normalize_stat_key,
)


ATTRIBUTE_SKILL_TO_PRIMARY_STAT: dict[int, StatKey] = {
    0x0005: StatKey.STRENGTH,
    0x0006: StatKey.DEXTERITY,
    0x0007: StatKey.WISDOM,
    0x0008: StatKey.CONSTITUTION,
}


@dataclass(frozen=True, slots=True)
class NormalAttackProfile:
    """Authoritative physical normal-attack range for one character.

    ``fixed_before_percent`` contains every non-range component: character
    base attack, passive/temporary effects, fixed equipment modifiers and
    fixed item attributes.  Only the equipped weapon template contributes a
    minimum/maximum range.
    """

    fixed_before_percent: int
    weapon_min: int
    weapon_max: int
    percent_bp: int
    displayed_attack: int

    @staticmethod
    def _apply_percent(value: int, percent_bp: int) -> int:
        return max(0, int(value) * (10_000 + int(percent_bp)) // 10_000)

    @property
    def attack_min(self) -> int:
        return self._apply_percent(
            self.fixed_before_percent + self.weapon_min, self.percent_bp
        )

    @property
    def attack_max(self) -> int:
        return self._apply_percent(
            self.fixed_before_percent + self.weapon_max, self.percent_bp
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "fixed_before_percent": int(self.fixed_before_percent),
            "weapon_min": int(self.weapon_min),
            "weapon_max": int(self.weapon_max),
            "percent_bp": int(self.percent_bp),
            "attack_min": int(self.attack_min),
            "attack_max": int(self.attack_max),
            "displayed_attack": int(self.displayed_attack),
        }


@dataclass(frozen=True, slots=True)
class TemporaryStatEffect:
    effect_id: str
    modifiers: tuple[StatModifier, ...]
    expires_at_monotonic: float | None = None

    def expired(self, now_monotonic: float) -> bool:
        return (
            self.expires_at_monotonic is not None
            and now_monotonic >= self.expires_at_monotonic
        )


class CharacterStatService:
    """Single authority for effective attributes of one online character.

    Database rows are treated as sources.  The effective snapshot is never
    written back to aggregate columns in ``characters``.
    """

    def __init__(self, database: Any, character_id: int, profession: int) -> None:
        self.database = database
        self.character_id = int(character_id)
        self.profession = int(profession)
        self._lock = threading.RLock()
        self._temporary_effects: dict[str, TemporaryStatEffect] = {}
        self._snapshot: StatSnapshot | None = None

    @property
    def snapshot(self) -> StatSnapshot:
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._calculate_locked()
            return self._snapshot

    def recalculate(self) -> tuple[StatSnapshot, tuple[str, ...]]:
        with self._lock:
            self._prune_expired_locked(time.monotonic())
            previous = self._snapshot
            current = self._calculate_locked()
            self._snapshot = current
            return current, current.changed_keys(previous)

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None

    def set_temporary_effect(
        self,
        effect_id: str,
        modifiers: Iterable[StatModifier],
        *,
        duration_seconds: float | None = None,
    ) -> tuple[StatSnapshot, tuple[str, ...]]:
        key = str(effect_id).strip()
        if not key:
            raise ValueError("effect_id must not be empty")
        normalized: list[StatModifier] = []
        for modifier in modifiers:
            normalized.append(StatModifier(
                stat=normalize_stat_key(modifier.stat),
                flat=int(modifier.flat),
                percent_bp=int(modifier.percent_bp),
                source_type="temporary",
                source_key=key,
                note=modifier.note,
            ))
        expires = None
        if duration_seconds is not None:
            duration = float(duration_seconds)
            if duration <= 0:
                raise ValueError("duration_seconds must be positive")
            expires = time.monotonic() + duration
        with self._lock:
            self._temporary_effects[key] = TemporaryStatEffect(
                effect_id=key,
                modifiers=tuple(normalized),
                expires_at_monotonic=expires,
            )
            return self.recalculate()

    def remove_temporary_effect(
        self, effect_id: str
    ) -> tuple[StatSnapshot, tuple[str, ...]]:
        with self._lock:
            self._temporary_effects.pop(str(effect_id), None)
            return self.recalculate()

    def prune_expired_effects(self) -> tuple[StatSnapshot, tuple[str, ...]] | None:
        with self._lock:
            removed = self._prune_expired_locked(time.monotonic())
            if not removed:
                return None
            previous = self._snapshot
            current = self._calculate_locked()
            self._snapshot = current
            return current, current.changed_keys(previous)

    def merge_into_character(self, character: Any) -> dict[str, Any]:
        data = dict(character)
        data.update(self.snapshot.as_dict())
        return data

    def normal_attack_profile(self) -> NormalAttackProfile:
        """Return the fixed character component plus the active weapon range.

        The character sheet keeps using the weapon average, while combat rolls
        only the weapon contribution.  With no active weapon, attack_min and
        attack_max are identical, so normal attacks are deterministic.
        """
        with self._lock:
            snapshot = self.snapshot
            breakdown = snapshot.breakdowns[StatKey.ATTACK_POWER.value]
            weapon_min = 0
            weapon_max = 0
            for equipped in self.database.list_equipped_items_for_character(
                self.character_id
            ):
                equipped_slot = int(equipped.get("equipped_slot", 0) or 0)
                if equipped_slot != EQUIPMENT_SLOT_WEAPON:
                    continue
                item = find_shop_item_by_item_id(int(equipped["item_id"]))
                if item is None:
                    continue
                weapon_min = max(0, int(item.attack_power))
                weapon_max = max(
                    weapon_min, int(item.attack_power_max or weapon_min)
                )
                break

            weapon_average = (weapon_min + weapon_max + 1) // 2
            fixed_before_percent = int(breakdown.before_percent) - weapon_average
            profile = NormalAttackProfile(
                fixed_before_percent=fixed_before_percent,
                weapon_min=weapon_min,
                weapon_max=weapon_max,
                percent_bp=int(breakdown.percent_bp_total),
                displayed_attack=int(snapshot.get(StatKey.ATTACK_POWER)),
            )
            # The profile must reproduce the value sent to the character sheet.
            displayed_from_profile = NormalAttackProfile._apply_percent(
                profile.fixed_before_percent + weapon_average, profile.percent_bp
            )
            if displayed_from_profile != profile.displayed_attack:
                raise RuntimeError(
                    "normal attack profile does not match effective attack: "
                    f"profile={displayed_from_profile}, "
                    f"snapshot={profile.displayed_attack}"
                )
            return profile

    def describe(self, stat: StatKey | str) -> dict[str, object]:
        key = normalize_stat_key(stat).value
        breakdown = self.snapshot.breakdowns[key]
        return {
            "stat": key,
            "base": breakdown.base,
            "flat_total": breakdown.flat_total,
            "percent_bp_total": breakdown.percent_bp_total,
            "before_percent": breakdown.before_percent,
            "effective": breakdown.effective,
            "contributions": [
                {
                    "source_type": item.source_type,
                    "source_key": item.source_key,
                    "flat": item.flat,
                    "percent_bp": item.percent_bp,
                    "note": item.note,
                }
                for item in breakdown.contributions
            ],
        }

    def _prune_expired_locked(self, now_monotonic: float) -> tuple[str, ...]:
        removed = tuple(
            key
            for key, effect in self._temporary_effects.items()
            if effect.expired(now_monotonic)
        )
        for key in removed:
            self._temporary_effects.pop(key, None)
        return removed

    def _calculate_locked(self) -> StatSnapshot:
        base = self.database.load_character_base_stats(self.character_id)
        if base is None:
            raise RuntimeError(
                f"Missing character_base_stats for character_id={self.character_id}"
            )
        modifiers: list[StatModifier] = []
        modifiers.extend(self._load_permanent_adjustments())
        modifiers.extend(self._load_skill_modifiers())
        modifiers.extend(self._load_equipment_modifiers())
        for effect in self._temporary_effects.values():
            modifiers.extend(effect.modifiers)
        return calculate_stat_snapshot(dict(base), modifiers)

    def _load_permanent_adjustments(self) -> list[StatModifier]:
        result: list[StatModifier] = []
        for row in self.database.list_character_stat_adjustments(self.character_id):
            result.append(StatModifier(
                stat=normalize_stat_key(row["stat_key"]),
                flat=int(row["flat_value"]),
                percent_bp=int(row["percent_bp"]),
                source_type="permanent",
                source_key=str(row["source_key"]),
                note=str(row["note"] or ""),
            ))
        return result

    def _load_skill_modifiers(self) -> list[StatModifier]:
        result: list[StatModifier] = []
        skill_levels = self.database.load_character_skill_levels(self.character_id)
        for skill_id, level in skill_levels.items():
            level = max(0, int(level))
            primary_stat = ATTRIBUTE_SKILL_TO_PRIMARY_STAT.get(int(skill_id))
            if primary_stat is not None and level:
                result.append(StatModifier(
                    stat=primary_stat,
                    flat=level,
                    source_type="skill",
                    source_key=f"skill:{int(skill_id):04X}:primary",
                    note="attribute level",
                ))
            for effect in self.database.load_skill_effective_modifiers(
                self.profession, int(skill_id), level
            ):
                result.append(StatModifier(
                    stat=normalize_stat_key(effect["stat_key"]),
                    flat=int(effect["flat_value"]),
                    percent_bp=int(effect["percent_bp"]),
                    source_type="skill",
                    source_key=f"skill:{int(skill_id):04X}:{effect['source_key']}",
                    note=str(effect.get("note", "")),
                ))
        return result

    def _load_equipment_modifiers(self) -> list[StatModifier]:
        result: list[StatModifier] = []
        equipped_rows = self.database.list_equipped_items_for_character(
            self.character_id
        )
        for equipped in equipped_rows:
            item_id = int(equipped["item_id"])
            item = find_shop_item_by_item_id(item_id)
            if item is None:
                continue
            instance_attrs: list[int] = []
            for index in range(1, 6):
                instance_attrs.extend(
                    parse_attribute_field(equipped.get(f"instance_attr{index}", ""))
                )
            for stat_name, value in item.stat_bonuses_for_instance(
                instance_attrs=instance_attrs
            ).items():
                if not value:
                    continue
                result.append(StatModifier(
                    stat=normalize_stat_key(stat_name),
                    flat=int(value),
                    source_type="equipment",
                    source_key=f"instance:{int(equipped['instance_id'])}",
                    note=f"template:{item_id}",
                ))
            for row in self.database.list_equipment_stat_modifiers(item_id):
                result.append(StatModifier(
                    stat=normalize_stat_key(row["stat_key"]),
                    flat=int(row["flat_value"]),
                    percent_bp=int(row["percent_bp"]),
                    source_type="equipment",
                    source_key=f"instance:{int(equipped['instance_id'])}:configured",
                    note=str(row["note"] or ""),
                ))
        return result
