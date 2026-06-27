from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

from merchant_pricing import (
    calculate_purchase_price as _calculate_purchase_price,
    calculate_recycle_price as _calculate_recycle_price,
    calculate_repair_price as _calculate_repair_price,
)

# Confirmed shop protocol opcodes.
OP_GAME_NPC_UI_TEXT = 0x0006
OP_GAME_SHOP_ITEM_LIST = 0x000B
CLIENT_OP_NPC_INTERACT = 0x8009
CLIENT_OP_SHOP_CATEGORY = 0x8018
CLIENT_OP_SHOP_TRANSACTION = 0x8019
# Compatibility alias retained for older imports/tools.
CLIENT_OP_SHOP_BUY = CLIENT_OP_SHOP_TRANSACTION

# 0x8019 action values recovered from the client shop UI.
SHOP_ACTION_PURCHASE = 2
SHOP_ACTION_RECYCLE = 3
SHOP_ACTION_REPAIR = 4
SHOP_ACTION_REPAIR_ALT = 5

BACKPACK_DEFAULT_SLOT_COUNT = 40
BACKPACK_MAX_SLOT_COUNT = 60
NPC_UI_TEXT_SUBTYPE_SHOP = 0x02

SHOP_BUY_RATE = int(os.environ.get("SHOP_BUY_RATE", "1000"))
SHOP_SELL_RATE = int(os.environ.get("SHOP_SELL_RATE", "500"))

# The third value in the 0x0006 subtype-2 tail is not an opaque mode number.
# UIXShop::FUN_00476250 treats it as a four-bit visibility mask for the four
# shop-function buttons.  The confirmed first three modes are:
#   bit 0 (1): purchase
#   bit 1 (2): recycle / sell to merchant
#   bit 2 (4): repair
# The fourth client mode is intentionally left disabled until its semantics
# are confirmed.  Therefore ordinary equipment merchants use 1|2|4 = 7.
SHOP_FUNCTION_PURCHASE = 0x01
SHOP_FUNCTION_RECYCLE = 0x02
SHOP_FUNCTION_REPAIR = 0x04
SHOP_FUNCTION_UNKNOWN_4 = 0x08
SHOP_FUNCTION_MASK = int(os.environ.get("SHOP_FUNCTION_MASK", "7")) & 0x0F
# Compatibility alias for older imports.  It now means "function bitmask".
SHOP_TAIL_MODE = SHOP_FUNCTION_MASK
SHOP_TAIL_RESERVED = int(os.environ.get("SHOP_TAIL_RESERVED", "0"))


def calculate_purchase_price(base_price: int) -> int:
    return _calculate_purchase_price(base_price, SHOP_BUY_RATE)


def calculate_recycle_price(base_price: int) -> int:
    return _calculate_recycle_price(base_price, SHOP_SELL_RATE)


def calculate_repair_price(
    base_price: int,
    current_durability: int,
    maximum_durability: int,
) -> int:
    """Return floor((missing/max) * actual NPC sale price * 20%)."""
    return _calculate_repair_price(
        base_price,
        current_durability,
        maximum_durability,
        SHOP_BUY_RATE,
    )

# Tooltip attribute IDs confirmed from language.xml and client behavior.
ITEM_ATTR_DEFENCE = 1
ITEM_ATTR_SPEED = 2
ITEM_ATTR_REQUIRED_STRENGTH = 4
ITEM_ATTR_REQUIRED_DEXTERITY = 5
ITEM_ATTR_REQUIRED_INTELLIGENCE = 6
ITEM_ATTR_REQUIRED_CONSTITUTION = 7
ITEM_ATTR_REQUIRED_CLASS = 9
ITEM_ATTR_REQUIRED_LEVEL = 12
ITEM_ATTR_ATTACK_POWER = 14
ITEM_ATTR_ATTACK_RANGE = 15
ITEM_ATTR_DURABILITY = 16
ITEM_ATTR_MAGIC_POWER = 30
ITEM_ATTR_BONUS_STRENGTH = 104
ITEM_ATTR_BONUS_DEXTERITY = 105
ITEM_ATTR_BONUS_INTELLIGENCE = 106
ITEM_ATTR_BONUS_CONSTITUTION = 107

