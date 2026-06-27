from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from mob_database import MOB_SCHEMA_SQL, decode_u8_grid
from merchant_pricing import calculate_recycle_price, calculate_repair_price
from stat_engine import STAT_KEY_NAMES, StatKey


CHARACTER_NAME_ENCODING = "cp936"
CHARACTER_NAME_MAX_BYTES = 16


def _is_allowed_character_name_char(value: str) -> bool:
    """Return whether one Unicode character is safe in a role name.

    KOK2 uses ``#`` as the role-token field separator and NUL-terminated
    protocol strings, so punctuation, whitespace and control characters are
    intentionally rejected.  CP936 letters/numbers (including Chinese
    ideographs) and underscore are accepted.
    """
    if value == "_":
        return True
    return unicodedata.category(value)[:1] in {"L", "N"}


def is_safe_character_name(name: str) -> bool:
    """Validate a database/protocol role name using the client CP936 code page.

    The historical server used an ASCII-only regular expression.  That made
    valid Chinese names impossible even though all client text packets use
    CP936.  Keep the same conservative delimiter/control protection while
    allowing CP936 letters and numbers.
    """
    if not isinstance(name, str) or not name:
        return False
    if name != name.strip():
        return False
    if not all(_is_allowed_character_name_char(ch) for ch in name):
        return False
    try:
        encoded = name.encode(CHARACTER_NAME_ENCODING, errors="strict")
    except UnicodeEncodeError:
        return False
    return 1 <= len(encoded) <= CHARACTER_NAME_MAX_BYTES

# Level-1 role skills persisted in character_skills.
ATTRIBUTE_INFO_SKILL_IDS = tuple(range(0x0001, 0x0009))
PROFESSION_LEVEL1_SKILL_IDS = {
    1: (10001, 10002, 10003),
    1000: (10001, 10002, 10003),
    2: (20001, 20002, 20003),
    2000: (20001, 20002, 20003),
    3: (30001, 30002, 30003),
    3000: (30001, 30002, 30003),
}
DEFAULT_SKILL_FIELD04_UNKNOWN = 1
DEFAULT_SKILL_FIELD0C_UNKNOWN = 1
DEFAULT_SKILL_FIELD0E_UNKNOWN = 1
DEFAULT_SKILL_LEVEL = 1

# V17.37: profession base stats before the seven level-1 attribute effects.
# These values are seeded once when profession_level1_stats is first created.
# Later administrator edits, deletions, or disabled rows are never restored.
DEFAULT_PROFESSION_LEVEL1_STAT_ROWS = (
    # profession, hp, mp, sp, attack, defense, magic_attack, normal_attack_interval_ms, note
    (1000, 240, 60, 140, 18, 10, 0, 2000, "战士职业基础值（七项属性加成前）"),
    (2000, 180, 200, 60, 10, 5, 18, 3000, "法师职业基础值（七项属性加成前）"),
    (3000, 220, 160, 120, 14, 8, 14, 2200, "牧师职业基础值（七项属性加成前）"),
)

# V17.33 skill-upgrade cost defaults. These rows are inserted only when the
# rule table is first created; later administrator edits are never restored or
# overwritten by Python. The key is the current skill level (from_level).
# A missing next-level row means that category has reached its configured cap.
LEVEL_SKILL_UPGRADE_EXP_BY_FROM_LEVEL = {
    1: 30, 2: 66, 3: 128, 4: 228, 5: 384, 6: 640, 7: 1010,
    8: 1525, 9: 2220, 10: 3393, 11: 4939, 12: 6925,
    13: 9425, 14: 12516, 15: 16283, 16: 20813, 17: 26203,
    18: 32552, 19: 39965, 20: 49640, 21: 62101, 22: 77939,
    23: 97819, 24: 122485, 25: 152760, 26: 189554,
    27: 233865, 28: 286787, 29: 349508, 30: 422662,
    31: 520494, 32: 633499, 33: 768185, 34: 927573,
    35: 1114713, 36: 1332935, 37: 1585814, 38: 1877173,
    39: 2211091, 40: 2606985, 41: 3072376, 42: 3615305,
    43: 4244349, 44: 4968642, 45: 5797884, 46: 6742362,
    47: 7812964, 48: 9021196, 49: 10379196, 50: 11899753,
    51: 13596321, 52: 15483034, 53: 17574725, 54: 19886940,
    55: 22435955, 56: 25238789, 57: 28313226, 58: 31677824,
    59: 35351937,
}

ATTRIBUTE_SKILL_UPGRADE_EXP_BY_FROM_LEVEL = {
    1: 12, 2: 40, 3: 51, 4: 91, 5: 154, 6: 256, 7: 404,
    8: 610, 9: 888, 10: 1357, 11: 1976, 12: 2770, 13: 3770,
    14: 5006, 15: 6513, 16: 8325, 17: 10481, 18: 13021,
    19: 15986, 20: 19856, 21: 24840, 22: 31176, 23: 39128,
    24: 48994, 25: 61104, 26: 75822, 27: 93546, 28: 114715,
    29: 139803, 30: 170665, 31: 208199, 32: 253380,
    33: 307274, 34: 371029, 35: 445885, 36: 533174,
    37: 634326, 38: 750869, 39: 885525, 40: 1042794,
    41: 1228950, 42: 1446122, 43: 1697740, 44: 1987457,
    45: 2319154, 46: 2696945, 47: 3125186, 48: 3608478,
    49: 4151678, 50: 4759901, 51: 5438528, 52: 6193213,
    53: 7029890, 54: 7954776, 55: 8974382, 56: 10095516,
    57: 11325290, 58: 12671130, 59: 14140775,
}

PROFESSION_SKILL_UPGRADE_EXP_BY_FROM_LEVEL = {
    1: 18, 2: 40, 3: 77, 4: 137, 5: 230, 6: 384, 7: 606,
    8: 915, 9: 1332, 10: 2036, 11: 2963, 12: 4155,
    13: 5655, 14: 7510, 15: 9770, 16: 12488, 17: 15722,
    18: 19531, 19: 23979,
}

ATTRIBUTE_SKILL_IDS = tuple(range(0x0002, 0x0009))

# V17.37: one-point effects for the seven attribute-info skills.
# profession=0 applies to every profession.  Exact profession rows can override
# an individual target column without changing Python code.
#
# HP/MP/SP rows deliberately modify only the maximum.  Spending one attribute
# point does not heal/refill the current resource; a newly created role starts
# full after all seven level-1 effects have been applied.
DEFAULT_SKILL_UPGRADE_EFFECT_ROWS = (
    (0, 0x0002, "max_hp", 20, "生命力：每点最大HP+20"),
    (0, 0x0003, "max_mp", 20, "魔力：每点最大MP+20"),
    (0, 0x0004, "max_sp", 20, "精力：每点最大SP+20"),
    (0, 0x0005, "attack_power", 1, "力量：每点物理攻击力+1"),
    (0, 0x0006, "defense_power", 1, "敏捷：每点物理防御力+1"),
    (0, 0x0006, "earth_resistance", 1, "敏捷：每点地抗性+1"),
    (0, 0x0006, "water_resistance", 1, "敏捷：每点水抗性+1"),
    (0, 0x0006, "fire_resistance", 1, "敏捷：每点火抗性+1"),
    (0, 0x0006, "wind_resistance", 1, "敏捷：每点风抗性+1"),
    (0, 0x0006, "light_resistance", 1, "敏捷：每点光抗性+1"),
    (0, 0x0006, "dark_resistance", 1, "敏捷：每点闇抗性+1"),
    (0, 0x0007, "magic_attack_power", 3, "智慧：每点魔法攻击力+3"),
    (0, 0x0008, "max_hp", 5, "体质：每点最大HP+5"),
    (0, 0x0008, "hp_regen_per_second", 1, "体质：每点HP回复/秒+1"),
)

# Milestone effects fire when the target skill level reaches first_target_level
# and then every interval_levels.  Thus levels 2,4,6... give one additional
# point of SP/MP regeneration, while level 1 gives none.
DEFAULT_SKILL_UPGRADE_MILESTONE_ROWS = (
    (0, 0x0005, "sp_regen_per_second", 2, 2, 1,
     "力量：每两点SP回复/秒+1，首次在Lv2生效"),
    (0, 0x0007, "mp_regen_per_second", 2, 2, 1,
     "智慧：每两点MP回复/秒+1，首次在Lv2生效"),
)

ATTRIBUTE_LEVEL_CHARACTER_COLUMN = {
    0x0005: "strength",
    0x0006: "dexterity",
    0x0007: "wisdom",
    0x0008: "constitution",
}

SKILL_EFFECT_CHARACTER_COLUMNS = frozenset({
    "hp", "max_hp", "mp", "max_mp", "sp", "max_sp",
    "strength", "wisdom", "dexterity", "constitution",
    "attack_power", "defense_power", "magic_attack_power",
    "earth_resistance", "water_resistance", "fire_resistance",
    "wind_resistance", "light_resistance", "dark_resistance",
    "hp_regen_per_second", "mp_regen_per_second", "sp_regen_per_second",
    "reputation",
})

# Default NPC shops.  One row is one shop bound to one NPC; no separate
# shop-definition table is needed for the current one-NPC/one-shop design.
DEFAULT_NPC_SHOP_ROWS = (
    (1, "novice_armour_shop", 10100, "史大福", "NPC10100_008", "防具商人", "一般", 190, 5, 1),
    (2, "weapon_shop_pike", 10100, "皮克", "NPC10100_009", "武器商人", "一般", 190, 5, 1),
    (3, "shield_shop_soder", 10100, "索德", "NPC10100_007", "盾牌商人", "一般", 190, 5, 1),
)

DEFAULT_SHOP_ITEM_IDS = {
    1: (
        0x005A01, 0x005B01, 0x006A01, 0x006B01,
        0x007A01, 0x007B01, 0x008A01, 0x008B01,
        0x009A01, 0x009B01, 0x009C01, 0x010A01,
    ),
    2: (
        0x101A01, 0x101B01, 0x101C01, 0x103A01,
        0x111A01, 0x111B01, 0x111C01, 0x112A01,
        0x102A01, 0x102B01, 0x102C01, 0x102D01,
    ),
    3: (
        0x131A01, 0x131B01, 0x132A01, 0x132B01,
        0x133A01, 0x133B01, 0x141A01, 0x141B01,
    ),
}

# Confirmed client destination codes from the equipment compatibility switch:
# weapons -> 210 (right hand), shields/off-hands -> 211 (left hand).
EQUIP_SLOT_NAME_BY_CODE = {
    210: "weapon",
    211: "shield",
    201: "armour",
    203: "pants",
    204: "shoes",
    205: "gloves",
    200: "helmet",
    202: "belt",
    206: "right_ring",
    207: "left_ring",
    209: "earring",
    208: "necklace",
    214: "badge1",
    215: "badge2",
    216: "badge3",
}
DUAL_WEAPON_SLOT_CODES = frozenset({210, 211})
NORMAL_EQUIP_SLOT_CODES = tuple(
    slot for slot in EQUIP_SLOT_NAME_BY_CODE if slot not in DUAL_WEAPON_SLOT_CODES
)
ALL_EQUIP_SLOT_ROWS = tuple(
    [(slot, 0, EQUIP_SLOT_NAME_BY_CODE[slot]) for slot in NORMAL_EQUIP_SLOT_CODES]
    + [(210, 1, "weapon1"), (211, 1, "shield1"),
       (210, 2, "weapon2"), (211, 2, "shield2")]
)

EQUIP_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS equip (
    id INTEGER PRIMARY KEY,
    resource_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    equip_type INTEGER NOT NULL DEFAULT 0,
    attack_power INTEGER NOT NULL DEFAULT 0,
    attack_power_max INTEGER NOT NULL DEFAULT 0,
    magic_power INTEGER NOT NULL DEFAULT 0,
    defense_power INTEGER NOT NULL DEFAULT 0,
    speed INTEGER NOT NULL DEFAULT 0,
    required_strength INTEGER NOT NULL DEFAULT 0,
    required_dexterity INTEGER NOT NULL DEFAULT 0,
    required_intelligence INTEGER NOT NULL DEFAULT 0,
    required_constitution INTEGER NOT NULL DEFAULT 0,
    required_profession INTEGER NOT NULL DEFAULT 0,
    client_class_limit_code INTEGER NOT NULL DEFAULT 0,
    required_level INTEGER NOT NULL DEFAULT 0,
    attack_range INTEGER NOT NULL DEFAULT 0,
    attack_range_max INTEGER NOT NULL DEFAULT 0,
    durability INTEGER NOT NULL DEFAULT 0,
    price INTEGER NOT NULL DEFAULT 0,
    base_special_attr_key INTEGER,
    base_special_attr_value INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1
)
"""

DEFAULT_EQUIP_ROWS = (
    (23041, '005a01', '新手战士胸甲', 211, 0, 0, 0, 6, 0, 0, 0, 0, 0, 1000, 0, 2, 0, 0, 150, 150, None, None, 1),
    (23297, '005b01', '新手战士手套', 251, 0, 0, 0, 2, 0, 0, 0, 0, 0, 1000, 0, 2, 0, 0, 150, 120, None, None, 1),
    (27137, '006a01', '新手战士裤甲', 231, 0, 0, 0, 3, 0, 0, 0, 0, 0, 1000, 0, 2, 0, 0, 150, 100, None, None, 1),
    (27393, '006b01', '新手战士长靴', 241, 0, 0, 0, 2, 0, 0, 0, 0, 0, 1000, 0, 2, 0, 0, 150, 120, None, None, 1),
    (31233, '007a01', '新手法师胸衣', 211, 0, 0, 0, 4, 0, 0, 0, 0, 0, 2000, 0, 2, 0, 0, 150, 150, None, None, 1),
    (31489, '007b01', '新手法师手套', 251, 0, 0, 0, 1, 0, 0, 0, 0, 0, 2000, 0, 2, 0, 0, 150, 120, None, None, 1),
    (35329, '008a01', '新手法师裙甲', 231, 0, 0, 0, 2, 0, 0, 0, 0, 0, 2000, 0, 2, 0, 0, 150, 100, None, None, 1),
    (35585, '008b01', '新手法师布靴', 241, 0, 0, 0, 1, 0, 0, 0, 0, 0, 2000, 0, 2, 0, 0, 150, 120, None, None, 1),
    (39425, '009a01', '新手牧师胸衣', 211, 0, 0, 0, 6, 0, 0, 0, 0, 0, 3000, 0, 2, 0, 0, 150, 150, None, None, 1),
    (39681, '009b01', '新手牧师手套', 251, 0, 0, 0, 2, 0, 0, 0, 0, 0, 3000, 0, 2, 0, 0, 150, 120, None, None, 1),
    (39937, '009c01', '新手牧师裙铠', 231, 0, 0, 0, 3, 0, 0, 0, 0, 0, 3000, 0, 2, 0, 0, 150, 100, None, None, 1),
    (0x10a01, '010a01', '新手牧师短靴', 241, 0, 0, 0, 2, 0, 0, 0, 0, 0, 3000, 0, 2, 0, 0, 150, 120, None, None, 1),
    (0x101a01, '101a01', '新手短剑', 101, 18, 27, 0, 0, 100, 0, 0, 0, 0, 1000, 0, 2, 1, 2, 150, 250, None, None, 1),
    (0x101b01, '101b01', '破风短剑', 101, 42, 63, 0, 0, 100, 4, 0, 0, 0, 1000, 0, 7, 1, 2, 150, 1000, None, None, 1),
    (0x101c01, '101c01', '金属短剑', 101, 72, 108, 0, 0, 100, 8, 0, 0, 0, 1000, 0, 12, 1, 2, 150, 5000, None, None, 1),
    (0x103a01, '103a01', '商会卫兵短剑', 101, 108, 162, 0, 0, 100, 12, 0, 0, 0, 1000, 0, 17, 1, 2, 150, 25000, None, None, 1),
    (0x111a01, '111a01', '学徒魔杖', 105, 12, 22, 10, 0, 100, 0, 0, 0, 0, 2000, 0, 2, 1, 1, 150, 250, None, None, 1),
    (0x111b01, '111b01', '灵风魔杖', 105, 28, 51, 18, 0, 100, 0, 0, 4, 0, 2000, 0, 7, 1, 1, 150, 1000, None, None, 1),
    (0x111c01, '111c01', '奥术魔杖', 105, 49, 88, 30, 0, 100, 0, 0, 8, 0, 2000, 0, 12, 1, 1, 150, 5000, None, None, 1),
    (0x112a01, '112a01', '商会秘法魔杖', 105, 76, 135, 42, 0, 100, 0, 0, 12, 0, 2000, 0, 17, 1, 1, 150, 25000, None, None, 1),
    (0x102a01, '102a01', '见习法锤', 102, 15, 24, 4, 0, 100, 0, 0, 0, 0, 3000, 0, 2, 1, 2, 150, 250, None, None, 1),
    (0x102b01, '102b01', '执事法锤', 102, 37, 56, 12, 0, 100, 0, 0, 2, 0, 3000, 0, 7, 1, 2, 150, 1000, None, None, 1),
    (0x102c01, '102c01', '祈祷法锤', 102, 65, 98, 20, 0, 100, 0, 0, 6, 0, 3000, 0, 12, 1, 2, 150, 5000, None, None, 1),
    (0x102d01, '102d01', '圣洁牧师法锤', 102, 97, 148, 28, 0, 100, 0, 0, 10, 0, 3000, 0, 17, 1, 2, 150, 25000, None, None, 1),
    (0x131a01, '131a01', '新手盾牌', 174, 0, 0, 0, 3, 0, 0, 0, 0, 0, 1000, 0, 2, 0, 0, 150, 120, None, None, 1),
    (0x131b01, '131b01', '防风盾牌', 174, 0, 0, 0, 7, 0, 4, 0, 0, 0, 1000, 0, 7, 0, 0, 150, 500, None, None, 1),
    (0x132a01, '132a01', '金属盾牌', 174, 0, 0, 0, 12, 0, 8, 0, 0, 0, 1000, 0, 12, 0, 0, 150, 2500, None, None, 1),
    (0x132b01, '132b01', '商会卫兵盾牌', 174, 0, 0, 0, 18, 0, 12, 0, 0, 0, 1000, 0, 17, 0, 0, 150, 12000, None, None, 1),
    (0x133a01, '133a01', '见习法盾', 174, 0, 0, 0, 2, 0, 0, 0, 0, 0, 3000, 0, 2, 0, 0, 150, 120, None, None, 1),
    (0x133b01, '133b01', '执事法盾', 174, 0, 0, 0, 5, 0, 0, 0, 0, 2, 3000, 0, 7, 0, 0, 150, 500, None, None, 1),
    (0x141a01, '141a01', '祈祷法盾', 174, 0, 0, 0, 8, 0, 0, 0, 0, 6, 3000, 0, 12, 0, 0, 150, 2500, None, None, 1),
    (0x141b01, '141b01', '圣洁牧师法盾', 174, 0, 0, 0, 11, 0, 0, 0, 0, 10, 3000, 0, 17, 0, 0, 150, 12000, None, None, 1),
)

# V17.13 bridge schemas.  Older databases are first normalized into these
# shapes, then migrated into equip_instance + location-only bag/equip tables.
LEGACY_CHARACTER_BAG_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS character_bag (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    bag_slot INTEGER,
    current_durability INTEGER,
    unique_attr1 TEXT NOT NULL DEFAULT '',
    unique_attr2 TEXT NOT NULL DEFAULT '',
    unique_attr3 TEXT NOT NULL DEFAULT '',
    unique_attr4 TEXT NOT NULL DEFAULT '',
    unique_attr5 TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE
)
"""

LEGACY_CHARACTER_EQUIP_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS character_equip (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    slot_code INTEGER NOT NULL,
    weapon_set INTEGER NOT NULL DEFAULT 0,
    slot_name TEXT NOT NULL,
    bag_item_id INTEGER,
    unique_attr1 TEXT NOT NULL DEFAULT '',
    unique_attr2 TEXT NOT NULL DEFAULT '',
    unique_attr3 TEXT NOT NULL DEFAULT '',
    unique_attr4 TEXT NOT NULL DEFAULT '',
    unique_attr5 TEXT NOT NULL DEFAULT '',
    UNIQUE(character_id, slot_code, weapon_set),
    FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE,
    FOREIGN KEY(bag_item_id) REFERENCES character_bag(id) ON DELETE SET NULL
)
"""

EQUIP_INSTANCE_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS equip_instance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    owner_character_id INTEGER NOT NULL,
    current_durability INTEGER NOT NULL DEFAULT 0,
    bind_state INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL DEFAULT '',
    source_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(template_id) REFERENCES equip(id),
    FOREIGN KEY(owner_character_id) REFERENCES characters(id) ON DELETE CASCADE
)
"""

EQUIP_INSTANCE_ATTR_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS equip_instance_attr (
    instance_id INTEGER NOT NULL,
    slot_no INTEGER NOT NULL CHECK(slot_no BETWEEN 1 AND 5),
    attr_key INTEGER NOT NULL,
    attr_value INTEGER NOT NULL,
    attr_source TEXT NOT NULL DEFAULT '',
    attr_level INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(instance_id, slot_no),
    FOREIGN KEY(instance_id) REFERENCES equip_instance(id) ON DELETE CASCADE
)
"""

CHARACTER_BAG_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS character_bag (
    character_id INTEGER NOT NULL,
    bag_slot INTEGER NOT NULL,
    instance_id INTEGER NOT NULL UNIQUE,
    PRIMARY KEY(character_id, bag_slot),
    FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE,
    FOREIGN KEY(instance_id) REFERENCES equip_instance(id) ON DELETE CASCADE
)
"""