# Equipment-part/type codes from language.xml <mfpart>.
MFPART_ALL_WEAPONS = 100
MFPART_SWORD = 101
MFPART_HAMMER = 102
MFPART_AXE = 103
MFPART_THRUST = 104
MFPART_WAND = 105
MFPART_RANGED = 106
MFPART_BOOK = 107
MFPART_ALL_ARMOUR = 170
MFPART_WRIST_SHIELD = 171
MFPART_SORCERY_GEM = 172
MFPART_SORCERY_CRYSTAL = 173
MFPART_HAND_SHIELD = 174
MFPART_HELMET = 201
MFPART_CHEST_ARMOUR = 211
MFPART_PANTS = 231
MFPART_SHOES = 241
MFPART_GLOVES = 251
MFPART_ALL_ACCESSORIES = 300
MFPART_RING = 301
MFPART_NECKLACE = 302
MFPART_EARRING = 303
MFPART_BELT = 311

# Client equipment destinations. Weapon/shield set 2 uses the same visible
# client slots; the active set is selected server-side by characters.active_weapon_set.
EQUIPMENT_SLOT_HELMET = 200
EQUIPMENT_SLOT_CHEST = 201
EQUIPMENT_SLOT_BELT = 202
EQUIPMENT_SLOT_PANTS = 203
EQUIPMENT_SLOT_SHOES = 204
EQUIPMENT_SLOT_GLOVES = 205
EQUIPMENT_SLOT_RIGHT_RING = 206
EQUIPMENT_SLOT_LEFT_RING = 207
EQUIPMENT_SLOT_NECKLACE = 208
EQUIPMENT_SLOT_EARRING = 209
EQUIPMENT_SLOT_WEAPON = 210
EQUIPMENT_SLOT_SHIELD = 211
# Client slots 212/213 are not used by the ordinary equipment compatibility
# switch.  The three badge slots are a separate group at 214..216.
EQUIPMENT_SLOT_BADGE_1 = 214
EQUIPMENT_SLOT_BADGE_2 = 215
EQUIPMENT_SLOT_BADGE_3 = 216

EQUIPMENT_SLOT_NAMES = {
    EQUIPMENT_SLOT_WEAPON: "weapon",
    EQUIPMENT_SLOT_SHIELD: "shield",
    EQUIPMENT_SLOT_CHEST: "armour",
    EQUIPMENT_SLOT_PANTS: "pants",
    EQUIPMENT_SLOT_SHOES: "shoes",
    EQUIPMENT_SLOT_GLOVES: "gloves",
    EQUIPMENT_SLOT_HELMET: "helmet",
    EQUIPMENT_SLOT_BELT: "belt",
    EQUIPMENT_SLOT_RIGHT_RING: "right_ring",
    EQUIPMENT_SLOT_LEFT_RING: "left_ring",
    EQUIPMENT_SLOT_EARRING: "earring",
    EQUIPMENT_SLOT_NECKLACE: "necklace",
    EQUIPMENT_SLOT_BADGE_1: "badge1",
    EQUIPMENT_SLOT_BADGE_2: "badge2",
    EQUIPMENT_SLOT_BADGE_3: "badge3",
}
EQUIPMENT_SLOTS = frozenset(EQUIPMENT_SLOT_NAMES)
DUAL_SET_SLOTS = frozenset({EQUIPMENT_SLOT_WEAPON, EQUIPMENT_SLOT_SHIELD})

_SINGLE_SLOT_BY_MFPART = {
    MFPART_HELMET: EQUIPMENT_SLOT_HELMET,
    MFPART_CHEST_ARMOUR: EQUIPMENT_SLOT_CHEST,
    MFPART_BELT: EQUIPMENT_SLOT_BELT,
    MFPART_PANTS: EQUIPMENT_SLOT_PANTS,
    MFPART_SHOES: EQUIPMENT_SLOT_SHOES,
    MFPART_GLOVES: EQUIPMENT_SLOT_GLOVES,
    MFPART_NECKLACE: EQUIPMENT_SLOT_NECKLACE,
    MFPART_EARRING: EQUIPMENT_SLOT_EARRING,
}
_WEAPON_MFPARTS = frozenset({
    MFPART_ALL_WEAPONS, MFPART_SWORD, MFPART_HAMMER, MFPART_AXE,
    MFPART_THRUST, MFPART_WAND, MFPART_RANGED, MFPART_BOOK,
})
_SHIELD_MFPARTS = frozenset({
    MFPART_WRIST_SHIELD, MFPART_SORCERY_GEM,
    MFPART_SORCERY_CRYSTAL, MFPART_HAND_SHIELD,
})


def equipment_slots_for_mfpart(mfpart: int) -> frozenset[int]:
    parsed = int(mfpart) & 0x0FFF
    single = _SINGLE_SLOT_BY_MFPART.get(parsed)
    if single is not None:
        return frozenset({single})
    if parsed in _WEAPON_MFPARTS:
        return frozenset({EQUIPMENT_SLOT_WEAPON})
    if parsed in _SHIELD_MFPARTS:
        return frozenset({EQUIPMENT_SLOT_SHIELD})
    if parsed == MFPART_RING:
        return frozenset({EQUIPMENT_SLOT_RIGHT_RING, EQUIPMENT_SLOT_LEFT_RING})
    return frozenset()


def equipment_slot_for_mfpart(mfpart: int) -> int | None:
    slots = equipment_slots_for_mfpart(mfpart)
    return next(iter(slots)) if len(slots) == 1 else None


def is_equipment_slot(slot: int) -> bool:
    return int(slot) in EQUIPMENT_SLOTS


CLASS_WARRIOR = 1000
CLASS_MAGE = 2000
CLASS_CLERIC = 3000

CLASS_DISPLAY_NAMES = {
    CLASS_WARRIOR: "战士",
    CLASS_MAGE: "法师",
    CLASS_CLERIC: "牧师",
}
TOOLTIP_REQUIREMENT_MET_COLOR = "EFEFEF"
TOOLTIP_REQUIREMENT_FAILED_COLOR = "F01E1E"


def normalize_class_id(value: int) -> int:
    parsed = int(value)
    return {1: CLASS_WARRIOR, 2: CLASS_MAGE, 3: CLASS_CLERIC, 4: CLASS_CLERIC}.get(
        parsed, parsed
    )


def _row_value(row: Any, key: str, default: int = 0) -> int:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return int(default)
    return int(default if value is None else value)


def _row_text(row: Any, key: str, default: str = "") -> str:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else str(value)


def _u16(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"uint16 out of range: {value}")
    return value.to_bytes(2, "big")


def _u32(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"uint32 out of range: {value}")
    return value.to_bytes(4, "big")


def _cstr_cp936(value: str) -> bytes:
    return value.encode("cp936", errors="replace") + b"\x00"


def _u32_array(values: tuple[int, ...]) -> bytes:
    return _u32(len(values)) + b"".join(_u32(value) for value in values)


def _pack_u16_pair(high: int, low: int) -> int:
    if not 0 <= high <= 0xFFFF or not 0 <= low <= 0xFFFF:
        raise ValueError(f"durability pair out of range: {high}/{low}")
    return (high << 16) | low