CHARACTER_EQUIP_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS character_equip (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    slot_code INTEGER NOT NULL,
    weapon_set INTEGER NOT NULL DEFAULT 0,
    slot_name TEXT NOT NULL,
    instance_id INTEGER UNIQUE,
    UNIQUE(character_id, slot_code, weapon_set),
    FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE,
    FOREIGN KEY(instance_id) REFERENCES equip_instance(id) ON DELETE SET NULL
)
"""


NPC_DEFAULT_ROWS_BY_MAP = {
    10100: (
        ("史利佛", "安全官", 110, 46),
        ("马修", "魔法见习生", 202, 171),
        ("高登", "大队长", 112, 52),
        ("安德鲁", "大法师", 207, 168),
        ("兰格", "牧师", 61, 138),
        ("斯提", "安全官", 110, 48),
        ("索德", "盾牌商人", 119, 100),
        ("史大福", "防具商人", 119, 99),
        ("皮克", "武器商人", 119, 98),
        ("赫尔西", "卫生官", 145, 67),
        ("巴利", "卫生官", 145, 64),
        ("阿鲁米", "安全官", 110, 50),
        ("魁斯特", "光明商会·赏金猎人", 173, 136),
        ("瑞伊渥", "黑暗商会·赏金猎人", 72, 119),
        ("奥多", "下水道管理员", 165, 228),
        ("辛吉", "朝圣者", 134, 74),
        ("帕里", "朝圣者", 134, 56),
        ("达克", "急救医生", 127, 103),
    ),
    10121: (
        ("桃乐丝", "防具商人", 26, 18),
        ("雪莉", "防具商人", 21, 18),
        ("库必卡", "工艺专家", 38, 22),
        ("佛克斯", "工艺药剂商人", 32, 22),
    ),
    10122: (
        ("史帝文（银行）", "银行行员", 24, 26),
    ),
    10123: (
        ("提米", "杂货商人", 24, 32),
        ("派德", "驯兽师", 40, 29),
        ("杰洛丝", "首饰商人", 34, 31),
    ),
    10124: (
        ("艾维斯", "武器商人", 39, 21),
        ("艾德华", "工艺专家", 27, 21),
        ("佛克斯", "工艺药剂商人", 25, 35),
    ),
    10125: (
        ("妮可", "牧师", 90, 117),
        ("史密斯", "药剂师", 38, 26),
        ("齐瓦士", "实习医生", 31, 32),
        ("甘道鲁", "巫术药师", 39, 40),
    ),
}

KNOWN_MAP_ROWS = (
    # id, terrain_name, display_name, scene_file, bgm_index, bgm_file
    (10100, '巴布朗中央区', '巴布朗中央区', r'map\\10100.Sce', 8, 'TW10100'),
    (10111, '下水道一层', '下水道一层', r'map\\10111.Sce', 13, 'TW10111'),
    (10112, '下水道二层', '下水道二层', r'map\\10112.Sce', 13, 'TW10111'),
    (10121, '防具铺', '防具铺', r'map\\10121.Sce', 12, 'TW10121'),
    (10122, '银行', '银行', r'map\\10122.Sce', 12, 'TW10121'),
    (10123, '杂货店', '杂货店', r'map\\10123.Sce', 12, 'TW10121'),
    (10124, '武器店', '武器店', r'map\\10124.Sce', 12, 'TW10121'),
    (10125, '医院', '医院', r'map\\10125.Sce', 12, 'TW10121'),
    (10200, '巴布朗-港区', '巴布朗-港区', r'map\\10200.Sce', 9, 'TW10200'),
    (10300, '巴布朗-西区', '巴布朗-西区', r'map\\10300.Sce', 10, 'TW10300'),
    (10400, '巴布朗-南区', '巴布朗-南区', r'map\\10400.Sce', 11, 'TW10400'),
    (20100, '割喉岛', '割喉岛', r'map\\20100.Sce', 107, 'CN10401'),
    (30100, '麦米尔斯草原', '麦米尔斯草原', r'map\\30100.Sce', 4, 'music4'),
    (30101, '雀谷', '雀谷', r'map\\30101.Sce', 15, 'TW30100'),
    (30200, '红岛丘陵', '红岛丘陵', r'map\\30200.Sce', 16, 'TW30200'),
    (30300, '莱茵草原', '莱茵草原', r'map\\30300.Sce', 16, 'TW30200'),
    (30400, '米拉尔湖', '米拉尔湖', r'map\\30400.Sce', 18, 'TW30400'),
    (30500, '亚里安海岸', '亚里安海岸', r'map\\30500.Sce', 21, 'TW30500'),
    (30600, '红岛高原', '红岛高原', r'map\\30600.Sce', None, ''),
    (30700, '北海岸山脉', '北海岸山脉', r'map\\30700.Sce', 22, 'TW30700'),
    (30800, '红谷北部', '红谷北部', r'map\\30800.Sce', 23, 'TW30800'),
    (30900, '红谷城', '红谷城', r'map\\30900.Sce', 22, 'TW30700'),
    (31000, '伤悲湖畔', '伤悲湖畔', r'map\\31000.Sce', 24, 'TW31000'),
    (31100, '艾努山', '艾努山', r'map\\31100.Sce', 24, 'TW31000'),
    (31200, '艾努平原', '艾努平原', r'map\\31200.Sce', 25, 'TW31200'),
    (31300, '怜悯草原', '怜悯草原', r'map\\31300.Sce', 25, 'TW31200'),
    (40100, '麦米尔斯森林', '麦米尔斯森林', r'map\\40100.Sce', 15, 'TW30100'),
    (40200, '红岛大湖', '红岛大湖', r'map\\40200.Sce', None, ''),
)

# Coordinate-triggered map transitions.  These rows are only one-time seed
# data.  Runtime teleport behavior is always read from map_teleports.
#
# teleport_key is intentionally stable and must not depend on coordinates.  This
# lets you edit trigger_x/trigger_y/target_x/target_y in the database without the
# startup migration recreating the old default coordinate as a duplicate row.
TELEPORT_DEFAULT_ROWS = (
    # teleport_key, source_map_id, trigger_x, trigger_y, target_map_id, target_x, target_y, target_direction, note
    ("central_to_armor_shop", 10100, 61, 104, 10121, 30, 30, 0, "巴布朗中央区 -> 防具铺"),
    ("armor_shop_to_central", 10121, 30, 32, 10100, 58, 104, 0, "防具铺 -> 巴布朗中央区"),
    ("central_to_bank", 10100, 128, 41, 10122, 24, 34, 0, "巴布朗中央区 -> 银行"),
    ("bank_to_central", 10122, 21, 34, 10100, 128, 44, 0, "银行 -> 巴布朗中央区"),
    ("central_to_grocery", 10100, 140, 103, 10123, 32, 40, 0, "巴布朗中央区 -> 杂货店"),
    ("grocery_to_central", 10123, 32, 42, 10100, 137, 103, 0, "杂货店 -> 巴布朗中央区"),
    ("central_to_weapon_shop", 10100, 38, 129, 10124, 32, 42, 0, "巴布朗中央区 -> 武器店"),
    ("weapon_shop_to_central", 10124, 32, 44, 10100, 41, 129, 0, "武器店 -> 巴布朗中央区"),
    ("central_to_hospital", 10100, 91, 117, 10125, 35, 40, 0, "巴布朗中央区 -> 医院"),
    ("hospital_to_central", 10125, 32, 42, 10100, 91, 120, 0, "医院 -> 巴布朗中央区"),
)


CHARACTER_COLUMN_ORDER = (
    "id",
    "account_id",
    "name",
    "gender",
    "level",
    "profession",
    "ancillary_profession",
    "map_id",
    "position_x",
    "position_y",
    "direction",
    "body",
    "hair",
    "head",
    "hand_r",
    "hand_l",
    "pants",
    "foot_r",
    "foot_l",
    "hp",
    "max_hp",
    "mp",
    "max_mp",
    "sp",
    "max_sp",
    "hp_regen_per_second",
    "mp_regen_per_second",
    "sp_regen_per_second",
    "created_at",
    "last_login_at",
    "last_logout_area",
    "player_gold",
    "depot_gold",
    "organization_label",
    "country_label",
    "reborn_label",
    "strength",
    "wisdom",
    "dexterity",
    "constitution",
    "attack_power",
    "defense_power",
    "magic_attack_power",
    "earth_resistance",
    "water_resistance",
    "fire_resistance",
    "wind_resistance",
    "light_resistance",
    "dark_resistance",
    "reputation",
    "evil",
    "kill_count",
    "pk_win_count",
    "pk_loss_count",
    "experience",
    "bag_capacity",
    "active_weapon_set",
)

CHARACTERS_CREATE_SQL = """
CREATE TABLE characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    name TEXT NOT NULL UNIQUE,
    gender INTEGER NOT NULL DEFAULT 1,
    level INTEGER NOT NULL DEFAULT 1,
    profession INTEGER NOT NULL DEFAULT 0,
    ancillary_profession INTEGER NOT NULL DEFAULT 0,

    map_id INTEGER NOT NULL DEFAULT 10100,
    position_x INTEGER NOT NULL DEFAULT 10,
    position_y INTEGER NOT NULL DEFAULT 10,
    direction INTEGER NOT NULL DEFAULT 0,

    body INTEGER NOT NULL DEFAULT 256,
    hair INTEGER NOT NULL DEFAULT 1000,
    head INTEGER NOT NULL DEFAULT 256,
    hand_r INTEGER NOT NULL DEFAULT 256,
    hand_l INTEGER NOT NULL DEFAULT 256,
    pants INTEGER NOT NULL DEFAULT 256,
    foot_r INTEGER NOT NULL DEFAULT 256,
    foot_l INTEGER NOT NULL DEFAULT 256,

    hp INTEGER NOT NULL DEFAULT 300,
    max_hp INTEGER NOT NULL DEFAULT 300,
    mp INTEGER NOT NULL DEFAULT 100,
    max_mp INTEGER NOT NULL DEFAULT 100,
    sp INTEGER NOT NULL DEFAULT 200,
    max_sp INTEGER NOT NULL DEFAULT 200,
    hp_regen_per_second INTEGER NOT NULL DEFAULT 0,
    mp_regen_per_second INTEGER NOT NULL DEFAULT 0,
    sp_regen_per_second INTEGER NOT NULL DEFAULT 0,

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    last_logout_area TEXT,
    player_gold INTEGER NOT NULL DEFAULT 888,
    depot_gold INTEGER NOT NULL DEFAULT 777,

    organization_label TEXT NOT NULL DEFAULT '无',
    country_label TEXT NOT NULL DEFAULT '无',
    reborn_label TEXT NOT NULL DEFAULT '00',
    strength INTEGER NOT NULL DEFAULT 1,
    wisdom INTEGER NOT NULL DEFAULT 1,
    dexterity INTEGER NOT NULL DEFAULT 1,
    constitution INTEGER NOT NULL DEFAULT 1,
    attack_power INTEGER NOT NULL DEFAULT 20,
    defense_power INTEGER NOT NULL DEFAULT 10,
    magic_attack_power INTEGER NOT NULL DEFAULT 20,
    earth_resistance INTEGER NOT NULL DEFAULT 0,
    water_resistance INTEGER NOT NULL DEFAULT 0,
    fire_resistance INTEGER NOT NULL DEFAULT 0,
    wind_resistance INTEGER NOT NULL DEFAULT 0,
    light_resistance INTEGER NOT NULL DEFAULT 0,
    dark_resistance INTEGER NOT NULL DEFAULT 0,
    reputation INTEGER NOT NULL DEFAULT 0,
    evil INTEGER NOT NULL DEFAULT 0,
    kill_count INTEGER NOT NULL DEFAULT 0,
    pk_win_count INTEGER NOT NULL DEFAULT 0,
    pk_loss_count INTEGER NOT NULL DEFAULT 0,
    experience INTEGER NOT NULL DEFAULT 100,
    bag_capacity INTEGER NOT NULL DEFAULT 40,
    active_weapon_set INTEGER NOT NULL DEFAULT 1,

    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY(map_id) REFERENCES maps(id)
)
"""


def default_level1_skill_ids_for_profession(profession: int) -> tuple[int, ...]:
    """Return the initial skill IDs for a newly created level-1 role."""
    profession_skills = PROFESSION_LEVEL1_SKILL_IDS.get(int(profession), ())
    return ATTRIBUTE_INFO_SKILL_IDS + tuple(profession_skills)


class Database:
    """Small SQLite wrapper used by the KOK2 local server."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._initial_schema_state_checked = False
        self._database_had_tables_at_first_connect = False

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if not self._initial_schema_state_checked:
            table_count = int(connection.execute(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchone()[0])
            self._database_had_tables_at_first_connect = table_count > 0
            self._initial_schema_state_checked = True
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            # Allow server_legacy.py to open an older kok2.db directly.
            # The map_table.xml patch added maps.display_name/bgm_index/bgm_file;
            # without this automatic migration, first_runtime_character() fails
            # before sync_map_table.py or init_database.py can be run.
            self._migrate_maps_schema(connection)
            self._migrate_characters_schema(connection)
            self._migrate_profession_level1_stats_schema(connection)
            self._migrate_character_skills_schema(connection)
            self._migrate_skill_upgrade_schema(connection)
            self._migrate_equipment_schema(connection)
            self._migrate_character_stat_sources_schema(connection)
            self._migrate_confirmed_level1_rules_v18_11(connection)
            self._migrate_shop_schema(connection)
            self._migrate_inventory_schema(connection)
            self._migrate_equipment_slot_layout_v17_27(connection)
            self._migrate_map_npcs_schema(connection)
            self._migrate_mob_schema(connection)
            self._migrate_teleports_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_schema(self, schema_path: Path) -> None:
        schema_sql = Path(schema_path).read_text(encoding="utf-8")
        with self.session() as connection:
            connection.executescript(schema_sql)
            self._migrate_maps_schema(connection)
            self._migrate_characters_schema(connection)
            self._migrate_profession_level1_stats_schema(connection)
            self._migrate_character_skills_schema(connection)
            self._migrate_skill_upgrade_schema(connection)
            self._migrate_equipment_schema(connection)
            self._migrate_character_stat_sources_schema(connection)
            self._migrate_confirmed_level1_rules_v18_11(connection)
            self._migrate_shop_schema(connection)
            self._migrate_inventory_schema(connection)
            self._migrate_equipment_slot_layout_v17_27(connection)
            self._migrate_map_npcs_schema(connection)
            self._migrate_mob_schema(connection)
            self._migrate_teleports_schema(connection)

    def _migrate_maps_schema(self, connection: sqlite3.Connection) -> None:
        """Add map-table columns without rewriting administrator-owned rows."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(maps)").fetchall()
        }
        if not columns:
            # maps table has not been created yet; initialize_schema() will
            # create it and call this migration again.
            return

        added_display_name = "display_name" not in columns
        added_bgm_file = "bgm_file" not in columns
        add_columns = (
            ("display_name", "TEXT NOT NULL DEFAULT ''"),
            ("bgm_index", "INTEGER"),
            ("bgm_file", "TEXT NOT NULL DEFAULT ''"),
        )
        for column_name, column_sql in add_columns:
            if column_name not in columns:
                connection.execute(
                    f"ALTER TABLE maps ADD COLUMN {column_name} {column_sql}"
                )

        # Backfill only while the column is first introduced.  A later manual
        # blank/NULL value is administrator data and must not be normalized on
        # every database session.
        if added_display_name:
            connection.execute(
                "UPDATE maps SET display_name = terrain_name "
                "WHERE display_name IS NULL OR display_name = ''"
            )
        if added_bgm_file:
            connection.execute(
                "UPDATE maps SET bgm_file = '' WHERE bgm_file IS NULL"
            )

        # Existing databases already own their map rows.  Record that the
        # optional default seed has effectively been completed, without
        # inserting or updating any content.
        self._mark_default_seed_owned_if_existing(
            connection,
            "default_map_data_seed_v17_29",
            int(connection.execute("SELECT COUNT(*) FROM maps").fetchone()[0]),
        )

    def seed_default_map_data(self, connection: sqlite3.Connection) -> bool:
        """Insert built-in map rows once, only for a new empty database."""
        migration_key = "default_map_data_seed_v17_29"
        if self._has_migration(connection, migration_key):
            return False
        if int(connection.execute("SELECT COUNT(*) FROM maps").fetchone()[0]) != 0:
            self._mark_migration(connection, migration_key)
            return False
        connection.executemany(
            """
            INSERT INTO maps (
                id, terrain_name, display_name, scene_file, bgm_index, bgm_file
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (int(map_id), terrain_name, display_name, scene_file, bgm_index, bgm_file)
                for map_id, terrain_name, display_name, scene_file, bgm_index, bgm_file
                in KNOWN_MAP_ROWS
            ],
        )
        self._mark_migration(connection, migration_key)
        return True

    def _migrate_profession_level1_stats_schema(
        self, connection: sqlite3.Connection
    ) -> None:
        """Create and seed pre-attribute profession base stats exactly once.

        The table is authoritative at runtime.  Once the seed migration is
        recorded, deleted/disabled/edited rows are never recreated by normal
        database sessions.
        """
        table_existed = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'profession_level1_stats'
            """
        ).fetchone() is not None

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profession_level1_stats (
                profession INTEGER PRIMARY KEY,
                hp INTEGER NOT NULL CHECK(hp >= 0),
                mp INTEGER NOT NULL CHECK(mp >= 0),
                sp INTEGER NOT NULL CHECK(sp >= 0),
                attack_power INTEGER NOT NULL CHECK(attack_power >= 0),
                defense_power INTEGER NOT NULL CHECK(defense_power >= 0),
                magic_attack_power INTEGER NOT NULL CHECK(magic_attack_power >= 0),
                normal_attack_interval_ms INTEGER NOT NULL DEFAULT 2000
                    CHECK(normal_attack_interval_ms >= 1),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )

        # V18.3: the player's normal-attack interval is a profession base
        # attribute, just like HP/MP/SP and attack power.  Add it once to old
        # databases and seed the three confirmed level-1 profession values.
        profession_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(profession_level1_stats)"
            ).fetchall()
        }
        if "normal_attack_interval_ms" not in profession_columns:
            connection.execute(
                "ALTER TABLE profession_level1_stats "
                "ADD COLUMN normal_attack_interval_ms INTEGER NOT NULL DEFAULT 2000"
            )
            connection.execute(
                """
                UPDATE profession_level1_stats
                SET normal_attack_interval_ms = CASE profession
                    WHEN 1000 THEN 2000
                    WHEN 2000 THEN 3000
                    WHEN 3000 THEN 2200
                    ELSE normal_attack_interval_ms
                END
                """
            )

        migration_key = "default_profession_level1_stats_seed_v17_34"
        if self._has_migration(connection, migration_key):
            return

        row_count = int(connection.execute(
            "SELECT COUNT(*) FROM profession_level1_stats"
        ).fetchone()[0])

        # A newly created table has no administrator-owned rows, whether this
        # is a brand-new database or an upgrade from V17.33. Seed it once.
        if not table_existed and row_count == 0:
            connection.executemany(
                """
                INSERT INTO profession_level1_stats (
                    profession, hp, mp, sp, attack_power, defense_power,
                    magic_attack_power, normal_attack_interval_ms, enabled, note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                DEFAULT_PROFESSION_LEVEL1_STAT_ROWS,
            )
        # If the table already existed, every row state (including empty) is
        # considered administrator-owned and must be preserved exactly.
        self._mark_migration(connection, migration_key)

    @staticmethod
    def _load_profession_level1_stats_row(
        connection: sqlite3.Connection, profession: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT
                profession, hp, mp, sp, attack_power, defense_power,
                magic_attack_power, normal_attack_interval_ms, enabled, note
            FROM profession_level1_stats
            WHERE profession = ? AND enabled = 1
            """,
            (int(profession),),
        ).fetchone()

    def get_profession_level1_stats(
        self, profession: int
    ) -> sqlite3.Row | None:
        """Return the enabled pre-attribute profession base for a new role."""
        with self.session() as connection:
            return self._load_profession_level1_stats_row(
                connection, int(profession)
            )

    def _migrate_characters_schema(self, connection: sqlite3.Connection) -> None:
        """Add missing role columns and keep characters table column order readable."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if not columns:
            return
        add_columns = (
            ("ancillary_profession", "INTEGER NOT NULL DEFAULT 0"),
            ("max_hp", "INTEGER NOT NULL DEFAULT 300"),
            ("max_mp", "INTEGER NOT NULL DEFAULT 100"),
            ("sp", "INTEGER NOT NULL DEFAULT 200"),
            ("max_sp", "INTEGER NOT NULL DEFAULT 200"),
            ("hp_regen_per_second", "INTEGER NOT NULL DEFAULT 0"),
            ("mp_regen_per_second", "INTEGER NOT NULL DEFAULT 0"),
            ("sp_regen_per_second", "INTEGER NOT NULL DEFAULT 0"),
            ("organization_label", "TEXT NOT NULL DEFAULT '无'"),
            ("country_label", "TEXT NOT NULL DEFAULT '无'"),
            ("reborn_label", "TEXT NOT NULL DEFAULT '00'"),
            ("strength", "INTEGER NOT NULL DEFAULT 1"),
            ("wisdom", "INTEGER NOT NULL DEFAULT 1"),
            ("dexterity", "INTEGER NOT NULL DEFAULT 1"),
            ("constitution", "INTEGER NOT NULL DEFAULT 1"),
            ("attack_power", "INTEGER NOT NULL DEFAULT 20"),
            ("defense_power", "INTEGER NOT NULL DEFAULT 10"),
            ("magic_attack_power", "INTEGER NOT NULL DEFAULT 20"),
            ("earth_resistance", "INTEGER NOT NULL DEFAULT 0"),
            ("water_resistance", "INTEGER NOT NULL DEFAULT 0"),
            ("fire_resistance", "INTEGER NOT NULL DEFAULT 0"),
            ("wind_resistance", "INTEGER NOT NULL DEFAULT 0"),
            ("light_resistance", "INTEGER NOT NULL DEFAULT 0"),
            ("dark_resistance", "INTEGER NOT NULL DEFAULT 0"),
            ("reputation", "INTEGER NOT NULL DEFAULT 0"),
            ("evil", "INTEGER NOT NULL DEFAULT 0"),
            ("kill_count", "INTEGER NOT NULL DEFAULT 0"),
            ("pk_win_count", "INTEGER NOT NULL DEFAULT 0"),
            ("pk_loss_count", "INTEGER NOT NULL DEFAULT 0"),
            ("experience", "INTEGER NOT NULL DEFAULT 100"),
            ("bag_capacity", "INTEGER NOT NULL DEFAULT 40"),
            ("active_weapon_set", "INTEGER NOT NULL DEFAULT 1"),
            ("last_logout_area", "TEXT"),
            ("player_gold", "INTEGER NOT NULL DEFAULT 888"),
            ("depot_gold", "INTEGER NOT NULL DEFAULT 777"),
        )
        for column_name, column_sql in add_columns:
            if column_name not in columns:
                connection.execute(
                    f"ALTER TABLE characters ADD COLUMN {column_name} {column_sql}"
                )
        self._reorder_characters_table_if_needed(connection)

    def _reorder_characters_table_if_needed(self, connection: sqlite3.Connection) -> None:
        current_order = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        ]
        if current_order == list(CHARACTER_COLUMN_ORDER):
            return

        missing = [name for name in CHARACTER_COLUMN_ORDER if name not in current_order]
        if missing:
            raise RuntimeError(
                "Cannot reorder characters table; missing column(s): "
                + ", ".join(missing)
            )

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        try:
            connection.execute("ALTER TABLE characters RENAME TO characters_migration_old")
            connection.execute(CHARACTERS_CREATE_SQL)
            column_csv = ", ".join(CHARACTER_COLUMN_ORDER)
            connection.execute(
                f"""
                INSERT INTO characters ({column_csv})
                SELECT {column_csv}
                FROM characters_migration_old
                """
            )
            connection.execute("DROP TABLE characters_migration_old")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_characters_account_id ON characters(account_id)"
            )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "Foreign-key check failed after characters table reorder: "
                    + repr(violations)
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_character_skills_schema(self, connection: sqlite3.Connection) -> None:
        """Add persisted skill columns to older bundled databases."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(character_skills)").fetchall()
        }
        if not columns:
            return
        add_columns = (
            ("field04_unknown", "INTEGER NOT NULL DEFAULT 1"),
            ("field0c_unknown", "INTEGER NOT NULL DEFAULT 1"),
            ("field0e_unknown", "INTEGER NOT NULL DEFAULT 1"),
            ("field1c_u32_array", "TEXT NOT NULL DEFAULT ''"),
        )
        for column_name, column_sql in add_columns:
            if column_name not in columns:
                connection.execute(
                    f"ALTER TABLE character_skills ADD COLUMN {column_name} {column_sql}"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_character_skills_character_id ON character_skills(character_id)"
        )

    def _migrate_skill_upgrade_schema(self, connection: sqlite3.Connection) -> None:
        """Create DB-owned skill upgrade rules and seed them exactly once."""
        had_cost_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_upgrade_costs'"
        ).fetchone() is not None
        had_effect_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_upgrade_effects'"
        ).fetchone() is not None
        had_milestone_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_upgrade_milestone_effects'"
        ).fetchone() is not None

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_upgrade_costs (
                skill_id INTEGER NOT NULL DEFAULT 0,
                from_level INTEGER NOT NULL,
                to_level INTEGER NOT NULL,
                exp_cost INTEGER NOT NULL DEFAULT 0,
                gold_cost INTEGER NOT NULL DEFAULT 0,
                required_character_level INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(skill_id, from_level)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_upgrade_effects (
                profession INTEGER NOT NULL DEFAULT 0,
                skill_id INTEGER NOT NULL,
                character_column TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(profession, skill_id, character_column)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_upgrade_milestone_effects (
                profession INTEGER NOT NULL DEFAULT 0,
                skill_id INTEGER NOT NULL,
                character_column TEXT NOT NULL,
                first_target_level INTEGER NOT NULL DEFAULT 2,
                interval_levels INTEGER NOT NULL DEFAULT 2,
                amount INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(profession, skill_id, character_column)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_skill_upgrade_cost_lookup "
            "ON skill_upgrade_costs(skill_id, from_level, enabled)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_skill_upgrade_effect_lookup "
            "ON skill_upgrade_effects(profession, skill_id, enabled)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_skill_upgrade_milestone_lookup "
            "ON skill_upgrade_milestone_effects(profession, skill_id, enabled)"
        )

        # Seed only when the table itself is introduced. Reopening the server,
        # deleting rows, or editing values never invokes this block again.
        if not had_cost_table:
            rows = []

            # skill_id=0 is the fallback used by profession skills.
            for from_level, exp_cost in (
                PROFESSION_SKILL_UPGRADE_EXP_BY_FROM_LEVEL.items()
            ):
                rows.append((
                    0, from_level, from_level + 1, exp_cost, 0,
                    from_level + 1, 1,
                    "职业技能通用规则：人物等级须达到目标技能等级",
                ))

            # skill_id=1 is the character-level skill.
            for from_level, exp_cost in LEVEL_SKILL_UPGRADE_EXP_BY_FROM_LEVEL.items():
                rows.append((
                    1, from_level, from_level + 1, exp_cost, 0,
                    from_level, 1,
                    "人物等级技能：允许从当前人物等级提升到下一级",
                ))

            # skill_id=2..8 are HP/MP/SP/STR/DEX/INT/CON. They share the
            # supplied attribute-cost curve but remain separate DB rows so an
            # administrator can tune each attribute independently later.
            for skill_id in ATTRIBUTE_SKILL_IDS:
                for from_level, exp_cost in (
                    ATTRIBUTE_SKILL_UPGRADE_EXP_BY_FROM_LEVEL.items()
                ):
                    rows.append((
                        skill_id, from_level, from_level + 1, exp_cost, 0,
                        from_level + 1, 1,
                        "人物基础属性：人物等级须达到目标属性等级",
                    ))

            connection.executemany(
                """
                INSERT INTO skill_upgrade_costs (
                    skill_id, from_level, to_level, exp_cost, gold_cost,
                    required_character_level, enabled, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        if not had_effect_table:
            connection.executemany(
                """
                INSERT INTO skill_upgrade_effects (
                    profession, skill_id, character_column, amount, enabled, note
                ) VALUES (?, ?, ?, ?, 1, ?)
                """,
                DEFAULT_SKILL_UPGRADE_EFFECT_ROWS,
            )

        if not had_milestone_table:
            connection.executemany(
                """
                INSERT INTO skill_upgrade_milestone_effects (
                    profession, skill_id, character_column, first_target_level,
                    interval_levels, amount, enabled, note
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                DEFAULT_SKILL_UPGRADE_MILESTONE_ROWS,
            )

    @staticmethod
    def _parse_legacy_attribute_pairs(value: object) -> list[tuple[int, int]]:
        """Best-effort parser for V17.13 TEXT attribute fields."""
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            data = list(value)
        else:
            text = str(value).strip()
            if not text:
                return []
            try:
                data = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                match = re.fullmatch(r"\s*(-?\d+)\s*[:;,]\s*(-?\d+)\s*", text)
                return [(int(match.group(1)), int(match.group(2)))] if match else []
        if isinstance(data, dict):
            key = data.get("key", data.get("attr_key"))
            val = data.get("value", data.get("attr_value"))
            return [(int(key), int(val))] if key is not None and val is not None else []
        if not isinstance(data, list):
            return []
        if len(data) == 2 and all(not isinstance(x, (list, tuple, dict)) for x in data):
            try:
                return [(int(data[0]), int(data[1]))]
            except (TypeError, ValueError):
                return []
        pairs: list[tuple[int, int]] = []
        for entry in data:
            pairs.extend(Database._parse_legacy_attribute_pairs(entry))
        return pairs

    def _migrate_equipment_schema(self, connection: sqlite3.Connection) -> None:
        """Create V17.14 equipment templates with exactly one base special attr."""
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "equip" not in tables:
            connection.execute(EQUIP_CREATE_SQL)
        else:
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(equip)").fetchall()
            }
            if "base_special_attr_key" not in columns or "base_special_attr_value" not in columns:
                old_rows = [dict(row) for row in connection.execute("SELECT * FROM equip").fetchall()]
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                try:
                    connection.execute("ALTER TABLE equip RENAME TO equip_v17_13_old")
                    connection.execute(EQUIP_CREATE_SQL)
                    for row in old_rows:
                        pairs = self._parse_legacy_attribute_pairs(row.get("special_attrs", ""))
                        base_key, base_value = pairs[0] if pairs else (None, None)
                        connection.execute(
                            """
                            INSERT INTO equip(
                                id,resource_key,name,equip_type,attack_power,attack_power_max,magic_power,
                                defense_power,speed,required_strength,required_dexterity,
                                required_intelligence,required_constitution,
                                required_profession,client_class_limit_code,required_level,
                                attack_range,attack_range_max,durability,price,base_special_attr_key,
                                base_special_attr_value,enabled
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                int(row["id"]), str(row.get("resource_key") or f"{int(row['id']):06x}"),
                                str(row.get("name") or f"未定义装备 0x{int(row['id']):06X}"),
                                int(row.get("equip_type") or 0), int(row.get("attack_power") or 0),
                                int(row.get("attack_power_max") or row.get("attack_power") or 0),
                                int(row.get("magic_power") or 0), int(row.get("defense_power") or 0),
                                int(row.get("speed") or 0), int(row.get("required_strength") or 0),
                                int(row.get("required_dexterity") or 0),
                                int(row.get("required_intelligence") or 0),
                                int(row.get("required_constitution") or 0),
                                int(row.get("required_profession") or 0),
                                int(row.get("client_class_limit_code") or 0),
                                int(row.get("required_level") or 0), int(row.get("attack_range") or 0),
                                int(row.get("attack_range_max") or row.get("attack_range") or 0),
                                int(row.get("durability") or 0), int(row.get("price") or 0),
                                base_key, base_value, int(row.get("enabled", 1) or 1),
                            ),
                        )
                    connection.execute("DROP TABLE equip_v17_13_old")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
            else:
                if "attack_power_max" not in columns:
                    connection.execute("ALTER TABLE equip ADD COLUMN attack_power_max INTEGER NOT NULL DEFAULT 0")
                    connection.execute("UPDATE equip SET attack_power_max=attack_power WHERE attack_power_max=0")
                if "attack_range_max" not in columns:
                    connection.execute("ALTER TABLE equip ADD COLUMN attack_range_max INTEGER NOT NULL DEFAULT 0")
                    connection.execute("UPDATE equip SET attack_range_max=attack_range WHERE attack_range_max=0")
                if "enabled" not in columns:
                    connection.execute("ALTER TABLE equip ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
                if "client_class_limit_code" not in columns:
                    connection.execute(
                        "ALTER TABLE equip ADD COLUMN client_class_limit_code INTEGER NOT NULL DEFAULT 0"
                    )

        # Runtime migrations are structural only.  Do not zero legacy columns
        # or restore deleted templates here: equip is administrator-owned data.
        self._mark_default_seed_owned_if_existing(
            connection,
            "default_equipment_data_seed_v17_29",
            int(connection.execute("SELECT COUNT(*) FROM equip").fetchone()[0]),
        )

    def seed_default_equipment_data(self, connection: sqlite3.Connection) -> bool:
        """Insert built-in equipment templates once for a new empty database."""
        migration_key = "default_equipment_data_seed_v17_29"
        if self._has_migration(connection, migration_key):
            return False
        if int(connection.execute("SELECT COUNT(*) FROM equip").fetchone()[0]) != 0:
            self._mark_migration(connection, migration_key)
            return False
        connection.executemany(
            """
            INSERT INTO equip(
                id,resource_key,name,equip_type,attack_power,attack_power_max,magic_power,
                defense_power,speed,required_strength,required_dexterity,
                required_intelligence,required_constitution,
                required_profession,client_class_limit_code,required_level,attack_range,attack_range_max,
                durability,price,base_special_attr_key,base_special_attr_value,enabled
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            DEFAULT_EQUIP_ROWS,
        )
        self._mark_migration(connection, migration_key)
        return True

    def _migrate_character_stat_sources_schema(
        self, connection: sqlite3.Connection
    ) -> None:
        """Create additive stat-source tables without rewriting characters.

        Existing aggregate columns remain untouched for compatibility, but the
        runtime stat engine no longer treats them as authoritative.  On first
        migration, any difference that cannot be explained by profession base
        plus current skill levels is preserved as an explicit ``legacy_import``
        adjustment, so the effective values remain byte-for-byte equivalent.
        """
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS character_base_stats (
                character_id INTEGER PRIMARY KEY,
                strength INTEGER NOT NULL DEFAULT 0,
                dexterity INTEGER NOT NULL DEFAULT 0,
                wisdom INTEGER NOT NULL DEFAULT 0,
                constitution INTEGER NOT NULL DEFAULT 0,
                attack_power INTEGER NOT NULL DEFAULT 0,
                defense_power INTEGER NOT NULL DEFAULT 0,
                magic_attack_power INTEGER NOT NULL DEFAULT 0,
                earth_resistance INTEGER NOT NULL DEFAULT 0,
                water_resistance INTEGER NOT NULL DEFAULT 0,
                fire_resistance INTEGER NOT NULL DEFAULT 0,
                wind_resistance INTEGER NOT NULL DEFAULT 0,
                light_resistance INTEGER NOT NULL DEFAULT 0,
                dark_resistance INTEGER NOT NULL DEFAULT 0,
                max_hp INTEGER NOT NULL DEFAULT 0,
                max_mp INTEGER NOT NULL DEFAULT 0,
                max_sp INTEGER NOT NULL DEFAULT 0,
                hp_regen_per_second INTEGER NOT NULL DEFAULT 0,
                mp_regen_per_second INTEGER NOT NULL DEFAULT 0,
                sp_regen_per_second INTEGER NOT NULL DEFAULT 0,
                normal_attack_interval_ms INTEGER NOT NULL DEFAULT 2000,
                FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS character_stat_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                stat_key TEXT NOT NULL,
                flat_value INTEGER NOT NULL DEFAULT 0,
                percent_bp INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                note TEXT NOT NULL DEFAULT '',
                UNIQUE(character_id, source_key, stat_key),
                FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_stat_modifiers (
                profession INTEGER NOT NULL DEFAULT 0,
                skill_id INTEGER NOT NULL,
                stat_key TEXT NOT NULL,
                flat_per_level INTEGER NOT NULL DEFAULT 0,
                percent_bp_per_level INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(profession, skill_id, stat_key)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS equipment_stat_modifiers (
                template_id INTEGER NOT NULL,
                stat_key TEXT NOT NULL,
                flat_value INTEGER NOT NULL DEFAULT 0,
                percent_bp INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(template_id, stat_key),
                FOREIGN KEY(template_id) REFERENCES equip(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_character_stat_adjustments_character "
            "ON character_stat_adjustments(character_id)"
        )

        # Import one explicit source decomposition for every pre-existing role.
        rows = connection.execute(
            "SELECT * FROM characters ORDER BY id"
        ).fetchall()
        for character in rows:
            character_id = int(character["id"])
            exists = connection.execute(
                "SELECT 1 FROM character_base_stats WHERE character_id = ?",
                (character_id,),
            ).fetchone()
            if exists is not None:
                continue
            base_row = self._load_profession_level1_stats_row(
                connection, int(character["profession"])
            )
            if base_row is None:
                # No profession base is available.  Use a neutral base and keep
                # the full unexplained remainder as an explicit legacy source;
                # this still preserves the old result without double-counting
                # current skill levels.
                base_values = {stat.value: 0 for stat in StatKey}
                base_values[StatKey.NORMAL_ATTACK_INTERVAL_MS.value] = 2000
            else:
                base_values = self._profession_base_stat_values(base_row)
            self._insert_character_base_stats(connection, character_id, base_values)
            skill_totals = self._calculate_persistent_skill_flat_totals(
                connection, character_id, int(character["profession"])
            )
            for stat_key in STAT_KEY_NAMES:
                if stat_key == StatKey.NORMAL_ATTACK_INTERVAL_MS.value:
                    continue
                legacy_value = int(character[stat_key])
                expected = int(base_values.get(stat_key, 0)) + int(
                    skill_totals.get(stat_key, 0)
                )
                residual = legacy_value - expected
                if residual:
                    connection.execute(
                        """
                        INSERT INTO character_stat_adjustments(
                            character_id, source_key, stat_key, flat_value,
                            percent_bp, enabled, note
                        ) VALUES (?, 'legacy_import', ?, ?, 0, 1, ?)
                        """,
                        (
                            character_id,
                            stat_key,
                            residual,
                            "Preserved unexplained value from legacy aggregate column",
                        ),
                    )

    @staticmethod
    def _profession_base_stat_values(base_row: sqlite3.Row) -> dict[str, int]:
        return {
            "strength": 0,
            "dexterity": 0,
            "wisdom": 0,
            "constitution": 0,
            "attack_power": int(base_row["attack_power"]),
            "defense_power": int(base_row["defense_power"]),
            "magic_attack_power": int(base_row["magic_attack_power"]),
            "earth_resistance": 0,
            "water_resistance": 0,
            "fire_resistance": 0,
            "wind_resistance": 0,
            "light_resistance": 0,
            "dark_resistance": 0,
            "max_hp": int(base_row["hp"]),
            "max_mp": int(base_row["mp"]),
            "max_sp": int(base_row["sp"]),
            "hp_regen_per_second": 0,
            "mp_regen_per_second": 0,
            "sp_regen_per_second": 0,
            "normal_attack_interval_ms": int(base_row["normal_attack_interval_ms"]),
        }

    @staticmethod
    def _insert_character_base_stats(
        connection: sqlite3.Connection,
        character_id: int,
        values: dict[str, int],
    ) -> None:
        columns = [stat.value for stat in StatKey]
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO character_base_stats(character_id, {', '.join(columns)}) "
            f"VALUES (?, {placeholders})",
            (int(character_id), *(int(values.get(column, 0)) for column in columns)),
        )

    def _calculate_persistent_skill_flat_totals(
        self,
        connection: sqlite3.Connection,
        character_id: int,
        profession: int,
    ) -> dict[str, int]:
        totals = {stat_key: 0 for stat_key in STAT_KEY_NAMES}
        rows = connection.execute(
            "SELECT skill_id, skill_level FROM character_skills WHERE character_id = ?",
            (int(character_id),),
        ).fetchall()
        primary_map = {
            0x0005: StatKey.STRENGTH.value,
            0x0006: StatKey.DEXTERITY.value,
            0x0007: StatKey.WISDOM.value,
            0x0008: StatKey.CONSTITUTION.value,
        }
        for row in rows:
            skill_id = int(row["skill_id"])
            level = max(0, int(row["skill_level"]))
            primary = primary_map.get(skill_id)
            if primary is not None:
                totals[primary] += level
            for stat_key, amount in self._load_skill_upgrade_effects(
                connection, int(profession), skill_id
            ).items():
                if stat_key in totals:
                    totals[stat_key] += int(amount) * level
            for stat_key, (first, interval, amount) in (
                self._load_skill_upgrade_milestone_effects(
                    connection, int(profession), skill_id
                ).items()
            ):
                if stat_key in totals:
                    totals[stat_key] += (
                        self._milestone_count_at_level(level, first, interval)
                        * int(amount)
                    )
        return totals

    def _migrate_confirmed_level1_rules_v18_11(
        self, connection: sqlite3.Connection
    ) -> None:
        """Apply the confirmed level-1 profession/attribute rules once.

        V18.10 accidentally seeded the mage pre-skill physical attack as 12.
        The confirmed value is 10.  Update only rows that still exactly match
        the old built-in seed, so administrator-owned custom bases are never
        overwritten.  Existing per-character base rows are treated the same
        way.

        The seven attribute skills remain separate persistent skill levels:
        0x0002 HP, 0x0003 MP, 0x0004 SP, 0x0005 STR, 0x0006 DEX,
        0x0007 WIS and 0x0008 CON.  Character level itself has no stat effect.
        """
        migration_key = "confirmed_level1_rules_v18_11"
        if self._has_migration(connection, migration_key):
            return

        connection.execute(
            """
            UPDATE profession_level1_stats
            SET attack_power = 10,
                note = '法师职业基础值（七项属性加成前）'
            WHERE profession = 2000
              AND hp = 180 AND mp = 200 AND sp = 60
              AND attack_power = 12
              AND defense_power = 5
              AND magic_attack_power = 18
              AND normal_attack_interval_ms = 3000
            """
        )

        # Change only per-character base rows that are still the exact old
        # built-in mage seed.  Custom bases and imported residual adjustments
        # are intentionally left untouched.
        mage_rows = connection.execute(
            """
            SELECT cbs.character_id
            FROM character_base_stats AS cbs
            JOIN characters AS c ON c.id = cbs.character_id
            WHERE c.profession = 2000
              AND cbs.strength = 0
              AND cbs.dexterity = 0
              AND cbs.wisdom = 0
              AND cbs.constitution = 0
              AND cbs.attack_power = 12
              AND cbs.defense_power = 5
              AND cbs.magic_attack_power = 18
              AND cbs.earth_resistance = 0
              AND cbs.water_resistance = 0
              AND cbs.fire_resistance = 0
              AND cbs.wind_resistance = 0
              AND cbs.light_resistance = 0
              AND cbs.dark_resistance = 0
              AND cbs.max_hp = 180
              AND cbs.max_mp = 200
              AND cbs.max_sp = 60
              AND cbs.hp_regen_per_second = 0
              AND cbs.mp_regen_per_second = 0
              AND cbs.sp_regen_per_second = 0
              AND cbs.normal_attack_interval_ms = 3000
            """
        ).fetchall()
        mage_character_ids = [int(row["character_id"]) for row in mage_rows]
        if mage_character_ids:
            placeholders = ", ".join("?" for _ in mage_character_ids)
            connection.execute(
                f"UPDATE character_base_stats SET attack_power = 10 "
                f"WHERE character_id IN ({placeholders})",
                mage_character_ids,
            )

            # Legacy aggregate columns are not authoritative, but keep the
            # exact untouched built-in aggregate coherent for old tools.  A
            # customized aggregate is never rewritten.
            for character_id in mage_character_ids:
                skill_totals = self._calculate_persistent_skill_flat_totals(
                    connection, character_id, 2000
                )
                old_expected = 12 + int(skill_totals.get("attack_power", 0))
                new_expected = 10 + int(skill_totals.get("attack_power", 0))
                connection.execute(
                    """
                    UPDATE characters
                    SET attack_power = ?
                    WHERE id = ? AND attack_power = ?
                    """,
                    (new_expected, character_id, old_expected),
                )

        self._mark_migration(connection, migration_key)

    def _migrate_shop_schema(self, connection: sqlite3.Connection) -> None:
        """Keep shops database-backed with only npc_shops + shop_items."""
        existing_tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # V17.26 used shops + npc_shops + shop_items plus a convenience view.
        # Preserve its rows while collapsing the one-NPC/one-shop relation.
        old_npc_rows: list[dict[str, object]] = []
        old_item_rows: list[dict[str, object]] = []
        npc_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(npc_shops)").fetchall()
        } if "npc_shops" in existing_tables else set()
        old_layout = bool(npc_columns) and "shop_id" in npc_columns and "shop_title" not in npc_columns
        if old_layout:
            old_npc_rows = [dict(row) for row in connection.execute(
                """
                SELECT ns.map_id,ns.npc_display_name,ns.npc_being_name,
                       ns.shop_id,ns.interaction_field0c,ns.enabled,
                       s.shop_key,s.default_title AS shop_title,
                       s.category_label,s.category_id,s.enabled AS shop_enabled
                FROM npc_shops AS ns
                JOIN shops AS s ON s.id=ns.shop_id
                ORDER BY ns.shop_id,ns.map_id,ns.npc_display_name
                """
            ).fetchall()]
            old_item_rows = [dict(row) for row in connection.execute(
                "SELECT shop_id,equip_id,sort_order,enabled FROM shop_items ORDER BY shop_id,sort_order,equip_id"
            ).fetchall()]
            connection.execute("DROP VIEW IF EXISTS shop_catalog")
            connection.execute("DROP TABLE shop_items")
            connection.execute("DROP TABLE npc_shops")
            connection.execute("DROP TABLE shops")
        else:
            connection.execute("DROP VIEW IF EXISTS shop_catalog")
            # A standalone shops table is obsolete in the simplified schema.
            if "shops" in existing_tables:
                connection.execute("DROP TABLE IF EXISTS shops")

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS npc_shops (
                id INTEGER PRIMARY KEY,
                shop_key TEXT NOT NULL UNIQUE,
                map_id INTEGER NOT NULL,
                npc_display_name TEXT NOT NULL,
                npc_being_name TEXT NOT NULL DEFAULT '',
                shop_title TEXT NOT NULL,
                category_label TEXT NOT NULL DEFAULT '一般',
                category_id INTEGER NOT NULL DEFAULT 190,
                interaction_field0c INTEGER NOT NULL DEFAULT 5,
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(map_id, npc_display_name),
                FOREIGN KEY(map_id) REFERENCES maps(id)
            );

            CREATE TABLE IF NOT EXISTS shop_items (
                npc_shop_id INTEGER NOT NULL,
                equip_id INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                client_sort_type INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(npc_shop_id, equip_id),
                FOREIGN KEY(npc_shop_id) REFERENCES npc_shops(id) ON DELETE CASCADE,
                FOREIGN KEY(equip_id) REFERENCES equip(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_shop_items_sort
            ON shop_items(npc_shop_id, sort_order);
            """
        )

        # V17.30: the client re-sorts shop rows by 0x000B field4.  Keep a
        # shop-only override in shop_items so administrators can control the
        # visible order without changing equip.equip_type (which controls the
        # actual equipment slot after purchase).
        shop_item_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(shop_items)"
            ).fetchall()
        }
        if "client_sort_type" not in shop_item_columns:
            connection.execute(
                "ALTER TABLE shop_items ADD COLUMN client_sort_type INTEGER"
            )

        if old_npc_rows:
            shop_id_map: dict[int, list[int]] = {}
            next_id = 1
            for row in old_npc_rows:
                new_id = next_id
                next_id += 1
                old_shop_id = int(row["shop_id"])
                shop_id_map.setdefault(old_shop_id, []).append(new_id)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO npc_shops(
                        id,shop_key,map_id,npc_display_name,npc_being_name,
                        shop_title,category_label,category_id,
                        interaction_field0c,enabled
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id,
                        str(row["shop_key"]) if len(shop_id_map[old_shop_id]) == 1
                        else f"{row['shop_key']}_{new_id}",
                        int(row["map_id"]), str(row["npc_display_name"]),
                        str(row["npc_being_name"] or ""), str(row["shop_title"]),
                        str(row["category_label"]), int(row["category_id"]),
                        int(row["interaction_field0c"]),
                        1 if int(row["enabled"]) and int(row["shop_enabled"]) else 0,
                    ),
                )
            for row in old_item_rows:
                for new_id in shop_id_map.get(int(row["shop_id"]), []):
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO shop_items(
                            npc_shop_id,equip_id,sort_order,enabled
                        ) VALUES (?,?,?,?)
                        """,
                        (new_id,int(row["equip_id"]),int(row["sort_order"]),int(row["enabled"])),
                    )

        existing_shop_rows = int(connection.execute(
            "SELECT COUNT(*) FROM npc_shops"
        ).fetchone()[0]) + int(connection.execute(
            "SELECT COUNT(*) FROM shop_items"
        ).fetchone()[0])
        self._mark_default_seed_owned_if_existing(
            connection, "default_shop_data_seed_v17_29", existing_shop_rows
        )

    def seed_default_shop_data(self, connection: sqlite3.Connection) -> bool:
        """Seed built-in shops only for a genuinely empty shop database.

        This method is intentionally not called by session().  Runtime schema
        migrations must never recreate rows that an administrator deleted or
        disabled in npc_shops/shop_items.  init_database.py calls this method
        explicitly when preparing a new database.

        Returns True when defaults were inserted, otherwise False.
        """
        migration_key = "default_shop_data_seed_v17_29"
        if self._has_migration(connection, migration_key):
            return False

        maps_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'maps'
            """
        ).fetchone()
        if maps_exists is None:
            return False

        shop_count = int(connection.execute(
            "SELECT COUNT(*) FROM npc_shops"
        ).fetchone()[0])
        item_count = int(connection.execute(
            "SELECT COUNT(*) FROM shop_items"
        ).fetchone()[0])

        # Any existing row means the database is administrator-owned data, not
        # an empty database waiting for defaults.  In particular, a partially
        # deleted/default shop list must be preserved exactly as edited.
        if shop_count != 0 or item_count != 0:
            self._mark_migration(connection, migration_key)
            return False

        connection.executemany(
            """
            INSERT INTO npc_shops(
                id,shop_key,map_id,npc_display_name,npc_being_name,
                shop_title,category_label,category_id,
                interaction_field0c,enabled
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            DEFAULT_NPC_SHOP_ROWS,
        )
        for npc_shop_id, equip_ids in DEFAULT_SHOP_ITEM_IDS.items():
            connection.executemany(
                """
                INSERT INTO shop_items(
                    npc_shop_id,equip_id,sort_order,enabled
                ) VALUES (?,?,?,1)
                """,
                [
                    (int(npc_shop_id), int(equip_id), int(index))
                    for index, equip_id in enumerate(equip_ids, start=1)
                ],
            )
        self._mark_migration(connection, migration_key)
        return True

    def list_npc_shops(self) -> list[sqlite3.Row]:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT id,shop_key,map_id,npc_display_name,npc_being_name,
                       shop_title,category_label,category_id,
                       interaction_field0c,enabled
                FROM npc_shops
                WHERE enabled <> 0
                ORDER BY id
                """
            ).fetchall()

    def list_shop_items(self) -> list[sqlite3.Row]:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT npc_shop_id,equip_id,sort_order,client_sort_type,enabled
                FROM shop_items
                WHERE enabled <> 0
                ORDER BY npc_shop_id,sort_order,equip_id
                """
            ).fetchall()

    def _ensure_character_equip_rows(
        self, connection: sqlite3.Connection, character_id: int | None = None,
    ) -> None:
        if character_id is None:
            character_ids = [int(row["id"]) for row in connection.execute(
                "SELECT id FROM characters"
            ).fetchall()]
        else:
            character_ids = [int(character_id)]
        for parsed_character_id in character_ids:
            for slot_code, weapon_set, slot_name in ALL_EQUIP_SLOT_ROWS:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO character_equip(
                        character_id,slot_code,weapon_set,slot_name
                    ) VALUES (?,?,?,?)
                    """,
                    (parsed_character_id, slot_code, weapon_set, slot_name),
                )

    def _is_v17_14_inventory_schema(self, connection: sqlite3.Connection) -> bool:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"equip_instance", "equip_instance_attr", "character_bag", "character_equip"}.issubset(tables):
            return False
        bag_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(character_bag)")}
        equip_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(character_equip)")}
        return "instance_id" in bag_columns and "item_id" not in bag_columns and "instance_id" in equip_columns

    def _create_v17_14_inventory_indexes_and_view(self, connection: sqlite3.Connection) -> None:
        connection.execute("CREATE INDEX IF NOT EXISTS idx_equip_instance_owner ON equip_instance(owner_character_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_equip_instance_template ON equip_instance(template_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_character_bag_character ON character_bag(character_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_character_equip_character ON character_equip(character_id)")
        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS trg_character_bag_instance_insert
            BEFORE INSERT ON character_bag
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM equip_instance
                    WHERE id=NEW.instance_id AND owner_character_id=NEW.character_id
                ) THEN RAISE(ABORT, 'bag instance owner mismatch') END;
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM character_equip WHERE instance_id=NEW.instance_id
                ) THEN RAISE(ABORT, 'instance is already equipped') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_character_bag_instance_update
            BEFORE UPDATE OF character_id,instance_id ON character_bag
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM equip_instance
                    WHERE id=NEW.instance_id AND owner_character_id=NEW.character_id
                ) THEN RAISE(ABORT, 'bag instance owner mismatch') END;
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM character_equip WHERE instance_id=NEW.instance_id
                ) THEN RAISE(ABORT, 'instance is already equipped') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_character_equip_instance_insert
            BEFORE INSERT ON character_equip WHEN NEW.instance_id IS NOT NULL
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM equip_instance
                    WHERE id=NEW.instance_id AND owner_character_id=NEW.character_id
                ) THEN RAISE(ABORT, 'equipped instance owner mismatch') END;
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM character_bag WHERE instance_id=NEW.instance_id
                ) THEN RAISE(ABORT, 'instance is still in bag') END;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_character_equip_instance_update
            BEFORE UPDATE OF character_id,instance_id ON character_equip
            WHEN NEW.instance_id IS NOT NULL
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM equip_instance
                    WHERE id=NEW.instance_id AND owner_character_id=NEW.character_id
                ) THEN RAISE(ABORT, 'equipped instance owner mismatch') END;
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM character_bag WHERE instance_id=NEW.instance_id
                ) THEN RAISE(ABORT, 'instance is still in bag') END;
            END;
            """
        )
        connection.execute("DROP VIEW IF EXISTS character_equip_layout")
        connection.execute(
            """
            CREATE VIEW character_equip_layout AS
            SELECT character_id,
                MAX(CASE WHEN slot_code=210 AND weapon_set=1 THEN instance_id END) AS weapon1,
                MAX(CASE WHEN slot_code=211 AND weapon_set=1 THEN instance_id END) AS shield1,
                MAX(CASE WHEN slot_code=210 AND weapon_set=2 THEN instance_id END) AS weapon2,
                MAX(CASE WHEN slot_code=211 AND weapon_set=2 THEN instance_id END) AS shield2,
                MAX(CASE WHEN slot_code=201 THEN instance_id END) AS armour,
                MAX(CASE WHEN slot_code=203 THEN instance_id END) AS pants,
                MAX(CASE WHEN slot_code=204 THEN instance_id END) AS shoes,
                MAX(CASE WHEN slot_code=205 THEN instance_id END) AS gloves,
                MAX(CASE WHEN slot_code=200 THEN instance_id END) AS helmet,
                MAX(CASE WHEN slot_code=202 THEN instance_id END) AS belt,
                MAX(CASE WHEN slot_code=206 THEN instance_id END) AS right_ring,
                MAX(CASE WHEN slot_code=207 THEN instance_id END) AS left_ring,
                MAX(CASE WHEN slot_code=209 THEN instance_id END) AS earring,
                MAX(CASE WHEN slot_code=208 THEN instance_id END) AS necklace,
                MAX(CASE WHEN slot_code=214 THEN instance_id END) AS badge1,
                MAX(CASE WHEN slot_code=215 THEN instance_id END) AS badge2,
                MAX(CASE WHEN slot_code=216 THEN instance_id END) AS badge3
            FROM character_equip GROUP BY character_id
            """
        )

    def _migrate_v17_13_inventory_to_instances(self, connection: sqlite3.Connection) -> None:
        """Split V17.13 owner rows into instance data and location indexes."""
        old_bag_rows = [dict(row) for row in connection.execute(
            "SELECT * FROM character_bag ORDER BY id"
        ).fetchall()]
        old_equip_rows = [dict(row) for row in connection.execute(
            "SELECT * FROM character_equip ORDER BY character_id,slot_code,weapon_set"
        ).fetchall()]
        equipped_by_instance = {
            int(row["bag_item_id"]): row
            for row in old_equip_rows if row.get("bag_item_id") is not None
        }

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        try:
            connection.execute("DROP VIEW IF EXISTS character_equip_layout")
            for index_name in (
                "idx_character_bag_character_id", "uq_character_bag_slot",
                "uq_character_equip_item", "idx_character_equip_character",
            ):
                connection.execute(f"DROP INDEX IF EXISTS {index_name}")
            connection.execute("ALTER TABLE character_bag RENAME TO character_bag_v17_13_old")
            connection.execute("ALTER TABLE character_equip RENAME TO character_equip_v17_13_old")
            connection.execute(EQUIP_INSTANCE_CREATE_SQL)
            connection.execute(EQUIP_INSTANCE_ATTR_CREATE_SQL)
            connection.execute(CHARACTER_BAG_CREATE_SQL)
            connection.execute(CHARACTER_EQUIP_CREATE_SQL)

            for row in old_bag_rows:
                instance_id = int(row["id"])
                template_id = int(row["item_id"])
                character_id = int(row["character_id"])
                template = connection.execute(
                    "SELECT durability FROM equip WHERE id=?", (template_id,)
                ).fetchone()
                default_durability = int(template["durability"] or 0) if template else 0
                current = row.get("current_durability")
                current_durability = default_durability if current is None else int(current)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO equip_instance(
                        id,template_id,owner_character_id,current_durability,
                        bind_state,source_type,source_id
                    ) VALUES (?,?,?,?,0,'legacy-v17.13',NULL)
                    """,
                    (instance_id, template_id, character_id, current_durability),
                )

                equipped_row = equipped_by_instance.get(instance_id)
                attr_fields: list[object] = []
                for index in range(1, 6):
                    equip_value = equipped_row.get(f"unique_attr{index}") if equipped_row else ""
                    bag_value = row.get(f"unique_attr{index}", "")
                    attr_fields.append(equip_value if str(equip_value or "").strip() else bag_value)
                attr_pairs: list[tuple[int, int]] = []
                for field in attr_fields:
                    for pair in self._parse_legacy_attribute_pairs(field):
                        if len(attr_pairs) < 5:
                            attr_pairs.append(pair)
                for slot_no, (attr_key, attr_value) in enumerate(attr_pairs, 1):
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO equip_instance_attr(
                            instance_id,slot_no,attr_key,attr_value,attr_source,attr_level
                        ) VALUES (?,?,?,?, 'legacy', 0)
                        """,
                        (instance_id, slot_no, attr_key, attr_value),
                    )

                if row.get("bag_slot") is not None:
                    connection.execute(
                        "INSERT INTO character_bag(character_id,bag_slot,instance_id) VALUES (?,?,?)",
                        (character_id, int(row["bag_slot"]), instance_id),
                    )

            self._ensure_character_equip_rows(connection)
            for row in old_equip_rows:
                old_instance = row.get("bag_item_id")
                if old_instance is None:
                    continue
                connection.execute(
                    """
                    UPDATE character_equip SET instance_id=?
                    WHERE character_id=? AND slot_code=? AND weapon_set=?
                    """,
                    (int(old_instance), int(row["character_id"]), int(row["slot_code"]), int(row["weapon_set"] or 0)),
                )

            connection.execute("DROP TABLE character_equip_v17_13_old")
            connection.execute("DROP TABLE character_bag_v17_13_old")
            self._create_v17_14_inventory_indexes_and_view(connection)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Foreign-key check failed after V17.14 inventory migration: " + repr(violations))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_inventory_schema(self, connection: sqlite3.Connection) -> None:
        """Migrate all historical layouts to V17.14 instance/location tables."""
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "characters" not in tables:
            return
        if self._is_v17_14_inventory_schema(connection):
            instance_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(equip_instance)").fetchall()
            }
            # A short-lived development build stored quantity on equipment
            # instances.  V17.14 defines one row as exactly one unique item.
            if "quantity" in instance_columns:
                connection.execute("ALTER TABLE equip_instance DROP COLUMN quantity")
            self._ensure_character_equip_rows(connection)
            self._create_v17_14_inventory_indexes_and_view(connection)
            return

        # First normalize V17.6-V17.12 layouts into the V17.13 bridge shape.
        connection.execute(LEGACY_CHARACTER_BAG_CREATE_SQL)
        bag_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(character_bag)").fetchall()
        }
        for name, sql in (
            ("current_durability", "INTEGER"),
            ("unique_attr1", "TEXT NOT NULL DEFAULT ''"),
            ("unique_attr2", "TEXT NOT NULL DEFAULT ''"),
            ("unique_attr3", "TEXT NOT NULL DEFAULT ''"),
            ("unique_attr4", "TEXT NOT NULL DEFAULT ''"),
            ("unique_attr5", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in bag_columns:
                connection.execute(f"ALTER TABLE character_bag ADD COLUMN {name} {sql}")

        legacy_items_rows: list[sqlite3.Row] = []
        if "character_items" in tables:
            legacy_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(character_items)").fetchall()
            }
            equipped_expr = "equipped_slot" if "equipped_slot" in legacy_columns else "NULL"
            legacy_items_rows = connection.execute(
                f"SELECT id,character_id,item_id,quantity,bag_slot,{equipped_expr} AS equipped_slot FROM character_items ORDER BY id"
            ).fetchall()
            for row in legacy_items_rows:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO character_bag(id,character_id,item_id,quantity,bag_slot)
                    VALUES (?,?,?,?,?)
                    """,
                    (int(row["id"]), int(row["character_id"]), int(row["item_id"]),
                     int(row["quantity"] or 1), None if row["equipped_slot"] is not None else row["bag_slot"]),
                )

        connection.execute(
            """
            INSERT OR IGNORE INTO equip(id,resource_key,name,equip_type)
            SELECT DISTINCT item_id,printf('%06x',item_id),printf('未定义装备 0x%06X',item_id),0
            FROM character_bag
            """
        )

        old_wide_rows: list[dict[str, object]] = []
        current_tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "character_equip" in current_tables:
            equip_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(character_equip)").fetchall()
            }
            if "slot_code" not in equip_columns:
                old_wide_rows = [dict(row) for row in connection.execute("SELECT * FROM character_equip")]
                connection.execute("ALTER TABLE character_equip RENAME TO character_equip_v17_12_old")
                connection.execute(LEGACY_CHARACTER_EQUIP_CREATE_SQL)
            else:
                for name in ("unique_attr1", "unique_attr2", "unique_attr3", "unique_attr4", "unique_attr5"):
                    if name not in equip_columns:
                        connection.execute(f"ALTER TABLE character_equip ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        else:
            connection.execute(LEGACY_CHARACTER_EQUIP_CREATE_SQL)

        self._ensure_character_equip_rows(connection)
        if old_wide_rows:
            old_column_to_slot = {
                "weapon": (210,1), "shield": (211,1), "armour": (201,0),
                "pants": (203,0), "shoes": (204,0), "gloves": (205,0),
                "helmet": (200,0), "belt": (202,0), "right_ring": (206,0),
                "left_ring": (207,0), "earring": (209,0), "necklace": (208,0),
                "badge1": (214,0), "badge2": (215,0), "badge3": (216,0),
            }
            for row in old_wide_rows:
                for column, (slot_code, weapon_set) in old_column_to_slot.items():
                    item = row.get(column)
                    if item is not None:
                        connection.execute(
                            "UPDATE character_equip SET bag_item_id=? WHERE character_id=? AND slot_code=? AND weapon_set=?",
                            (int(item), int(row["character_id"]), slot_code, weapon_set),
                        )
            connection.execute("DROP TABLE character_equip_v17_12_old")

        for row in legacy_items_rows:
            if row["equipped_slot"] is None:
                continue
            slot_code = int(row["equipped_slot"])
            if slot_code not in EQUIP_SLOT_NAME_BY_CODE:
                continue
            weapon_set = 1 if slot_code in DUAL_WEAPON_SLOT_CODES else 0
            connection.execute(
                "UPDATE character_equip SET bag_item_id=? WHERE character_id=? AND slot_code=? AND weapon_set=?",
                (int(row["id"]), int(row["character_id"]), slot_code, weapon_set),
            )
        if "character_items" in tables:
            connection.execute("DROP TABLE character_items")
        connection.execute(
            """
            UPDATE character_bag SET current_durability=COALESCE(
                current_durability,(SELECT durability FROM equip WHERE equip.id=character_bag.item_id),0
            )
            """
        )
        self._migrate_v17_13_inventory_to_instances(connection)

    def _migrate_equipment_slot_layout_v17_27(
        self, connection: sqlite3.Connection,
    ) -> None:
        """Remap the previously guessed accessory/hand slots to client codes."""
        migration_key = "v17_27_confirmed_equipment_slot_codes"
        if self._has_migration(connection, migration_key):
            return
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "character_equip" not in tables:
            return
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(character_equip)").fetchall()
        }
        if "slot_code" not in columns or "instance_id" not in columns:
            return

        rows = [dict(row) for row in connection.execute(
            "SELECT character_id,slot_code,weapon_set,slot_name,instance_id FROM character_equip"
        ).fetchall()]
        old_layout = any(
            (int(row["slot_code"]) == 213 and str(row["slot_name"]) == "weapon")
            or (int(row["slot_code"]) == 210 and str(row["slot_name"]) == "earring")
            for row in rows
        )
        old_to_new = {
            200: 200, 201: 201, 202: 202, 203: 203, 204: 204, 205: 205,
            206: 214, 207: 215, 208: 216,
            209: 208, 210: 209, 211: 206, 212: 207,
            213: 210, 214: 211,
        }
        equipped: list[tuple[int, int, int, int]] = []
        for row in rows:
            if row["instance_id"] is None:
                continue
            old_slot = int(row["slot_code"])
            new_slot = old_to_new.get(old_slot, old_slot) if old_layout else old_slot
            weapon_set = int(row["weapon_set"] or 0)
            if new_slot in DUAL_WEAPON_SLOT_CODES:
                weapon_set = 1 if weapon_set not in (1, 2) else weapon_set
            else:
                weapon_set = 0
            equipped.append((int(row["character_id"]), new_slot, weapon_set, int(row["instance_id"])))

        character_ids = [int(row["id"]) for row in connection.execute("SELECT id FROM characters")]
        connection.execute("DELETE FROM character_equip")
        for character_id in character_ids:
            self._ensure_character_equip_rows(connection, character_id)
        for character_id, slot_code, weapon_set, instance_id in equipped:
            connection.execute(
                """
                UPDATE character_equip SET instance_id=?
                WHERE character_id=? AND slot_code=? AND weapon_set=?
                """,
                (instance_id, character_id, slot_code, weapon_set),
            )
        self._create_v17_14_inventory_indexes_and_view(connection)
        self._mark_migration(connection, migration_key)

    def first_account(self) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT *
                FROM accounts
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()

    def first_runtime_character(self) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT
                    c.*,
                    a.username AS account_username,
                    m.terrain_name,
                    m.scene_file,
                    m.display_name AS map_display_name,
                    m.bgm_index AS map_bgm_index,
                    m.bgm_file AS map_bgm_file,
                    COALESCE(ps.normal_attack_interval_ms, 2000)
                        AS normal_attack_interval_ms
                FROM characters AS c
                JOIN accounts AS a ON a.id = c.account_id
                JOIN maps AS m ON m.id = c.map_id
                LEFT JOIN profession_level1_stats AS ps
                       ON ps.profession = c.profession AND ps.enabled = 1
                ORDER BY a.id, c.id
                LIMIT 1
                """
            ).fetchone()

    def first_runtime_character_for_account(
        self,
        account_username: str,
    ) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT
                    c.*,
                    a.username AS account_username,
                    m.terrain_name,
                    m.scene_file,
                    m.display_name AS map_display_name,
                    m.bgm_index AS map_bgm_index,
                    m.bgm_file AS map_bgm_file,
                    COALESCE(ps.normal_attack_interval_ms, 2000)
                        AS normal_attack_interval_ms
                FROM characters AS c
                JOIN accounts AS a ON a.id = c.account_id
                JOIN maps AS m ON m.id = c.map_id
                LEFT JOIN profession_level1_stats AS ps
                       ON ps.profession = c.profession AND ps.enabled = 1
                WHERE a.username = ?
                ORDER BY c.id
                LIMIT 1
                """,
                (account_username,),
            ).fetchone()

    def get_account_by_username(self, username: str) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT *
                FROM accounts
                WHERE username = ?
                """,
                (username,),
            ).fetchone()

    def update_account_last_login(self, account_id: int) -> None:
        with self.session() as connection:
            connection.execute(
                """
                UPDATE accounts
                SET last_login_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (account_id,),
            )

    def update_character_last_login(self, character_id: int) -> None:
        with self.session() as connection:
            cursor = connection.execute(
                """
                UPDATE characters
                SET last_login_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(character_id),),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Character last_login_at update failed: "
                    f"character_id={character_id}, updated_rows={cursor.rowcount}"
                )

    def update_character_last_logout_area(
        self,
        character_id: int,
        logout_area: str | int,
    ) -> None:
        area_text = str(logout_area)
        with self.session() as connection:
            cursor = connection.execute(
                """
                UPDATE characters
                SET last_logout_area = ?
                WHERE id = ?
                """,
                (area_text, int(character_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Character last_logout_area update failed: "
                    f"character_id={character_id}, updated_rows={cursor.rowcount}"
                )

    def _ensure_schema_migrations_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_key TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    def _has_migration(self, connection: sqlite3.Connection, migration_key: str) -> bool:
        self._ensure_schema_migrations_table(connection)
        row = connection.execute(
            """
            SELECT 1
            FROM schema_migrations
            WHERE migration_key = ?
            """,
            (migration_key,),
        ).fetchone()
        return row is not None

    def _mark_migration(self, connection: sqlite3.Connection, migration_key: str) -> None:
        self._ensure_schema_migrations_table(connection)
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (migration_key)
            VALUES (?)
            """,
            (migration_key,),
        )

    def _mark_default_seed_owned_if_existing(
        self,
        connection: sqlite3.Connection,
        migration_key: str,
        row_count: int,
    ) -> None:
        """Mark existing databases as administrator-owned, even if a table is empty.

        A brand-new database created through initialize_schema() has no tables at
        the first connection and remains eligible for explicit default seeding.
        An upgraded/administrator database already had a schema, so an empty
        content table may be an intentional deletion and must not be repopulated.
        """
        if int(row_count) > 0 or self._database_had_tables_at_first_connect:
            self._mark_migration(connection, migration_key)

    def _migrate_teleports_schema(self, connection: sqlite3.Connection) -> None:
        """Create/migrate teleport structures without restoring deleted routes."""
        maps_exists = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'maps'
            """
        ).fetchone()
        if maps_exists is None:
            return

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS map_teleports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teleport_key TEXT,
                source_map_id INTEGER NOT NULL,
                trigger_x INTEGER NOT NULL,
                trigger_y INTEGER NOT NULL,
                target_map_id INTEGER NOT NULL,
                target_x INTEGER NOT NULL,
                target_y INTEGER NOT NULL,
                target_direction INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(source_map_id, trigger_x, trigger_y),
                FOREIGN KEY(source_map_id) REFERENCES maps(id),
                FOREIGN KEY(target_map_id) REFERENCES maps(id)
            )
            """
        )

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(map_teleports)").fetchall()
        }
        add_columns = (
            ("teleport_key", "TEXT"),
            ("target_direction", "INTEGER NOT NULL DEFAULT 0"),
            ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("note", "TEXT NOT NULL DEFAULT ''"),
            ("created_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"),
        )
        for column_name, column_sql in add_columns:
            if column_name not in columns:
                connection.execute(f"ALTER TABLE map_teleports ADD COLUMN {column_name} {column_sql}")

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_map_teleports_lookup
            ON map_teleports(source_map_id, trigger_x, trigger_y, enabled)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_map_teleports_target_map
            ON map_teleports(target_map_id)
            """
        )

        # Legacy upgrade only: assign stable keys to already-existing default
        # routes.  Missing routes are not recreated during a runtime session.
        legacy_migration_key = "map_teleports_default_seed_v2"
        if not self._has_migration(connection, legacy_migration_key):
            for (
                teleport_key,
                source_map_id,
                _trigger_x,
                _trigger_y,
                target_map_id,
                _target_x,
                _target_y,
                _target_direction,
                note,
            ) in TELEPORT_DEFAULT_ROWS:
                keyed_row = connection.execute(
                    """
                    SELECT id FROM map_teleports
                    WHERE teleport_key = ?
                    ORDER BY id LIMIT 1
                    """,
                    (str(teleport_key),),
                ).fetchone()
                if keyed_row is not None:
                    continue
                existing_row = connection.execute(
                    """
                    SELECT id FROM map_teleports
                    WHERE (teleport_key IS NULL OR teleport_key = '')
                      AND source_map_id = ?
                      AND target_map_id = ?
                      AND note = ?
                    ORDER BY id LIMIT 1
                    """,
                    (int(source_map_id), int(target_map_id), str(note)),
                ).fetchone()
                if existing_row is not None:
                    connection.execute(
                        "UPDATE map_teleports SET teleport_key = ? WHERE id = ?",
                        (str(teleport_key), int(existing_row["id"])),
                    )
            self._mark_migration(connection, legacy_migration_key)

        self._mark_default_seed_owned_if_existing(
            connection,
            "default_teleport_data_seed_v17_29",
            int(connection.execute("SELECT COUNT(*) FROM map_teleports").fetchone()[0]),
        )

        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_map_teleports_key
            ON map_teleports(teleport_key)
            WHERE teleport_key IS NOT NULL AND teleport_key != ''
            """
        )

    def seed_default_teleport_data(self, connection: sqlite3.Connection) -> bool:
        """Insert built-in teleport routes once for a new empty database."""
        migration_key = "default_teleport_data_seed_v17_29"
        if self._has_migration(connection, migration_key):
            return False
        if int(connection.execute("SELECT COUNT(*) FROM map_teleports").fetchone()[0]) != 0:
            self._mark_migration(connection, migration_key)
            return False
        connection.executemany(
            """
            INSERT INTO map_teleports (
                teleport_key, source_map_id, trigger_x, trigger_y,
                target_map_id, target_x, target_y, target_direction,
                enabled, note
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            [
                (
                    str(teleport_key), int(source_map_id), int(trigger_x),
                    int(trigger_y), int(target_map_id), int(target_x),
                    int(target_y), int(target_direction), str(note),
                )
                for (
                    teleport_key, source_map_id, trigger_x, trigger_y,
                    target_map_id, target_x, target_y, target_direction, note,
                ) in TELEPORT_DEFAULT_ROWS
            ],
        )
        self._mark_migration(connection, migration_key)
        return True

    def _swap_npc_scale_yz_once(self, connection: sqlite3.Connection, table_name: str) -> None:
        """
        Older seeded NPC tables used the scale_y/scale_z names in reverse.
        Swap their stored values once per table so future database edits can use
        the corrected names directly.
        """
        migration_key = f"npc_scale_yz_swap:{table_name}"
        if self._has_migration(connection, migration_key):
            return

        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if "scale_y" not in columns or "scale_z" not in columns:
            return

        temp_name = f"temp_scale_swap_{table_name}"
        connection.execute(
            f"CREATE TEMP TABLE {temp_name} AS SELECT id, scale_y AS old_y, scale_z AS old_z FROM {table_name}"
        )
        connection.execute(
            f"""
            UPDATE {table_name}
            SET
                scale_y = (SELECT old_z FROM {temp_name} WHERE {temp_name}.id = {table_name}.id),
                scale_z = (SELECT old_y FROM {temp_name} WHERE {temp_name}.id = {table_name}.id)
            WHERE id IN (SELECT id FROM {temp_name})
            """
        )
        connection.execute(f"DROP TABLE {temp_name}")
        self._mark_migration(connection, migration_key)


    def _migrate_mob_schema(self, connection: sqlite3.Connection) -> None:
        """Create monster structures without inserting or restoring game data."""
        maps_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maps'"
        ).fetchone()
        if maps_exists is None:
            return
        connection.executescript(MOB_SCHEMA_SQL)
        # Keep the optional source monster ID separate from the confirmed
        # client mesh resource ID.  The source ID is currently unconfirmed and
        # remains blank; older databases are migrated without inventing values.
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(mob_templates)").fetchall()
        }
        if "monster_data_id" not in columns:
            connection.execute(
                "ALTER TABLE mob_templates "
                "ADD COLUMN monster_data_id TEXT NOT NULL DEFAULT ''"
            )
        if "aggro_mode" not in columns:
            connection.execute(
                "ALTER TABLE mob_templates "
                "ADD COLUMN aggro_mode TEXT NOT NULL DEFAULT 'RETALIATE'"
            )
        if "aggro_range" not in columns:
            connection.execute(
                "ALTER TABLE mob_templates "
                "ADD COLUMN aggro_range REAL NOT NULL DEFAULT 0.0"
            )

        # V18.4: database-authoritative idle wandering.  These are template
        # properties rather than map-wide settings, so one map may freely mix
        # stationary and roaming monsters.
        wander_columns = {
            "idle_wander_enabled": (
                "INTEGER NOT NULL DEFAULT 1"
            ),
            "idle_wander_radius": (
                "REAL NOT NULL DEFAULT 3.0"
            ),
            "idle_wander_min_pause_ms": (
                "INTEGER NOT NULL DEFAULT 3000"
            ),
            "idle_wander_max_pause_ms": (
                "INTEGER NOT NULL DEFAULT 7000"
            ),
            "idle_wander_move_speed": (
                "INTEGER NOT NULL DEFAULT 180"
            ),
        }
        for column_name, definition in wander_columns.items():
            if column_name not in columns:
                connection.execute(
                    f"ALTER TABLE mob_templates ADD COLUMN {column_name} {definition}"
                )
                columns.add(column_name)

        # V18.3 one-time balance migration.  It only replaces the old
        # prototype defaults, so administrator-edited values are preserved.
        timing_migration_key = "v18_3_normal_mob_combat_timing_defaults"
        if not self._has_migration(connection, timing_migration_key):
            connection.execute(
                "UPDATE mob_templates SET default_leash_range = 20.0 "
                "WHERE default_leash_range = 10.0"
            )
            connection.execute(
                "UPDATE mob_templates SET corpse_ms = 5000 "
                "WHERE corpse_ms = 3000"
            )
            connection.execute(
                "UPDATE mob_templates SET default_respawn_ms = 30000 "
                "WHERE default_respawn_ms = 10000"
            )
            self._mark_migration(connection, timing_migration_key)

    def list_mob_templates(
        self,
        *,
        include_disabled: bool = False,
    ) -> list[dict[str, object]]:
        with self.session() as connection:
            where_sql = "" if include_disabled else "WHERE enabled != 0"
            rows = connection.execute(
                f"SELECT * FROM mob_templates {where_sql} ORDER BY template_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def list_mob_spawn_regions(
        self,
        map_id: int | None = None,
        *,
        include_disabled: bool = False,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if map_id is not None:
            clauses.append("map_id = ?")
            parameters.append(int(map_id))
        if not include_disabled:
            clauses.append("enabled != 0")
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.session() as connection:
            rows = connection.execute(
                f"SELECT * FROM mob_spawn_regions{where_sql} ORDER BY map_id, region_id",
                tuple(parameters),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_mob_region_populations(
        self,
        *,
        map_id: int | None = None,
        region_id: int | None = None,
        include_disabled: bool = False,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if map_id is not None:
            clauses.append("r.map_id = ?")
            parameters.append(int(map_id))
        if region_id is not None:
            clauses.append("p.region_id = ?")
            parameters.append(int(region_id))
        if not include_disabled:
            clauses.extend(("p.enabled != 0", "r.enabled != 0", "t.enabled != 0"))
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.session() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*, r.map_id, r.region_key, r.region_name,
                       t.template_key, t.display_name,
                       t.monster_data_id, t.client_model_id
                FROM mob_region_populations AS p
                JOIN mob_spawn_regions AS r ON r.region_id = p.region_id
                JOIN mob_templates AS t ON t.template_id = p.template_id
                {where_sql}
                ORDER BY r.map_id, r.region_id, p.sort_order, p.population_id
                """,
                tuple(parameters),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_map_collision_grid(self, map_id: int) -> dict[str, object] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM map_collision_grids WHERE map_id = ?",
                (int(map_id),),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            width = int(item["width"])
            height = int(item["height"])
            item["raw_grid"] = decode_u8_grid(
                item["grid_data"],
                str(item["encoding"]),
                expected_size=width * height,
            )
            return item

    def is_map_cell_walkable(self, map_id: int, x: int, y: int) -> bool:
        grid = self.get_map_collision_grid(int(map_id))
        if grid is None:
            return False
        width = int(grid["width"])
        height = int(grid["height"])
        grid_x = int(x)
        grid_y = int(y)
        if not (0 <= grid_x < width and 0 <= grid_y < height):
            return False
        raw = grid["raw_grid"]
        return int(raw[grid_y * width + grid_x]) == int(grid["walkable_value"])

    def list_map_mob_spawns(
        self,
        map_id: int,
        *,
        include_disabled: bool = False,
    ) -> list[dict[str, object]]:
        enabled_sql = "" if include_disabled else (
            "AND s.enabled != 0 AND t.enabled != 0 "
            "AND (r.region_id IS NULL OR r.enabled != 0) "
            "AND (p.population_id IS NULL OR p.enabled != 0)"
        )
        with self.session() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    s.*,
                    t.template_key,
                    t.display_name,
                    t.monster_data_id,
                    t.client_model_id,
                    t.level,
                    t.max_hp, t.max_mp, t.max_sp,
                    t.attack_min, t.attack_max,
                    t.defense_power,
                    t.magic_attack_min, t.magic_attack_max,
                    t.magic_defense_power,
                    t.earth_resistance, t.water_resistance,
                    t.fire_resistance, t.wind_resistance,
                    t.light_resistance, t.dark_resistance,
                    t.experience_reward,
                    t.gold_min, t.gold_max,
                    t.special_text,
                    t.first_attack_delay_ms,
                    t.attack_interval_ms,
                    t.attack_range,
                    t.move_speed,
                    t.idle_wander_enabled,
                    t.idle_wander_radius,
                    t.idle_wander_min_pause_ms,
                    t.idle_wander_max_pause_ms,
                    t.idle_wander_move_speed,
                    COALESCE(s.leash_range_override,
                             p.leash_range_override,
                             t.default_leash_range) AS effective_leash_range,
                    t.corpse_ms,
                    COALESCE(s.respawn_ms_override,
                             p.respawn_ms_override,
                             t.default_respawn_ms) AS effective_respawn_ms,
                    t.action_type,
                    t.attack_effect_field10,
                    t.aggro_mode AS aggro_mode,
                    t.aggro_range AS aggro_range,
                    t.scale_x, t.scale_y, t.scale_z,
                    t.ghost_mode,
                    t.being_field0c AS being_field0c,
                    t.being_field10,
                    t.being_field18, t.being_field1c,
                    t.being_field20,
                    t.being_field2c AS being_field2c,
                    t.array1_u32_array, t.array2_u32_array,
                    r.region_key, r.region_name,
                    p.population_key
                FROM map_mob_spawns AS s
                JOIN mob_templates AS t ON t.template_id = s.template_id
                LEFT JOIN mob_spawn_regions AS r ON r.region_id = s.region_id
                LEFT JOIN mob_region_populations AS p
                       ON p.population_id = s.population_id
                WHERE s.map_id = ?
                {enabled_sql}
                ORDER BY s.spawn_id
                """,
                (int(map_id),),
            ).fetchall()
            result: list[dict[str, object]] = []
            for row in rows:
                item = dict(row)
                spawn_id = int(item["spawn_id"])
                network_id = int(item["network_id"] or (2_000_000 + spawn_id))
                item["network_id"] = network_id
                item["internal_name"] = (
                    f"NPC{int(item['map_id'])}_{1000 + spawn_id:03d}#{network_id}"
                )
                result.append(item)
            return result

    def _migrate_map_npcs_schema(self, connection: sqlite3.Connection) -> None:
        """Create NPC table structures without restoring or rewriting NPC rows."""
        total_rows = 0
        for map_id in sorted(NPC_DEFAULT_ROWS_BY_MAP):
            self._create_map_npcs_table(connection, int(map_id))
            table_name = f"map_npcs_{int(map_id)}"
            total_rows += int(connection.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0])
        self._mark_default_seed_owned_if_existing(
            connection, "default_map_npc_data_seed_v17_29", total_rows
        )

    def seed_default_map_npc_data(self, connection: sqlite3.Connection) -> bool:
        """Insert the built-in NPC roster once for a new empty database."""
        migration_key = "default_map_npc_data_seed_v17_29"
        if self._has_migration(connection, migration_key):
            return False
        total_rows = 0
        for map_id in sorted(NPC_DEFAULT_ROWS_BY_MAP):
            self._create_map_npcs_table(connection, int(map_id))
            table_name = f"map_npcs_{int(map_id)}"
            total_rows += int(connection.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0])
        if total_rows != 0:
            self._mark_migration(connection, migration_key)
            return False
        for map_id in sorted(NPC_DEFAULT_ROWS_BY_MAP):
            self._seed_map_npcs_for_map(connection, int(map_id))
        self._mark_migration(connection, migration_key)
        return True

    def _create_map_npcs_table(self, connection: sqlite3.Connection, map_id: int) -> None:
        table_name = f"map_npcs_{int(map_id)}"
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                npc_model_id TEXT NOT NULL DEFAULT '01001000',
                position_x INTEGER NOT NULL,
                position_y INTEGER NOT NULL,
                direction INTEGER NOT NULL DEFAULT 0,
                scale_x INTEGER NOT NULL DEFAULT 100,
                scale_y INTEGER NOT NULL DEFAULT 100,
                scale_z INTEGER NOT NULL DEFAULT 100,
                ghost_mode INTEGER NOT NULL DEFAULT 0,
                field0c INTEGER NOT NULL DEFAULT 0,
                field10 INTEGER NOT NULL DEFAULT 0,
                field18 INTEGER NOT NULL DEFAULT 0,
                field1c INTEGER NOT NULL DEFAULT 0,
                field20 INTEGER NOT NULL DEFAULT 100,
                field2c INTEGER NOT NULL DEFAULT 1,
                array1_u32_array TEXT NOT NULL DEFAULT '',
                array2_u32_array TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        add_columns = (
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("npc_model_id", "TEXT NOT NULL DEFAULT '01001000'"),
            ("direction", "INTEGER NOT NULL DEFAULT 0"),
            ("scale_x", "INTEGER NOT NULL DEFAULT 100"),
            ("scale_y", "INTEGER NOT NULL DEFAULT 100"),
            ("scale_z", "INTEGER NOT NULL DEFAULT 100"),
            ("ghost_mode", "INTEGER NOT NULL DEFAULT 0"),
            ("field0c", "INTEGER NOT NULL DEFAULT 0"),
            ("field10", "INTEGER NOT NULL DEFAULT 0"),
            ("field18", "INTEGER NOT NULL DEFAULT 0"),
            ("field1c", "INTEGER NOT NULL DEFAULT 0"),
            ("field20", "INTEGER NOT NULL DEFAULT 100"),
            ("field2c", "INTEGER NOT NULL DEFAULT 1"),
            ("array1_u32_array", "TEXT NOT NULL DEFAULT ''"),
            ("array2_u32_array", "TEXT NOT NULL DEFAULT ''"),
            ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("note", "TEXT NOT NULL DEFAULT ''"),
        )
        for column_name, column_sql in add_columns:
            if column_name not in columns:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_name}_enabled ON {table_name}(enabled, id)"
        )
        self._swap_npc_scale_yz_once(connection, table_name)

    def _seed_map_npcs_for_map(self, connection: sqlite3.Connection, map_id: int) -> None:
        table_name = f"map_npcs_{int(map_id)}"
        for display_name, title, position_x, position_y in NPC_DEFAULT_ROWS_BY_MAP.get(int(map_id), ()):
            connection.execute(
                f"""
                INSERT INTO {table_name} (
                    display_name,
                    title,
                    npc_model_id,
                    position_x,
                    position_y,
                    direction,
                    scale_x,
                    scale_y,
                    scale_z,
                    ghost_mode,
                    field0c,
                    field10,
                    field18,
                    field1c,
                    field20,
                    field2c,
                    array1_u32_array,
                    array2_u32_array,
                    enabled,
                    note
                )
                VALUES (?, ?, '01001000', ?, ?, 0, 100, 100, 100, 0, 0, 0, 0, 0, 100, 1, '', '', 1, '')
                """,
                (display_name, title, int(position_x), int(position_y)),
            )

    def list_map_npcs_for_map(self, map_id: int) -> list[dict[str, object]]:
        """Return NPC rows with shop title/click mode resolved without DB writes.

        map_npcs_<id>.enabled remains authoritative for whether an NPC spawns.
        For an enabled shop binding, npc_shops.shop_title and
        npc_shops.interaction_field0c are the effective shop-facing values.
        """
        table_name = f"map_npcs_{int(map_id)}"
        with self.session() as connection:
            table_exists = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table_name,),
            ).fetchone()
            if table_exists is None:
                return []
            rows = connection.execute(
                f"""
                SELECT n.*,
                       s.shop_title AS effective_shop_title,
                       s.interaction_field0c AS effective_interaction_field0c
                FROM {table_name} AS n
                LEFT JOIN npc_shops AS s
                  ON s.map_id = ?
                 AND s.npc_display_name = n.display_name
                 AND s.enabled <> 0
                WHERE n.enabled != 0
                ORDER BY n.id
                """,
                (int(map_id),),
            ).fetchall()
            result: list[dict[str, object]] = []
            for row in rows:
                item = dict(row)
                if item.get("effective_shop_title") is not None:
                    item["title"] = str(item["effective_shop_title"])
                if item.get("effective_interaction_field0c") is not None:
                    item["field0c"] = int(item["effective_interaction_field0c"])
                item.pop("effective_shop_title", None)
                item.pop("effective_interaction_field0c", None)
                result.append(item)
            return result

    def first_character_for_account(
        self,
        account_id: int,
    ) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT
                    c.*,
                    m.terrain_name,
                    m.scene_file,
                    m.display_name AS map_display_name,
                    m.bgm_index AS map_bgm_index,
                    m.bgm_file AS map_bgm_file
                FROM characters AS c
                JOIN maps AS m ON m.id = c.map_id
                WHERE c.account_id = ?
                ORDER BY c.id
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()

    def get_character_by_name(self, name: str) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT
                    c.*,
                    m.terrain_name,
                    m.scene_file,
                    m.display_name AS map_display_name,
                    m.bgm_index AS map_bgm_index,
                    m.bgm_file AS map_bgm_file
                FROM characters AS c
                JOIN maps AS m ON m.id = c.map_id
                WHERE c.name = ?
                """,
                (name,),
            ).fetchone()

    def list_characters_for_account(
        self,
        account_id: int,
    ) -> list[sqlite3.Row]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT
                    c.*,
                    m.terrain_name,
                    m.scene_file,
                    m.display_name AS map_display_name,
                    m.bgm_index AS map_bgm_index,
                    m.bgm_file AS map_bgm_file
                FROM characters AS c
                JOIN maps AS m ON m.id = c.map_id
                WHERE c.account_id = ?
                ORDER BY c.id
                """,
                (account_id,),
            ).fetchall()

        return list(rows)

    def create_character_for_account(
        self,
        account_username: str,
        name: str,
        gender: int,
        profession: int,
        map_id: int,
        position_x: int,
        position_y: int,
        direction: int,
        body: int,
        hair: int,
        head: int,
        hand_r: int,
        hand_l: int,
        pants: int,
        foot_r: int,
        foot_l: int,
        bag_capacity: int = 40,
        ancillary_profession: int = 0,
    ) -> tuple[bool, str, sqlite3.Row | None]:
        character_name = name.strip()
        if not character_name:
            return False, "empty-name", None

        if "#" in character_name:
            return False, "invalid-name", None

        # Do not persist binary/control-byte payloads as role names.
        # A malformed 0x9003 parse can otherwise poison the account role list and
        # make the next login black-screen before the role UI is built.
        if not is_safe_character_name(character_name):
            return False, "invalid-name", None

        try:
            with self.session() as connection:
                account = connection.execute(
                    """
                    SELECT id
                    FROM accounts
                    WHERE username = ?
                    """,
                    (account_username,),
                ).fetchone()

                if account is None:
                    return False, "account-not-found", None

                base_stats = self._load_profession_level1_stats_row(
                    connection, int(profession)
                )
                if base_stats is None:
                    print(
                        "[database] Character creation rejected: no enabled "
                        "profession_level1_stats row for "
                        f"profession={int(profession)}"
                    )
                    return False, "profession-base-stats-unavailable", None

                existing = connection.execute(
                    """
                    SELECT id, account_id, name
                    FROM characters
                    WHERE name = ?
                    """,
                    (character_name,),
                ).fetchone()

                if existing is not None:
                    return False, "duplicate-name", existing

                initial_stats = self._build_level1_character_stats(
                    connection, int(profession), base_stats
                )
                cursor = connection.execute(
                    """
                    INSERT INTO characters (
                        account_id, name, gender, level, profession,
                        ancillary_profession, map_id, position_x, position_y,
                        direction, body, hair, head, hand_r, hand_l, pants,
                        foot_r, foot_l, hp, max_hp, mp, max_mp, sp, max_sp,
                        hp_regen_per_second, mp_regen_per_second,
                        sp_regen_per_second, strength, wisdom, dexterity,
                        constitution, attack_power, defense_power,
                        magic_attack_power, earth_resistance, water_resistance,
                        fire_resistance, wind_resistance, light_resistance,
                        dark_resistance, bag_capacity
                    )
                    VALUES (
                        ?, ?, ?, 1, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        int(account["id"]), character_name, int(gender),
                        int(profession), int(ancillary_profession), int(map_id),
                        int(position_x), int(position_y), int(direction),
                        int(body), int(hair), int(head), int(hand_r),
                        int(hand_l), int(pants), int(foot_r), int(foot_l),
                        int(initial_stats["hp"]), int(initial_stats["max_hp"]),
                        int(initial_stats["mp"]), int(initial_stats["max_mp"]),
                        int(initial_stats["sp"]), int(initial_stats["max_sp"]),
                        int(initial_stats["hp_regen_per_second"]),
                        int(initial_stats["mp_regen_per_second"]),
                        int(initial_stats["sp_regen_per_second"]),
                        int(initial_stats["strength"]),
                        int(initial_stats["wisdom"]),
                        int(initial_stats["dexterity"]),
                        int(initial_stats["constitution"]),
                        int(initial_stats["attack_power"]),
                        int(initial_stats["defense_power"]),
                        int(initial_stats["magic_attack_power"]),
                        int(initial_stats["earth_resistance"]),
                        int(initial_stats["water_resistance"]),
                        int(initial_stats["fire_resistance"]),
                        int(initial_stats["wind_resistance"]),
                        int(initial_stats["light_resistance"]),
                        int(initial_stats["dark_resistance"]),
                        int(bag_capacity),
                    ),
                )

                character_id = int(cursor.lastrowid)
                character = connection.execute(
                    """
                    SELECT
                        c.*,
                        m.terrain_name,
                        m.scene_file
                    FROM characters AS c
                    JOIN maps AS m ON m.id = c.map_id
                    WHERE c.id = ?
                    """,
                    (character_id,),
                ).fetchone()

                if character is None:
                    raise RuntimeError(
                        "Created character could not be reloaded: "
                        f"id={character_id}"
                    )

                self._insert_default_skills_for_character(
                    connection,
                    character_id,
                    int(profession),
                    1,
                )
                self._insert_character_base_stats(
                    connection,
                    character_id,
                    self._profession_base_stat_values(base_stats),
                )

                return True, "created", character

        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "characters.name" in message or "UNIQUE" in message:
                return False, "duplicate-name", None
            return False, f"integrity-error:{message}", None


    def delete_character_for_account(
        self,
        account_username: str,
        character_name: str,
    ) -> tuple[bool, str]:
        """Delete one character owned by one account.

        Foreign keys in schema.sql use ON DELETE CASCADE for character_skills
        and character_bag, so deleting the character row also removes its
        dependent per-character rows.
        """
        role_name = character_name.strip()
        if not role_name:
            return False, "empty-name"

        if not is_safe_character_name(role_name):
            return False, "invalid-name"

        with self.session() as connection:
            account = connection.execute(
                """
                SELECT id
                FROM accounts
                WHERE username = ?
                """,
                (account_username,),
            ).fetchone()

            if account is None:
                return False, "account-not-found"

            existing = connection.execute(
                """
                SELECT id, account_id, name
                FROM characters
                WHERE account_id = ?
                  AND name = ?
                """,
                (int(account["id"]), role_name),
            ).fetchone()

            if existing is None:
                return False, "character-not-found"

            cursor = connection.execute(
                """
                DELETE FROM characters
                WHERE id = ?
                  AND account_id = ?
                """,
                (int(existing["id"]), int(account["id"])),
            )

            if cursor.rowcount != 1:
                return False, f"delete-rowcount-{cursor.rowcount}"

        return True, "deleted"

    def update_character_position(
        self,
        character_id: int,
        map_id: int,
        position_x: int,
        position_y: int,
        direction: int,
    ) -> None:
        if not 0 <= position_x <= 0xFFFF:
            raise ValueError(
                f"position_x must be between 0 and 65535, got {position_x}"
            )
        if not 0 <= position_y <= 0xFFFF:
            raise ValueError(
                f"position_y must be between 0 and 65535, got {position_y}"
            )
        if not 0 <= direction <= 0xFFFF:
            raise ValueError(
                f"direction must be between 0 and 65535, got {direction}"
            )

        with self.session() as connection:
            cursor = connection.execute(
                """
                UPDATE characters
                SET
                    map_id = ?,
                    position_x = ?,
                    position_y = ?,
                    direction = ?
                WHERE id = ?
                """,
                (
                    map_id,
                    position_x,
                    position_y,
                    direction,
                    character_id,
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Character position update failed: "
                    f"character_id={character_id}, "
                    f"updated_rows={cursor.rowcount}"
                )

    def get_character_position(
        self,
        character_id: int,
    ) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    name,
                    map_id,
                    position_x,
                    position_y,
                    direction
                FROM characters
                WHERE id = ?
                """,
                (character_id,),
            ).fetchone()


    def find_enabled_teleport(
        self,
        source_map_id: int,
        trigger_x: int,
        trigger_y: int,
    ) -> sqlite3.Row | None:
        # This method is called for every 0x8005 movement packet, so keep it a
        # lightweight read and avoid running the full migration path each time.
        connection = self.connect()
        try:
            return connection.execute(
                """
                SELECT *
                FROM map_teleports
                WHERE enabled != 0
                  AND source_map_id = ?
                  AND trigger_x = ?
                  AND trigger_y = ?
                ORDER BY id
                LIMIT 1
                """,
                (int(source_map_id), int(trigger_x), int(trigger_y)),
            ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table: map_teleports" in str(error):
                return None
            raise
        finally:
            connection.close()

    def list_enabled_teleports(self) -> list[sqlite3.Row]:
        with self.session() as connection:
            return list(connection.execute(
                """
                SELECT *
                FROM map_teleports
                WHERE enabled != 0
                ORDER BY source_map_id, trigger_x, trigger_y, id
                """
            ).fetchall())

    def _insert_default_skills_for_character(
        self,
        connection: sqlite3.Connection,
        character_id: int,
        profession: int,
        character_level: int = 1,
    ) -> None:
        """Insert missing default skills for a level-1 role.

        This intentionally uses INSERT OR IGNORE, so existing per-character skill
        levels are never overwritten by the default seeding path.
        """
        skill_level = max(1, int(character_level))
        for slot_index, skill_id in enumerate(
            default_level1_skill_ids_for_profession(int(profession))
        ):
            connection.execute(
                """
                INSERT OR IGNORE INTO character_skills (
                    character_id,
                    skill_id,
                    field04_unknown,
                    field0c_unknown,
                    field0e_unknown,
                    skill_level,
                    field1c_u32_array,
                    slot_index
                )
                VALUES (?, ?, ?, ?, ?, ?, '', ?)
                """,
                (
                    int(character_id),
                    int(skill_id),
                    DEFAULT_SKILL_FIELD04_UNKNOWN,
                    DEFAULT_SKILL_FIELD0C_UNKNOWN,
                    DEFAULT_SKILL_FIELD0E_UNKNOWN,
                    skill_level,
                    int(slot_index),
                ),
            )

    def ensure_default_skills_for_character(
        self,
        character_id: int,
        profession: int,
        character_level: int = 1,
    ) -> None:
        """Public wrapper used by init_database/server runtime migration."""
        with self.session() as connection:
            self._insert_default_skills_for_character(
                connection,
                int(character_id),
                int(profession),
                int(character_level),
            )

    def list_skills_for_character(
        self,
        character_id: int,
    ) -> list[sqlite3.Row]:
        """Return persisted opcode 0x0022 skill rows for one character."""
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT
                    character_id,
                    skill_id,
                    field04_unknown,
                    field0c_unknown,
                    field0e_unknown,
                    skill_level,
                    field1c_u32_array,
                    slot_index
                FROM character_skills
                WHERE character_id = ?
                ORDER BY
                    CASE WHEN slot_index IS NULL THEN 1 ELSE 0 END,
                    slot_index,
                    skill_id
                """,
                (int(character_id),),
            ).fetchall()

        return list(rows)

    @staticmethod
    def _select_skill_upgrade_rule(
        connection: sqlite3.Connection,
        skill_id: int,
        from_level: int,
    ) -> sqlite3.Row | None:
        """Prefer an exact skill rule, then the skill_id=0 fallback."""
        exact = connection.execute(
            """
            SELECT * FROM skill_upgrade_costs
            WHERE skill_id = ? AND from_level = ?
            LIMIT 1
            """,
            (int(skill_id), int(from_level)),
        ).fetchone()
        if exact is not None:
            return exact if int(exact["enabled"]) != 0 else None
        fallback = connection.execute(
            """
            SELECT * FROM skill_upgrade_costs
            WHERE skill_id = 0 AND from_level = ?
            LIMIT 1
            """,
            (int(from_level),),
        ).fetchone()
        if fallback is None or int(fallback["enabled"]) == 0:
            return None
        return fallback

    def get_skill_upgrade_rule(
        self,
        skill_id: int,
        from_level: int,
    ) -> sqlite3.Row | None:
        with self.session() as connection:
            return self._select_skill_upgrade_rule(
                connection, int(skill_id), int(from_level)
            )

    @staticmethod
    def _load_skill_upgrade_effects(
        connection: sqlite3.Connection,
        profession: int,
        skill_id: int,
    ) -> dict[str, int]:
        """Load generic effects, then let profession rows override by column."""
        rows = connection.execute(
            """
            SELECT profession, character_column, amount, enabled
            FROM skill_upgrade_effects
            WHERE skill_id = ?
              AND profession IN (0, ?)
            ORDER BY profession
            """,
            (int(skill_id), int(profession)),
        ).fetchall()
        effects: dict[str, int] = {}
        for row in rows:
            column = str(row["character_column"])
            if column not in SKILL_EFFECT_CHARACTER_COLUMNS:
                raise ValueError(
                    f"Unsafe skill_upgrade_effects.character_column: {column!r}"
                )
            if int(row["enabled"]) == 0:
                # An exact disabled row intentionally suppresses the generic
                # row for this column; it must not silently fall back.
                effects.pop(column, None)
            else:
                effects[column] = int(row["amount"])
        return effects

    @staticmethod
    def _load_skill_upgrade_milestone_effects(
        connection: sqlite3.Connection,
        profession: int,
        skill_id: int,
    ) -> dict[str, tuple[int, int, int]]:
        """Load milestone rows; exact profession rows override generic rows."""
        rows = connection.execute(
            """
            SELECT profession, character_column, first_target_level,
                   interval_levels, amount, enabled
            FROM skill_upgrade_milestone_effects
            WHERE skill_id = ?
              AND profession IN (0, ?)
            ORDER BY profession
            """,
            (int(skill_id), int(profession)),
        ).fetchall()
        effects: dict[str, tuple[int, int, int]] = {}
        for row in rows:
            column = str(row["character_column"])
            if column not in SKILL_EFFECT_CHARACTER_COLUMNS:
                raise ValueError(
                    "Unsafe skill_upgrade_milestone_effects.character_column: "
                    f"{column!r}"
                )
            if int(row["enabled"]) == 0:
                effects.pop(column, None)
                continue
            first = max(1, int(row["first_target_level"]))
            interval = max(1, int(row["interval_levels"]))
            effects[column] = (first, interval, int(row["amount"]))
        return effects

    @staticmethod
    def _milestone_count_at_level(level: int, first: int, interval: int) -> int:
        level = int(level)
        first = max(1, int(first))
        interval = max(1, int(interval))
        if level < first:
            return 0
        return ((level - first) // interval) + 1

    def _build_level1_character_stats(
        self,
        connection: sqlite3.Connection,
        profession: int,
        base_stats: sqlite3.Row,
    ) -> dict[str, int]:
        """Apply all seven level-1 attribute effects to one profession base."""
        values = {
            "max_hp": int(base_stats["hp"]),
            "max_mp": int(base_stats["mp"]),
            "max_sp": int(base_stats["sp"]),
            "attack_power": int(base_stats["attack_power"]),
            "defense_power": int(base_stats["defense_power"]),
            "magic_attack_power": int(base_stats["magic_attack_power"]),
            "normal_attack_interval_ms": int(base_stats["normal_attack_interval_ms"]),
            "earth_resistance": 0,
            "water_resistance": 0,
            "fire_resistance": 0,
            "wind_resistance": 0,
            "light_resistance": 0,
            "dark_resistance": 0,
            "hp_regen_per_second": 0,
            "mp_regen_per_second": 0,
            "sp_regen_per_second": 0,
            "strength": 1,
            "dexterity": 1,
            "wisdom": 1,
            "constitution": 1,
        }
        for skill_id in ATTRIBUTE_SKILL_IDS:
            for column, amount in self._load_skill_upgrade_effects(
                connection, int(profession), int(skill_id)
            ).items():
                values[column] = int(values.get(column, 0)) + int(amount)
            for column, (first, interval, amount) in (
                self._load_skill_upgrade_milestone_effects(
                    connection, int(profession), int(skill_id)
                ).items()
            ):
                count = self._milestone_count_at_level(1, first, interval)
                values[column] = int(values.get(column, 0)) + count * int(amount)
        values["hp"] = max(0, int(values["max_hp"]))
        values["mp"] = max(0, int(values["max_mp"]))
        values["sp"] = max(0, int(values["max_sp"]))
        return values

    def upgrade_character_skill(
        self,
        character_id: int,
        skill_id: int,
        action: int = 1,
    ) -> dict[str, object]:
        """Validate and atomically apply one client 0x8021 upgrade request."""
        result: dict[str, object] = {
            "accepted": False,
            "action": int(action),
            "character_id": int(character_id),
            "skill_id": int(skill_id),
        }
        if int(action) != 1:
            result["reason"] = "unsupported-action"
            return result

        with self.session() as connection:
            character = connection.execute(
                "SELECT * FROM characters WHERE id = ?",
                (int(character_id),),
            ).fetchone()
            if character is None:
                result["reason"] = "character-not-found"
                return result

            skill = connection.execute(
                """
                SELECT * FROM character_skills
                WHERE character_id = ? AND skill_id = ?
                """,
                (int(character_id), int(skill_id)),
            ).fetchone()
            if skill is None:
                result["reason"] = "skill-not-owned"
                return result

            from_level = int(skill["skill_level"])
            rule = self._select_skill_upgrade_rule(
                connection, int(skill_id), from_level
            )
            if rule is None:
                result.update({
                    "reason": "no-enabled-upgrade-rule",
                    "from_level": from_level,
                })
                return result

            to_level = int(rule["to_level"])
            exp_cost = max(0, int(rule["exp_cost"]))
            gold_cost = max(0, int(rule["gold_cost"]))
            required_character_level = max(0, int(rule["required_character_level"]))
            character_level = int(character["level"])
            experience_before = int(character["experience"])
            gold_before = int(character["player_gold"])

            if to_level <= from_level:
                result["reason"] = "invalid-rule-level-range"
                return result
            if character_level < required_character_level:
                result.update({
                    "reason": "character-level-too-low",
                    "from_level": from_level,
                    "to_level": to_level,
                    "character_level": character_level,
                    "required_character_level": required_character_level,
                })
                return result
            if experience_before < exp_cost:
                result.update({
                    "reason": "not-enough-experience",
                    "from_level": from_level,
                    "to_level": to_level,
                    "experience": experience_before,
                    "exp_cost": exp_cost,
                })
                return result
            if gold_before < gold_cost:
                result.update({
                    "reason": "not-enough-gold",
                    "from_level": from_level,
                    "to_level": to_level,
                    "gold": gold_before,
                    "gold_cost": gold_cost,
                })
                return result

            updates: dict[str, int] = {
                "experience": experience_before - exp_cost,
                "player_gold": gold_before - gold_cost,
            }
            if int(skill_id) == 0x0001:
                updates["level"] = to_level
            else:
                # Derived attributes are recalculated from the skill level by
                # CharacterStatService.  Only explicitly stateful/non-derived
                # legacy effects remain direct database updates.
                level_delta = to_level - from_level
                direct_state_columns = {"hp", "mp", "sp", "reputation"}
                effects = self._load_skill_upgrade_effects(
                    connection, int(character["profession"]), int(skill_id)
                )
                for column, amount in effects.items():
                    if column in direct_state_columns:
                        updates[column] = max(
                            0, int(character[column]) + int(amount) * level_delta
                        )
                milestone_effects = self._load_skill_upgrade_milestone_effects(
                    connection, int(character["profession"]), int(skill_id)
                )
                for column, (first, interval, amount) in milestone_effects.items():
                    if column not in direct_state_columns:
                        continue
                    before_count = self._milestone_count_at_level(
                        from_level, first, interval
                    )
                    after_count = self._milestone_count_at_level(
                        to_level, first, interval
                    )
                    triggered = after_count - before_count
                    if triggered:
                        updates[column] = max(
                            0,
                            int(updates.get(column, character[column]))
                            + int(amount) * triggered,
                        )
            # Never write effective attributes back into characters here.

            assignments = ", ".join(f"{column} = ?" for column in updates)
            connection.execute(
                f"UPDATE characters SET {assignments} WHERE id = ?",
                (*updates.values(), int(character_id)),
            )
            connection.execute(
                """
                UPDATE character_skills
                SET skill_level = ?
                WHERE character_id = ? AND skill_id = ?
                """,
                (to_level, int(character_id), int(skill_id)),
            )

            result.update({
                "accepted": True,
                "reason": "upgraded",
                "from_level": from_level,
                "to_level": to_level,
                "exp_cost": exp_cost,
                "gold_cost": gold_cost,
                "experience_before": experience_before,
                "experience_after": updates["experience"],
                "gold_before": gold_before,
                "gold_after": updates["player_gold"],
                "character_updates": dict(updates),
            })
            return result

    def load_character_base_stats(
        self, character_id: int
    ) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM character_base_stats WHERE character_id = ?",
                (int(character_id),),
            ).fetchone()

    def update_character_base_stats(
        self, character_id: int, **updates: int
    ) -> dict[str, int]:
        if not updates:
            return {}
        invalid = sorted(set(updates) - STAT_KEY_NAMES)
        if invalid:
            raise ValueError(f"Unknown base stat key(s): {invalid}")
        normalized = {key: int(value) for key, value in updates.items()}
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        with self.session() as connection:
            cursor = connection.execute(
                f"UPDATE character_base_stats SET {assignments} "
                "WHERE character_id = ?",
                (*normalized.values(), int(character_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Missing character_base_stats for character_id={character_id}"
                )
        return normalized

    def set_character_stat_adjustment(
        self,
        character_id: int,
        source_key: str,
        stat_key: str,
        *,
        flat_value: int = 0,
        percent_bp: int = 0,
        enabled: bool = True,
        note: str = "",
    ) -> None:
        if stat_key not in STAT_KEY_NAMES:
            raise ValueError(f"Unknown stat key: {stat_key!r}")
        source = str(source_key).strip()
        if not source:
            raise ValueError("source_key must not be empty")
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO character_stat_adjustments(
                    character_id, source_key, stat_key, flat_value,
                    percent_bp, enabled, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(character_id, source_key, stat_key) DO UPDATE SET
                    flat_value=excluded.flat_value,
                    percent_bp=excluded.percent_bp,
                    enabled=excluded.enabled,
                    note=excluded.note
                """,
                (
                    int(character_id), source, stat_key, int(flat_value),
                    int(percent_bp), 1 if enabled else 0, str(note),
                ),
            )

    def list_character_stat_adjustments(
        self, character_id: int
    ) -> list[sqlite3.Row]:
        with self.session() as connection:
            return list(connection.execute(
                """
                SELECT source_key, stat_key, flat_value, percent_bp, note
                FROM character_stat_adjustments
                WHERE character_id = ? AND enabled = 1
                ORDER BY id
                """,
                (int(character_id),),
            ).fetchall())

    def load_character_skill_levels(self, character_id: int) -> dict[int, int]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT skill_id, skill_level
                FROM character_skills
                WHERE character_id = ?
                ORDER BY skill_id
                """,
                (int(character_id),),
            ).fetchall()
            return {
                int(row["skill_id"]): max(0, int(row["skill_level"]))
                for row in rows
            }

    def load_skill_effective_modifiers(
        self,
        profession: int,
        skill_id: int,
        skill_level: int,
    ) -> list[dict[str, object]]:
        """Return derived skill modifiers without mutating character columns."""
        level = max(0, int(skill_level))
        with self.session() as connection:
            result: list[dict[str, object]] = []
            for stat_key, amount in self._load_skill_upgrade_effects(
                connection, int(profession), int(skill_id)
            ).items():
                if stat_key not in STAT_KEY_NAMES:
                    continue
                result.append({
                    "stat_key": stat_key,
                    "flat_value": int(amount) * level,
                    "percent_bp": 0,
                    "source_key": "attribute_rule_per_level",
                    "note": "skill_upgrade_effects",
                })
            for stat_key, (first, interval, amount) in (
                self._load_skill_upgrade_milestone_effects(
                    connection, int(profession), int(skill_id)
                ).items()
            ):
                if stat_key not in STAT_KEY_NAMES:
                    continue
                triggers = self._milestone_count_at_level(level, first, interval)
                if triggers:
                    result.append({
                        "stat_key": stat_key,
                        "flat_value": int(amount) * triggers,
                        "percent_bp": 0,
                        "source_key": f"attribute_rule_milestone_{first}_{interval}",
                        "note": "skill_upgrade_milestone_effects",
                    })

            rows = connection.execute(
                """
                SELECT profession, stat_key, flat_per_level,
                       percent_bp_per_level, enabled, note
                FROM skill_stat_modifiers
                WHERE skill_id = ? AND profession IN (0, ?)
                ORDER BY profession
                """,
                (int(skill_id), int(profession)),
            ).fetchall()
            configured: dict[str, sqlite3.Row] = {}
            for row in rows:
                configured[str(row["stat_key"])] = row
            for stat_key, row in configured.items():
                if stat_key not in STAT_KEY_NAMES or int(row["enabled"]) == 0:
                    continue
                result.append({
                    "stat_key": stat_key,
                    "flat_value": int(row["flat_per_level"]) * level,
                    "percent_bp": int(row["percent_bp_per_level"]) * level,
                    "source_key": "configured_per_level",
                    "note": str(row["note"] or ""),
                })
            return result

    def list_equipment_stat_modifiers(
        self, template_id: int
    ) -> list[sqlite3.Row]:
        with self.session() as connection:
            return list(connection.execute(
                """
                SELECT stat_key, flat_value, percent_bp, note
                FROM equipment_stat_modifiers
                WHERE template_id = ? AND enabled = 1
                ORDER BY stat_key
                """,
                (int(template_id),),
            ).fetchall())

    def update_character_resources(
        self,
        character_id: int,
        *,
        hp: int | None = None,
        mp: int | None = None,
        sp: int | None = None,
    ) -> dict[str, int]:
        updates = {
            key: max(0, int(value))
            for key, value in (("hp", hp), ("mp", mp), ("sp", sp))
            if value is not None
        }
        if not updates:
            return {}
        with self.session() as connection:
            assignments = ", ".join(f"{column} = ?" for column in updates)
            connection.execute(
                f"UPDATE characters SET {assignments} WHERE id = ?",
                (*updates.values(), int(character_id)),
            )
        return updates

    def apply_character_regeneration(
        self,
        character_id: int,
        regeneration_ticks: int = 1,
        *,
        max_hp: int | None = None,
        max_mp: int | None = None,
        max_sp: int | None = None,
        hp_regen_per_second: int | None = None,
        mp_regen_per_second: int | None = None,
        sp_regen_per_second: int | None = None,
    ) -> dict[str, object]:
        """Persist current resources using caller-supplied effective limits/rates.

        The runtime stat engine is authoritative for maxima and regeneration.
        Legacy character columns are read only as a compatibility fallback when
        an older caller omits the explicit values.
        """
        ticks = max(0, int(regeneration_ticks))
        result: dict[str, object] = {
            "character_id": int(character_id),
            "regeneration_ticks": ticks,
            "elapsed_seconds": ticks,
            "changed": False,
        }
        if ticks <= 0:
            return result
        connection = self.connect()
        try:
            row = connection.execute(
                """
                SELECT hp, max_hp, mp, max_mp, sp, max_sp,
                       hp_regen_per_second, mp_regen_per_second,
                       sp_regen_per_second
                FROM characters
                WHERE id = ?
                """,
                (int(character_id),),
            ).fetchone()
            if row is None:
                result["reason"] = "character-not-found"
                return result

            effective = {
                "max_hp": int(row["max_hp"] if max_hp is None else max_hp),
                "max_mp": int(row["max_mp"] if max_mp is None else max_mp),
                "max_sp": int(row["max_sp"] if max_sp is None else max_sp),
                "hp_regen_per_second": int(
                    row["hp_regen_per_second"]
                    if hp_regen_per_second is None else hp_regen_per_second
                ),
                "mp_regen_per_second": int(
                    row["mp_regen_per_second"]
                    if mp_regen_per_second is None else mp_regen_per_second
                ),
                "sp_regen_per_second": int(
                    row["sp_regen_per_second"]
                    if sp_regen_per_second is None else sp_regen_per_second
                ),
            }
            updates: dict[str, int] = {}
            for current_column, maximum_key, regen_key in (
                ("hp", "max_hp", "hp_regen_per_second"),
                ("mp", "max_mp", "mp_regen_per_second"),
                ("sp", "max_sp", "sp_regen_per_second"),
            ):
                current = max(0, int(row[current_column]))
                maximum = max(0, int(effective[maximum_key]))
                rate = max(0, int(effective[regen_key]))
                new_value = min(maximum, current + rate * ticks)
                if new_value != current:
                    updates[current_column] = new_value

            if updates:
                assignments = ", ".join(f"{column} = ?" for column in updates)
                connection.execute(
                    f"UPDATE characters SET {assignments} WHERE id = ?",
                    (*updates.values(), int(character_id)),
                )
                result.update(updates)
                result["changed"] = True
            result.update(effective)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply_character_damage(
        self,
        character_id: int,
        damage: int,
        *,
        minimum_hp: int = 1,
        max_hp_override: int | None = None,
    ) -> dict[str, object]:
        """Apply authoritative player HP damage in SQLite.

        ``minimum_hp`` is currently kept at 1 by the combat runtime because the
        player death/respawn protocol has not yet been verified. Once that
        protocol is implemented, callers can pass 0 without changing this
        database method.
        """
        requested_damage = max(0, int(damage))
        hp_floor = max(0, int(minimum_hp))
        result: dict[str, object] = {
            "character_id": int(character_id),
            "requested_damage": requested_damage,
            "minimum_hp": hp_floor,
            "changed": False,
        }
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT hp, max_hp
                FROM characters
                WHERE id = ?
                """,
                (int(character_id),),
            ).fetchone()
            if row is None:
                result["reason"] = "character-not-found"
                connection.rollback()
                return result

            hp_before = max(0, int(row["hp"]))
            max_hp = max(
                0,
                int(row["max_hp"] if max_hp_override is None else max_hp_override),
            )
            effective_floor = min(hp_floor, max_hp)
            hp_after = max(effective_floor, hp_before - requested_damage)
            applied_damage = max(0, hp_before - hp_after)

            if hp_after != hp_before:
                connection.execute(
                    "UPDATE characters SET hp = ? WHERE id = ?",
                    (hp_after, int(character_id)),
                )
                result["changed"] = True

            connection.commit()
            result.update({
                "reason": "applied",
                "hp_before": hp_before,
                "hp_after": hp_after,
                "max_hp": max_hp,
                "applied_damage": applied_damage,
                "lethal_prevented": (
                    requested_damage > 0
                    and hp_before - requested_damage < effective_floor
                ),
            })
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_equipment_templates(self) -> list[sqlite3.Row]:
        with self.session() as connection:
            return list(connection.execute("SELECT * FROM equip ORDER BY id").fetchall())

    def get_equipment_template(self, item_id: int) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM equip WHERE id=?", (int(item_id),)
            ).fetchone()

    def grant_mob_kill_reward(
        self,
        character_id: int,
        experience_reward: int,
        gold_reward: int,
    ) -> tuple[bool, str, dict[str, int] | None]:
        """Credit one monster kill reward atomically.

        The caller supplies values already loaded from the killed monster's
        SQLite template.  Both persisted fields are capped at the unsigned
        32-bit range used by the confirmed client property packets.
        """
        character_id = int(character_id)
        requested_experience = max(0, int(experience_reward))
        requested_gold = max(0, int(gold_reward))
        with self.session() as connection:
            row = connection.execute(
                "SELECT experience,player_gold FROM characters WHERE id=?",
                (character_id,),
            ).fetchone()
            if row is None:
                return False, "character-not-found", {
                    "character_id": character_id,
                }

            experience_before = max(0, int(row["experience"] or 0))
            gold_before = max(0, int(row["player_gold"] or 0))
            experience_after = min(
                0xFFFFFFFF, experience_before + requested_experience
            )
            gold_after = min(0xFFFFFFFF, gold_before + requested_gold)
            credited_experience = experience_after - experience_before
            credited_gold = gold_after - gold_before

            if credited_experience > 0 or credited_gold > 0:
                updated = connection.execute(
                    """
                    UPDATE characters
                    SET experience=?, player_gold=?
                    WHERE id=?
                    """,
                    (experience_after, gold_after, character_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        "character disappeared during mob reward"
                    )

            return True, "mob-reward-credited", {
                "character_id": character_id,
                "experience_requested": requested_experience,
                "experience_awarded": credited_experience,
                "experience_before": experience_before,
                "experience_after": experience_after,
                "gold_requested": requested_gold,
                "gold_awarded": credited_gold,
                "gold_before": gold_before,
                "gold_after": gold_after,
            }

    def purchase_shop_item(
        self, character_id: int, item_id: int, unit_price: int,
        quantity: int = 1, client_slot_limit: int = 24,
        durability: int | None = None,
    ) -> tuple[bool, str, dict[str, int] | None]:
        """Create one unique equipment instance and index it in the bag."""
        character_id, item_id = int(character_id), int(item_id)
        unit_price, quantity = int(unit_price), int(quantity)
        if quantity != 1 or unit_price < 0:
            return False, "equipment-quantity-must-be-one", None
        total_price = unit_price * quantity
        with self.session() as connection:
            character = connection.execute(
                "SELECT player_gold,bag_capacity FROM characters WHERE id=?", (character_id,)
            ).fetchone()
            if character is None:
                return False, "character-not-found", None
            template = connection.execute(
                "SELECT durability,enabled FROM equip WHERE id=?", (item_id,)
            ).fetchone()
            if template is None or not int(template["enabled"] or 0):
                return False, "equipment-template-not-found", {"item_id": item_id}
            capacity = min(max(0, int(character["bag_capacity"])), max(0, int(client_slot_limit)))
            occupied = {
                int(row["bag_slot"])
                for row in connection.execute(
                    "SELECT bag_slot FROM character_bag WHERE character_id=?", (character_id,)
                ).fetchall()
            }
            free_slot = next((slot for slot in range(capacity) if slot not in occupied), None)
            if free_slot is None:
                return False, "bag-full", {"capacity": capacity}
            gold_before = int(character["player_gold"] or 0)
            if gold_before < total_price:
                return False, "insufficient-gold", {
                    "gold_before": gold_before, "total_price": total_price,
                }
            max_durability = int(template["durability"] or 0)
            current_durability = max_durability if durability is None else max(0, int(durability))
            if max_durability:
                current_durability = min(current_durability, max_durability)
            cursor = connection.execute(
                "UPDATE characters SET player_gold=player_gold-? WHERE id=? AND player_gold>=?",
                (total_price, character_id, total_price),
            )
            if cursor.rowcount != 1:
                return False, "gold-update-race", None
            instance_cursor = connection.execute(
                """
                INSERT INTO equip_instance(
                    template_id,owner_character_id,current_durability,
                    bind_state,source_type,source_id
                ) VALUES (?,?,?,0,'shop',?)
                """,
                (item_id, character_id, current_durability, item_id),
            )
            instance_id = int(instance_cursor.lastrowid)
            connection.execute(
                "INSERT INTO character_bag(character_id,bag_slot,instance_id) VALUES (?,?,?)",
                (character_id, free_slot, instance_id),
            )
            return True, "purchased", {
                "equip_instance_id": instance_id,
                # Compatibility label for older logs/tests; this is no longer a bag-row ID.
                "character_bag_id": instance_id,
                "item_id": item_id, "quantity": quantity, "bag_slot": free_slot,
                "unit_price": unit_price, "total_price": total_price,
                "gold_before": gold_before, "gold_after": gold_before-total_price,
            }

    def recycle_shop_item(
        self,
        character_id: int,
        instance_id: int,
        recycle_rate: int,
    ) -> tuple[bool, str, dict[str, int] | None]:
        """Recycle one unequipped owned item and credit gold atomically."""
        character_id = int(character_id)
        instance_id = int(instance_id)
        recycle_rate = max(0, int(recycle_rate))
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT ei.id AS instance_id,ei.template_id,
                       e.price,e.enabled,cb.bag_slot,
                       ce.slot_code AS equipped_slot,c.player_gold
                FROM equip_instance AS ei
                JOIN equip AS e ON e.id=ei.template_id
                JOIN characters AS c ON c.id=ei.owner_character_id
                LEFT JOIN character_bag AS cb
                  ON cb.instance_id=ei.id AND cb.character_id=ei.owner_character_id
                LEFT JOIN character_equip AS ce
                  ON ce.instance_id=ei.id AND ce.character_id=ei.owner_character_id
                WHERE ei.id=? AND ei.owner_character_id=?
                """,
                (instance_id, character_id),
            ).fetchone()
            if row is None:
                return False, "owned-instance-not-found", {
                    "instance_id": instance_id,
                }
            if not int(row["enabled"] or 0):
                return False, "item-template-disabled", {
                    "instance_id": instance_id,
                }
            if row["equipped_slot"] is not None:
                return False, "equipped-item-must-be-unequipped", {
                    "instance_id": instance_id,
                    "equipped_slot": int(row["equipped_slot"]),
                }
            if row["bag_slot"] is None:
                return False, "item-not-in-bag", {
                    "instance_id": instance_id,
                }

            base_price = max(0, int(row["price"] or 0))
            recycle_price = calculate_recycle_price(base_price, recycle_rate)
            gold_before = int(row["player_gold"] or 0)

            deleted = connection.execute(
                "DELETE FROM equip_instance WHERE id=? AND owner_character_id=?",
                (instance_id, character_id),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("equipment instance disappeared during recycle")
            updated = connection.execute(
                "UPDATE characters SET player_gold=player_gold+? WHERE id=?",
                (recycle_price, character_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("character disappeared during recycle")

            return True, "recycled", {
                "instance_id": instance_id,
                "item_id": int(row["template_id"]),
                "bag_slot": int(row["bag_slot"]),
                "base_price": base_price,
                "recycle_rate": recycle_rate,
                "recycle_price": recycle_price,
                "gold_before": gold_before,
                "gold_after": gold_before + recycle_price,
            }

    def repair_shop_item(
        self,
        character_id: int,
        instance_id: int,
        purchase_rate: int,
    ) -> tuple[bool, str, dict[str, int] | None]:
        """Restore one owned item to maximum durability in one transaction."""
        character_id = int(character_id)
        instance_id = int(instance_id)
        purchase_rate = max(0, int(purchase_rate))
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT ei.id AS instance_id,ei.template_id,
                       ei.current_durability,e.durability AS max_durability,
                       e.price,e.enabled,cb.bag_slot,
                       ce.slot_code AS equipped_slot,c.player_gold
                FROM equip_instance AS ei
                JOIN equip AS e ON e.id=ei.template_id
                JOIN characters AS c ON c.id=ei.owner_character_id
                LEFT JOIN character_bag AS cb
                  ON cb.instance_id=ei.id AND cb.character_id=ei.owner_character_id
                LEFT JOIN character_equip AS ce
                  ON ce.instance_id=ei.id AND ce.character_id=ei.owner_character_id
                WHERE ei.id=? AND ei.owner_character_id=?
                """,
                (instance_id, character_id),
            ).fetchone()
            if row is None:
                return False, "owned-instance-not-found", {
                    "instance_id": instance_id,
                }
            if not int(row["enabled"] or 0):
                return False, "item-template-disabled", {
                    "instance_id": instance_id,
                }
            if row["bag_slot"] is None and row["equipped_slot"] is None:
                return False, "item-has-no-valid-location", {
                    "instance_id": instance_id,
                }

            maximum = max(0, int(row["max_durability"] or 0))
            if maximum <= 0:
                return False, "item-not-repairable", {
                    "instance_id": instance_id,
                    "max_durability": maximum,
                }
            current = min(maximum, max(0, int(row["current_durability"] or 0)))
            missing = maximum - current
            base_price = max(0, int(row["price"] or 0))
            repair_cost = calculate_repair_price(
                base_price, current, maximum, purchase_rate
            )
            gold_before = int(row["player_gold"] or 0)
            result = {
                "instance_id": instance_id,
                "item_id": int(row["template_id"]),
                "current_durability": current,
                "max_durability": maximum,
                "missing_durability": missing,
                "base_price": base_price,
                "purchase_rate": purchase_rate,
                "repair_cost": repair_cost,
                "gold_before": gold_before,
            }
            if missing == 0:
                return True, "already-full", {
                    **result,
                    "new_durability": maximum,
                    "gold_after": gold_before,
                }
            if gold_before < repair_cost:
                return False, "insufficient-gold", {
                    **result,
                    "gold_after": gold_before,
                }

            updated = connection.execute(
                """
                UPDATE characters
                SET player_gold=player_gold-?
                WHERE id=? AND player_gold>=?
                """,
                (repair_cost, character_id, repair_cost),
            )
            if updated.rowcount != 1:
                return False, "gold-update-race", result
            repaired = connection.execute(
                """
                UPDATE equip_instance
                SET current_durability=?
                WHERE id=? AND owner_character_id=?
                """,
                (maximum, instance_id, character_id),
            )
            if repaired.rowcount != 1:
                raise RuntimeError("equipment instance disappeared during repair")

            return True, "repaired", {
                **result,
                "new_durability": maximum,
                "gold_after": gold_before - repair_cost,
            }

    def move_character_item(
        self, character_id: int, source_slot: int, target_slot: int,
        client_slot_limit: int = 60,
    ) -> tuple[bool, str, dict[str, int | None] | None]:
        character_id, source_slot, target_slot = int(character_id), int(source_slot), int(target_slot)
        with self.session() as connection:
            character = connection.execute(
                "SELECT bag_capacity FROM characters WHERE id=?", (character_id,)
            ).fetchone()
            if character is None:
                return False, "character-not-found", None
            capacity = min(max(0, int(character["bag_capacity"])), max(0, int(client_slot_limit)))
            if not (0 <= source_slot < capacity and 0 <= target_slot < capacity):
                return False, "slot-out-of-range", {
                    "source_slot": source_slot, "target_slot": target_slot, "capacity": capacity,
                }
            source = connection.execute(
                "SELECT instance_id FROM character_bag WHERE character_id=? AND bag_slot=?",
                (character_id, source_slot),
            ).fetchone()
            if source is None:
                return False, "source-empty", {
                    "source_slot": source_slot, "target_slot": target_slot, "capacity": capacity,
                }
            source_id = int(source["instance_id"])
            if source_slot == target_slot:
                return True, "no-op", {
                    "source_item_id": source_id, "target_item_id": None,
                    "source_slot": source_slot, "target_slot": target_slot, "capacity": capacity,
                }
            target = connection.execute(
                "SELECT instance_id FROM character_bag WHERE character_id=? AND bag_slot=?",
                (character_id, target_slot),
            ).fetchone()
            if target is None:
                connection.execute(
                    "UPDATE character_bag SET bag_slot=? WHERE character_id=? AND bag_slot=?",
                    (target_slot, character_id, source_slot),
                )
                return True, "moved", {
                    "source_item_id": source_id, "target_item_id": None,
                    "source_slot": source_slot, "target_slot": target_slot, "capacity": capacity,
                }
            target_id = int(target["instance_id"])
            connection.execute(
                "DELETE FROM character_bag WHERE character_id=? AND bag_slot IN (?,?)",
                (character_id, source_slot, target_slot),
            )
            connection.execute(
                "INSERT INTO character_bag(character_id,bag_slot,instance_id) VALUES (?,?,?)",
                (character_id, target_slot, source_id),
            )
            connection.execute(
                "INSERT INTO character_bag(character_id,bag_slot,instance_id) VALUES (?,?,?)",
                (character_id, source_slot, target_id),
            )
            return True, "swapped", {
                "source_item_id": source_id, "target_item_id": target_id,
                "source_slot": source_slot, "target_slot": target_slot, "capacity": capacity,
            }

    def get_backpack_item_at_slot(self, character_id: int, bag_slot: int) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                """
                SELECT ei.id AS instance_id,ei.id,ei.template_id AS item_id,
                       ei.template_id,1 AS quantity,ei.current_durability,
                       ei.owner_character_id,cb.character_id,cb.bag_slot
                FROM character_bag cb
                JOIN equip_instance ei ON ei.id=cb.instance_id
                WHERE cb.character_id=? AND cb.bag_slot=?
                """,
                (int(character_id), int(bag_slot)),
            ).fetchone()

    def _equipment_set_for_slot(
        self, connection: sqlite3.Connection, character_id: int, slot_code: int,
    ) -> int:
        if int(slot_code) not in DUAL_WEAPON_SLOT_CODES:
            return 0
        row = connection.execute(
            "SELECT active_weapon_set FROM characters WHERE id=?", (int(character_id),)
        ).fetchone()
        return 1 if row is None else min(2, max(1, int(row["active_weapon_set"] or 1)))

    def equip_character_item(
        self, character_id: int, source_bag_slot: int, target_equipped_slot: int,
        expected_item_id: int, client_slot_limit: int = 60,
    ) -> tuple[bool, str, dict[str, int | str | None] | None]:
        character_id, source_bag_slot = int(character_id), int(source_bag_slot)
        target_equipped_slot, expected_item_id = int(target_equipped_slot), int(expected_item_id)
        if target_equipped_slot not in EQUIP_SLOT_NAME_BY_CODE:
            return False, "unknown-equipment-slot", {"target_equipped_slot": target_equipped_slot}
        with self.session() as connection:
            character = connection.execute(
                "SELECT bag_capacity,active_weapon_set FROM characters WHERE id=?", (character_id,)
            ).fetchone()
            if character is None:
                return False, "character-not-found", None
            capacity = min(max(0, int(character["bag_capacity"])), max(0, int(client_slot_limit)))
            if not 0 <= source_bag_slot < capacity:
                return False, "source-slot-out-of-range", {
                    "source_slot": source_bag_slot, "capacity": capacity,
                }
            source = connection.execute(
                """
                SELECT ei.id AS instance_id,ei.template_id
                FROM character_bag cb
                JOIN equip_instance ei ON ei.id=cb.instance_id
                WHERE cb.character_id=? AND cb.bag_slot=? AND ei.owner_character_id=?
                """,
                (character_id, source_bag_slot, character_id),
            ).fetchone()
            if source is None:
                return False, "source-empty", None
            if int(source["template_id"]) != expected_item_id:
                return False, "source-item-changed", None
            source_id = int(source["instance_id"])
            weapon_set = self._equipment_set_for_slot(connection, character_id, target_equipped_slot)
            self._ensure_character_equip_rows(connection, character_id)
            equip_row = connection.execute(
                """
                SELECT * FROM character_equip
                WHERE character_id=? AND slot_code=? AND weapon_set=?
                """,
                (character_id, target_equipped_slot, weapon_set),
            ).fetchone()
            if equip_row is None:
                return False, "equipment-row-missing", None
            target_id = None if equip_row["instance_id"] is None else int(equip_row["instance_id"])
            connection.execute(
                "DELETE FROM character_bag WHERE character_id=? AND bag_slot=? AND instance_id=?",
                (character_id, source_bag_slot, source_id),
            )
            if target_id is not None:
                connection.execute(
                    "UPDATE character_equip SET instance_id=NULL WHERE id=?", (int(equip_row["id"]),)
                )
                connection.execute(
                    "INSERT INTO character_bag(character_id,bag_slot,instance_id) VALUES (?,?,?)",
                    (character_id, source_bag_slot, target_id),
                )
            connection.execute(
                "UPDATE character_equip SET instance_id=? WHERE id=?",
                (source_id, int(equip_row["id"])),
            )
            return True, "equipped-replaced" if target_id else "equipped", {
                "source_item_id": source_id, "replaced_item_id": target_id,
                "source_slot": source_bag_slot, "equipped_slot": target_equipped_slot,
                "weapon_set": weapon_set, "equipment_column": str(equip_row["slot_name"]),
                "capacity": capacity,
            }

    def unequip_character_item(
        self, character_id: int, source_equipped_slot: int, target_bag_slot: int,
        client_slot_limit: int = 60,
    ) -> tuple[bool, str, dict[str, int | str | None] | None]:
        character_id = int(character_id)
        source_equipped_slot, target_bag_slot = int(source_equipped_slot), int(target_bag_slot)
        if source_equipped_slot not in EQUIP_SLOT_NAME_BY_CODE:
            return False, "unknown-equipment-slot", None
        with self.session() as connection:
            character = connection.execute(
                "SELECT bag_capacity FROM characters WHERE id=?", (character_id,)
            ).fetchone()
            if character is None:
                return False, "character-not-found", None
            capacity = min(max(0, int(character["bag_capacity"])), max(0, int(client_slot_limit)))
            if not 0 <= target_bag_slot < capacity:
                return False, "target-slot-out-of-range", {
                    "target_slot": target_bag_slot, "capacity": capacity,
                }
            if connection.execute(
                "SELECT 1 FROM character_bag WHERE character_id=? AND bag_slot=?",
                (character_id, target_bag_slot),
            ).fetchone() is not None:
                return False, "target-bag-slot-occupied", None
            weapon_set = self._equipment_set_for_slot(connection, character_id, source_equipped_slot)
            equip_row = connection.execute(
                "SELECT * FROM character_equip WHERE character_id=? AND slot_code=? AND weapon_set=?",
                (character_id, source_equipped_slot, weapon_set),
            ).fetchone()
            if equip_row is None or equip_row["instance_id"] is None:
                return False, "source-equipment-empty", None
            source_id = int(equip_row["instance_id"])
            connection.execute(
                "UPDATE character_equip SET instance_id=NULL WHERE id=?", (int(equip_row["id"]),)
            )
            connection.execute(
                "INSERT INTO character_bag(character_id,bag_slot,instance_id) VALUES (?,?,?)",
                (character_id, target_bag_slot, source_id),
            )
            return True, "unequipped", {
                "source_item_id": source_id, "source_equipped_slot": source_equipped_slot,
                "weapon_set": weapon_set, "equipment_column": str(equip_row["slot_name"]),
                "target_slot": target_bag_slot, "capacity": capacity,
            }

    @staticmethod
    def _instance_attr_select_sql(alias: str = "ei") -> str:
        return ",\n".join(
            f"(SELECT printf('%d:%d',a.attr_key,a.attr_value) FROM equip_instance_attr a "
            f"WHERE a.instance_id={alias}.id AND a.slot_no={slot}) AS instance_attr{slot}"
            for slot in range(1, 6)
        )

    def list_items_for_character(self, character_id: int) -> list[sqlite3.Row]:
        attr_sql = self._instance_attr_select_sql("ei")
        with self.session() as connection:
            return list(connection.execute(
                f"""
                SELECT ei.id,ei.id AS instance_id,ei.template_id AS item_id,
                       ei.template_id,1 AS quantity,ei.current_durability,
                       cb.bag_slot,ce.slot_code AS equipped_slot,ce.weapon_set,
                       ce.slot_name AS equipment_column,c.active_weapon_set,
                       CASE WHEN ce.slot_code IN (210,211)
                            THEN ce.weapon_set=c.active_weapon_set
                            ELSE ce.instance_id IS NOT NULL END AS active_equipment,
                       {attr_sql}
                FROM equip_instance ei
                JOIN characters c ON c.id=ei.owner_character_id
                LEFT JOIN character_bag cb
                  ON cb.instance_id=ei.id AND cb.character_id=ei.owner_character_id
                LEFT JOIN character_equip ce
                  ON ce.instance_id=ei.id AND ce.character_id=ei.owner_character_id
                WHERE ei.owner_character_id=?
                ORDER BY CASE WHEN cb.bag_slot IS NULL THEN 1 ELSE 0 END,
                         cb.bag_slot,ce.slot_code,ce.weapon_set,ei.id
                """,
                (int(character_id),),
            ).fetchall())

    def list_equipped_items_for_character(self, character_id: int) -> list[dict[str, int | str]]:
        attr_sql = self._instance_attr_select_sql("ei")
        with self.session() as connection:
            rows = connection.execute(
                f"""
                SELECT ce.*,ei.id AS instance_id,ei.template_id AS item_id,
                       1 AS quantity,ei.current_durability,c.active_weapon_set,
                       {attr_sql}
                FROM character_equip ce
                JOIN equip_instance ei ON ei.id=ce.instance_id
                JOIN characters c ON c.id=ce.character_id
                WHERE ce.character_id=?
                  AND (ce.slot_code NOT IN (210,211) OR ce.weapon_set=c.active_weapon_set)
                ORDER BY ce.slot_code
                """,
                (int(character_id),),
            ).fetchall()
            result: list[dict[str, int | str]] = []
            for row in rows:
                item: dict[str, int | str] = {
                    "instance_id": int(row["instance_id"]),
                    "item_id": int(row["item_id"]),
                    "quantity": int(row["quantity"]),
                    "equipped_slot": int(row["slot_code"]),
                    "weapon_set": int(row["weapon_set"]),
                    "equipment_column": str(row["slot_name"]),
                }
                for slot in range(1, 6):
                    item[f"instance_attr{slot}"] = str(row[f"instance_attr{slot}"] or "")
                result.append(item)
            return result

    def get_character_equip(self, character_id: int) -> list[sqlite3.Row]:
        with self.session() as connection:
            self._ensure_character_equip_rows(connection, int(character_id))
            return list(connection.execute(
                "SELECT * FROM character_equip WHERE character_id=? ORDER BY slot_code,weapon_set",
                (int(character_id),),
            ).fetchall())

    def set_instance_attribute(
        self, instance_id: int, slot_no: int, attr_key: int, attr_value: int,
        attr_source: str = "", attr_level: int = 0,
    ) -> None:
        if not 1 <= int(slot_no) <= 5:
            raise ValueError("slot_no must be between 1 and 5")
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO equip_instance_attr(
                    instance_id,slot_no,attr_key,attr_value,attr_source,attr_level
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(instance_id,slot_no) DO UPDATE SET
                    attr_key=excluded.attr_key,attr_value=excluded.attr_value,
                    attr_source=excluded.attr_source,attr_level=excluded.attr_level
                """,
                (int(instance_id), int(slot_no), int(attr_key), int(attr_value), str(attr_source), int(attr_level)),
            )

    def set_active_weapon_set(self, character_id: int, weapon_set: int) -> bool:
        parsed = int(weapon_set)
        if parsed not in (1, 2):
            return False
        with self.session() as connection:
            cursor = connection.execute(
                "UPDATE characters SET active_weapon_set=? WHERE id=?", (parsed, int(character_id))
            )
            return cursor.rowcount == 1

    def load_runtime_character(
        self,
        account_username: str,
        character_name: str,
    ) -> sqlite3.Row:
        """
        Load one character together with its account and map data.

        Raises a clear RuntimeError when the configured database row is absent.
        """
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT
                    c.*,
                    a.username AS account_username,
                    m.terrain_name,
                    m.scene_file,
                    m.display_name AS map_display_name,
                    m.bgm_index AS map_bgm_index,
                    m.bgm_file AS map_bgm_file,
                    COALESCE(ps.normal_attack_interval_ms, 2000)
                        AS normal_attack_interval_ms
                FROM characters AS c
                JOIN accounts AS a ON a.id = c.account_id
                JOIN maps AS m ON m.id = c.map_id
                LEFT JOIN profession_level1_stats AS ps
                       ON ps.profession = c.profession AND ps.enabled = 1
                WHERE a.username = ?
                  AND c.name = ?
                """,
                (
                    account_username,
                    character_name,
                ),
            ).fetchone()

        if row is None:
            raise RuntimeError(
                "Database character not found: "
                f"account={account_username!r}, "
                f"character={character_name!r}, "
                f"database={self.db_path}"
            )

        return row