def parse_attribute_field(value: Any) -> tuple[int, ...]:
    """Decode one flexible unique/special attribute field.

    Accepted examples:
      "116:3"
      "[116, 3]"
      '{"key": 116, "value": 3}'
      "[[116,3],[104,1]]"
    Empty/invalid values are ignored instead of breaking login.
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        obj = value
    elif isinstance(value, dict):
        obj = value
    else:
        text = str(value).strip()
        if not text:
            return ()
        if ":" in text and not text.startswith(("[", "{")):
            left, right = text.split(":", 1)
            try:
                return (int(left, 0), int(right, 0))
            except ValueError:
                return ()
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return ()

    pairs: list[int] = []
    if isinstance(obj, dict):
        if "key" in obj and "value" in obj:
            try:
                return (int(obj["key"]), int(obj["value"]))
            except (TypeError, ValueError):
                return ()
        for key, val in obj.items():
            try:
                pairs.extend((int(key), int(val)))
            except (TypeError, ValueError):
                continue
        return tuple(pairs)
    if isinstance(obj, (list, tuple)):
        if len(obj) == 2 and not isinstance(obj[0], (list, tuple, dict)):
            try:
                return (int(obj[0]), int(obj[1]))
            except (TypeError, ValueError):
                return ()
        for entry in obj:
            pairs.extend(parse_attribute_field(entry))
    return tuple(pairs)


def merge_attribute_pairs(*arrays: Iterable[int]) -> tuple[int, ...]:
    """Merge key/value arrays; later arrays override earlier values."""
    order: list[int] = []
    values: dict[int, int] = {}
    for array in arrays:
        data = list(array)
        for index in range(0, len(data) - 1, 2):
            key = int(data[index])
            val = int(data[index + 1])
            if key not in values:
                order.append(key)
            values[key] = val
    result: list[int] = []
    for key in order:
        result.extend((key, values[key]))
    return tuple(result)


@dataclass(frozen=True)
class ShopItem:
    """Database-backed equipment template used by shop, bag and equipment."""

    item_id: int
    name: str
    price: int
    equipment_part: int
    attack_power: int = 0
    attack_power_max: int = 0
    magic_power: int = 0
    defence: int = 0
    speed: int = 0
    required_strength: int = 0
    required_dexterity: int = 0
    required_intelligence: int = 0
    required_constitution: int = 0
    required_class: int = 0
    client_class_limit_code: int = 0
    required_level: int = 0
    attack_range: int = 0
    attack_range_max: int = 0
    durability: int = 0
    base_special_attrs: tuple[int, ...] = ()
    bonus_strength: int = 0
    bonus_dexterity: int = 0
    bonus_intelligence: int = 0
    bonus_constitution: int = 0
    # 0x000B field2 -> item+0x174 is the client move quantity / badge value.
    # Original non-stackable equipment uses 0: the item object still exists,
    # but the UI does not draw a stack-count badge.  field5 -> item+0x184 is
    # the maximum stack size and remains 1 for non-stackable equipment.
    field5: int = 1
    resource_key_override: str = ""

    @classmethod
    def from_equip_row(cls, row: Any) -> "ShopItem":
        return cls(
            item_id=_row_value(row, "id"),
            resource_key_override=_row_text(row, "resource_key"),
            name=_row_text(row, "name"),
            equipment_part=_row_value(row, "equip_type"),
            attack_power=_row_value(row, "attack_power"),
            attack_power_max=_row_value(row, "attack_power_max"),
            magic_power=_row_value(row, "magic_power"),
            defence=_row_value(row, "defense_power"),
            speed=_row_value(row, "speed"),
            required_strength=_row_value(row, "required_strength"),
            required_dexterity=_row_value(row, "required_dexterity"),
            required_intelligence=_row_value(row, "required_intelligence"),
            required_constitution=_row_value(row, "required_constitution"),
            required_class=_row_value(row, "required_profession"),
            client_class_limit_code=_row_value(row, "client_class_limit_code"),
            required_level=_row_value(row, "required_level"),
            attack_range=_row_value(row, "attack_range"),
            attack_range_max=_row_value(row, "attack_range_max"),
            durability=_row_value(row, "durability"),
            price=_row_value(row, "price"),
            base_special_attrs=(
                (_row_value(row, "base_special_attr_key"), _row_value(row, "base_special_attr_value"))
                if _row_value(row, "base_special_attr_key", 0) else ()
            ),
        )

    @property
    def resource_key(self) -> str:
        text = self.resource_key_override.strip().lower()
        return text or f"{self.item_id:06x}"

    @property
    def allowed_equipment_slots(self) -> frozenset[int]:
        return equipment_slots_for_mfpart(self.equipment_part)

    def tooltip_attrs_for_instance(
        self,
        *,
        current_durability: int | None = None,
        instance_attrs: Iterable[int] = (),
        include_required_class: bool = True,
    ) -> tuple[int, ...]:
        values: list[int] = []
        if self.defence:
            values.extend((ITEM_ATTR_DEFENCE, int(self.defence)))
        if self.speed:
            values.extend((ITEM_ATTR_SPEED, int(self.speed)))
        if self.required_strength:
            values.extend((ITEM_ATTR_REQUIRED_STRENGTH, int(self.required_strength)))
        if self.required_dexterity:
            values.extend((ITEM_ATTR_REQUIRED_DEXTERITY, int(self.required_dexterity)))
        if self.required_intelligence:
            values.extend((ITEM_ATTR_REQUIRED_INTELLIGENCE, int(self.required_intelligence)))
        if self.required_constitution:
            values.extend((ITEM_ATTR_REQUIRED_CONSTITUTION, int(self.required_constitution)))
        # itemattr 9 expects a real clazz id: 1000=Warrior, 2000=Mage,
        # 3000=Priest.  Values >=10001 are interpreted as clazz+10000
        # (the client appends the localized "系" suffix).  V17.15's
        # 1000001 value was invalid and became clazz 990001.
        if include_required_class and self.required_class:
            values.extend((ITEM_ATTR_REQUIRED_CLASS, int(self.required_class)))
        if self.required_level:
            values.extend((ITEM_ATTR_REQUIRED_LEVEL, int(self.required_level)))
        if self.attack_power:
            attack_min = int(self.attack_power)
            attack_max = max(attack_min, int(self.attack_power_max or attack_min))
            attack_value = (
                _pack_u16_pair(attack_min, attack_max - attack_min)
                if attack_max > attack_min else attack_min
            )
            values.extend((ITEM_ATTR_ATTACK_POWER, attack_value))
        if self.attack_range:
            range_min = int(self.attack_range)
            range_max = max(range_min, int(self.attack_range_max or range_min))
            range_value = (
                _pack_u16_pair(range_min, range_max)
                if range_max > range_min else range_min
            )
            values.extend((ITEM_ATTR_ATTACK_RANGE, range_value))
        max_durability = max(0, int(self.durability))
        current = max_durability if current_durability is None else max(0, int(current_durability))
        current = min(current, max_durability) if max_durability else current
        values.extend((ITEM_ATTR_DURABILITY, _pack_u16_pair(current, max_durability)))
        if self.magic_power:
            values.extend((ITEM_ATTR_MAGIC_POWER, int(self.magic_power)))
        if self.bonus_strength:
            values.extend((ITEM_ATTR_BONUS_STRENGTH, int(self.bonus_strength)))
        if self.bonus_dexterity:
            values.extend((ITEM_ATTR_BONUS_DEXTERITY, int(self.bonus_dexterity)))
        if self.bonus_intelligence:
            values.extend((ITEM_ATTR_BONUS_INTELLIGENCE, int(self.bonus_intelligence)))
        if self.bonus_constitution:
            values.extend((ITEM_ATTR_BONUS_CONSTITUTION, int(self.bonus_constitution)))
        return merge_attribute_pairs(values, self.base_special_attrs, instance_attrs)

    @property
    def tooltip_attrs(self) -> tuple[int, ...]:
        return self.tooltip_attrs_for_instance()

    def display_text_for_viewer(self, viewer_profession: int | None = None) -> str:
        """Build field3 as name plus optional rich-text requirement description.

        cBaseItem::FUN_004e8ba0 splits field3 at the first newline: the first
        line remains the item name and the remaining text becomes the tooltip's
        extra-description field.  The generic itemattr renderer never marks
        key 9 red, so profession status is rendered through this native rich
        text path when the viewer profession is known.
        """
        if not self.required_class or viewer_profession is None:
            return self.name
        required = normalize_class_id(self.required_class)
        viewer = normalize_class_id(viewer_profession)
        class_name = CLASS_DISPLAY_NAMES.get(required, str(required))
        color = (
            TOOLTIP_REQUIREMENT_MET_COLOR
            if viewer == required
            else TOOLTIP_REQUIREMENT_FAILED_COLOR
        )
        line = f"<font color=#{color}>职业需求：{class_name}</font>"
        return f"{self.name}\n{line}"

    def requirement_failures(self, character: Any) -> tuple[str, ...]:
        failures: list[str] = []
        level = _row_value(character, "level", 1)
        profession = normalize_class_id(_row_value(character, "profession", 0))
        strength = _row_value(character, "strength", 0)
        dexterity = _row_value(character, "dexterity", 0)
        intelligence = _row_value(character, "wisdom", 0)
        constitution = _row_value(character, "constitution", 0)
        if level < self.required_level:
            failures.append(f"level {level} < {self.required_level}")
        if self.required_class and profession != normalize_class_id(self.required_class):
            failures.append(
                f"profession {profession} != required {normalize_class_id(self.required_class)}"
            )
        if strength < self.required_strength:
            failures.append(f"strength {strength} < {self.required_strength}")
        if dexterity < self.required_dexterity:
            failures.append(f"dexterity {dexterity} < {self.required_dexterity}")
        if intelligence < self.required_intelligence:
            failures.append(f"intelligence {intelligence} < {self.required_intelligence}")
        if constitution < self.required_constitution:
            failures.append(f"constitution {constitution} < {self.required_constitution}")
        return tuple(failures)

    @property
    def stat_bonuses(self) -> dict[str, int]:
        return self.stat_bonuses_for_instance()

    def stat_bonuses_for_instance(
        self,
        *,
        instance_attrs: Iterable[int] = (),
    ) -> dict[str, int]:
        attack_min = int(self.attack_power)
        attack_max = max(attack_min, int(self.attack_power_max or attack_min))
        totals = {
            "attack_power": (attack_min + attack_max + 1) // 2,
            "magic_attack_power": int(self.magic_power),
            "defense_power": int(self.defence),
            "strength": int(self.bonus_strength),
            "dexterity": int(self.bonus_dexterity),
            "wisdom": int(self.bonus_intelligence),
            "constitution": int(self.bonus_constitution),
        }
        attr_to_stat = {
            104: "strength", 105: "dexterity", 106: "wisdom", 107: "constitution",
            115: "attack_power", 116: "defense_power", 123: "magic_attack_power",
        }
        attrs = merge_attribute_pairs(self.base_special_attrs, instance_attrs)
        for index in range(0, len(attrs) - 1, 2):
            stat_name = attr_to_stat.get(int(attrs[index]))
            if stat_name is not None:
                totals[stat_name] += int(attrs[index + 1])
        return totals

    def build_record(
        self,
        category_id: int,
        stack_count: int | None = None,
        *,
        viewer_profession: int | None = None,
        client_sort_type: int | None = None,
    ) -> bytes:
        # Equipment is an independent instance, not a stack.  The original
        # client uses field2=0 for such objects: it suppresses the quantity
        # badge and later echoes 0 as the 0x800B move quantity.  Existence is
        # carried by the object record / stable key, not by this stack value.
        record_stack_count = 0
        attrs = self.tooltip_attrs_for_instance(
            include_required_class=viewer_profession is None,
        )
        return b"".join((
            _cstr_cp936(self.resource_key),
            _u16(category_id),
            _u16(record_stack_count),
            _cstr_cp936(self.display_text_for_viewer(viewer_profession)),
            # The shop object may use a database-controlled type solely for
            # the client's shop-list sorting.  Owned inventory/equipment
            # records always keep the real equip.equip_type below.
            _u16(
                self.equipment_part
                if client_sort_type is None
                else int(client_sort_type)
            ),
            # Maximum stack size 1 prevents merging.  Badge suppression is
            # controlled independently by field2=0 above.
            _u16(int(self.field5)),
            _u32(self.price),
            _u32(self.item_id),
            _u32_array(attrs),
        ))

    def _build_owned_record(
        self,
        *,
        instance_id: int,
        location_slot: int,
        stack_count: int = 0,
        current_durability: int | None = None,
        instance_attrs: Iterable[int] = (),
        viewer_profession: int | None = None,
    ) -> bytes:
        instance_key = f"inv{int(instance_id):08x}"
        attrs = self.tooltip_attrs_for_instance(
            current_durability=current_durability,
            instance_attrs=instance_attrs,
            include_required_class=viewer_profession is None,
        )
        return b"".join((
            _cstr_cp936(instance_key),
            _u16(int(location_slot)),
            _u16(0),
            _cstr_cp936(self.display_text_for_viewer(viewer_profession)),
            _u16(self.equipment_part),
            # Maximum stack size 1 prevents merging.  Badge suppression is
            # controlled independently by field2=0 above.
            _u16(int(self.field5)),
            _u32(self.price),
            _u32(self.item_id),
            _u32_array(attrs),
        ))

    def build_inventory_record(self, *, instance_id: int, bag_slot: int, stack_count: int = 0,
                               current_durability: int | None = None,
                               instance_attrs: Iterable[int] = (),
                               viewer_profession: int | None = None) -> bytes:
        if not 0 <= int(bag_slot) < 190:
            raise ValueError(f"backpack slot must be in 0..189: {bag_slot}")
        return self._build_owned_record(
            instance_id=instance_id, location_slot=bag_slot, stack_count=stack_count,
            current_durability=current_durability, instance_attrs=instance_attrs,
            viewer_profession=viewer_profession,
        )

    def build_equipment_record(self, *, instance_id: int, equipped_slot: int, stack_count: int = 0,
                               current_durability: int | None = None,
                               instance_attrs: Iterable[int] = (),
                               viewer_profession: int | None = None) -> bytes:
        parsed_slot = int(equipped_slot)
        if parsed_slot not in self.allowed_equipment_slots:
            raise ValueError(
                f"item {self.name!r} accepts slots {sorted(self.allowed_equipment_slots)}, got {parsed_slot}"
            )
        return self._build_owned_record(
            instance_id=instance_id, location_slot=parsed_slot, stack_count=stack_count,
            current_durability=current_durability,
            instance_attrs=instance_attrs,
            viewer_profession=viewer_profession,
        )


@dataclass
class ShopDefinition:
    key: str
    npc_being_names: frozenset[str]
    default_title: str
    category_label: str
    category_id: int
    items: tuple[ShopItem, ...]
    # Optional 0x000B field4 override for each item.  The legacy client sorts
    # shop rows by this field instead of preserving packet order.  None means
    # use the real equip.equip_type.
    client_sort_types: tuple[int | None, ...] = ()

    def build_open_payload(self, window_title: str | None = None) -> bytes:
        title = window_title or self.default_title
        text = (
            f"{title}\n1\n{self.category_label}\n{self.category_id}\n"
            f"{SHOP_BUY_RATE},{SHOP_SELL_RATE},{SHOP_FUNCTION_MASK},{SHOP_TAIL_RESERVED}\n"
        )
        return bytes((NPC_UI_TEXT_SUBTYPE_SHOP,)) + _cstr_cp936(text)

    def build_item_records(
        self,
        requested_category: str | int,
        *,
        viewer_profession: int | None = None,
    ) -> list[bytes]:
        category_id = int(requested_category)
        if category_id != self.category_id:
            raise ValueError(f"shop {self.key!r} expected category {self.category_id}, got {category_id}")
        sort_types = self.client_sort_types
        return [
            item.build_record(
                category_id,
                viewer_profession=viewer_profession,
                client_sort_type=(
                    sort_types[index]
                    if index < len(sort_types)
                    else None
                ),
            )
            for index, item in enumerate(self.items)
        ]

    def find_item(self, resource_key: str) -> ShopItem | None:
        normalized = str(resource_key).strip().lower()
        return next((item for item in self.items if item.resource_key == normalized), None)


# Runtime equipment and shop registries are populated from SQLite.
_EQUIPMENT_CATALOG: dict[int, ShopItem] = {}
_ALL_SHOPS: list[ShopDefinition] = []
_SHOPS_BY_NPC: dict[str, ShopDefinition] = {}
_SHOPS_BY_NPC_IDENTITY: dict[tuple[int, str], ShopDefinition] = {}


def configure_equipment_catalog(rows: Iterable[Any]) -> None:
    """Load all enabled equipment templates from SQLite."""
    catalog = {int(row["id"]): ShopItem.from_equip_row(row) for row in rows}
    _EQUIPMENT_CATALOG.clear()
    _EQUIPMENT_CATALOG.update(catalog)


def configure_shop_definitions(
    npc_shop_rows: Iterable[Any],
    shop_item_rows: Iterable[Any],
) -> None:
    """Build one database-backed shop for each bound NPC."""
    shops_by_id: dict[int, ShopDefinition] = {}
    identity_bindings: list[tuple[int, str, int]] = []

    for row in npc_shop_rows:
        shop_id = int(row["id"])
        being_name = str(row["npc_being_name"] or "").strip()
        shops_by_id[shop_id] = ShopDefinition(
            key=str(row["shop_key"]),
            npc_being_names=frozenset({being_name}) if being_name else frozenset(),
            default_title=str(row["shop_title"]),
            category_label=str(row["category_label"]),
            category_id=int(row["category_id"]),
            items=(),
        )
        identity_bindings.append(
            (int(row["map_id"]), str(row["npc_display_name"]), shop_id)
        )

    item_rows_by_shop: dict[int, list[tuple[int, int | None]]] = {
        shop_id: [] for shop_id in shops_by_id
    }
    for row in shop_item_rows:
        shop_id = int(row["npc_shop_id"])
        if shop_id not in item_rows_by_shop:
            continue
        try:
            raw_sort_type = row["client_sort_type"]
        except (KeyError, IndexError, TypeError):
            raw_sort_type = None
        item_rows_by_shop[shop_id].append((
            int(row["equip_id"]),
            None if raw_sort_type is None else int(raw_sort_type),
        ))

    for shop_id, shop in shops_by_id.items():
        resolved = [
            (_EQUIPMENT_CATALOG[item_id], client_sort_type)
            for item_id, client_sort_type in item_rows_by_shop.get(shop_id, [])
            if item_id in _EQUIPMENT_CATALOG
        ]
        shop.items = tuple(item for item, _ in resolved)
        shop.client_sort_types = tuple(sort_type for _, sort_type in resolved)

    _ALL_SHOPS.clear()
    _ALL_SHOPS.extend(shops_by_id[shop_id] for shop_id in sorted(shops_by_id))
    _SHOPS_BY_NPC.clear()
    for shop in _ALL_SHOPS:
        for being_name in shop.npc_being_names:
            _SHOPS_BY_NPC[being_name] = shop
    _SHOPS_BY_NPC_IDENTITY.clear()
    for map_id, display_name, shop_id in identity_bindings:
        _SHOPS_BY_NPC_IDENTITY[(map_id, display_name)] = shops_by_id[shop_id]


def decode_ascii_c_string(payload: bytes) -> str:
    return payload.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def decode_shop_category_request(payload: bytes) -> tuple[int | None, str]:
    if not payload:
        return None, ""
    return payload[0], payload[1:].split(b"\x00", 1)[0].decode("ascii", errors="replace")


def parse_npc_being_name(being_name: str) -> tuple[int | None, int | None]:
    if not being_name.startswith("NPC") or "_" not in being_name:
        return None, None
    map_text, npc_text = being_name[3:].split("_", 1)
    if not map_text.isdigit() or not npc_text.isdigit():
        return None, None
    return int(map_text), int(npc_text)


def find_shop_for_npc(being_name: str) -> ShopDefinition | None:
    return _SHOPS_BY_NPC.get(being_name)


def find_shop_for_npc_identity(map_id: int, display_name: str) -> ShopDefinition | None:
    return _SHOPS_BY_NPC_IDENTITY.get((int(map_id), str(display_name)))


@dataclass(frozen=True)
class ShopTransactionRequest:
    action: int
    resource_key: str
    client_value1: int
    client_value2: int

    @property
    def client_value(self) -> int:
        """Compatibility view of the two native uint16 fields as one uint32."""
        return (int(self.client_value1) << 16) | int(self.client_value2)


def decode_shop_transaction_request(payload: bytes) -> ShopTransactionRequest:
    if len(payload) < 6:
        raise ValueError(f"0x8019 payload too short: {len(payload)}")
    nul_index = payload.find(b"\x00", 1)
    if nul_index < 0:
        raise ValueError("0x8019 item key is not NUL terminated")
    tail = payload[nul_index + 1:]
    if len(tail) != 4:
        raise ValueError(
            "0x8019 trailing fields must be two uint16 values, "
            f"got {len(tail)} byte(s)"
        )
    resource_key = payload[1:nul_index].decode("ascii", errors="strict").lower()
    if not resource_key:
        raise ValueError("0x8019 item key is empty")
    return ShopTransactionRequest(
        action=int(payload[0]),
        resource_key=resource_key,
        client_value1=int.from_bytes(tail[0:2], "big"),
        client_value2=int.from_bytes(tail[2:4], "big"),
    )


# Compatibility names retained for older modules and tools.
ShopPurchaseRequest = ShopTransactionRequest
decode_shop_purchase_request = decode_shop_transaction_request


def parse_owned_instance_key(resource_key: str) -> int | None:
    """Parse the authoritative owned-object key: inv + eight hex digits."""
    text = str(resource_key).strip().lower()
    if len(text) != 11 or not text.startswith("inv"):
        return None
    try:
        instance_id = int(text[3:], 16)
    except ValueError:
        return None
    return instance_id if instance_id > 0 else None


def find_shop_for_item_id(item_id: int) -> ShopDefinition | None:
    parsed = int(item_id)
    for shop in _ALL_SHOPS:
        if any(item.item_id == parsed for item in shop.items):
            return shop
    return None


def find_shop_item_by_item_id(item_id: int) -> ShopItem | None:
    return _EQUIPMENT_CATALOG.get(int(item_id))
