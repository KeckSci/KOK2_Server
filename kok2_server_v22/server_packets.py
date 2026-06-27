from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

from protocol import c_string

OP_LOGIN_SERVER_LIST = 0x7009
OP_LOGIN_STATUS = 0x7000
OP_LOGIN_CHANGE_CONNECT = 0x7003
OP_LOGIN_SCENE_STATE = 0x0002

OP_GAME_MAP_INIT = 0x0002
OP_GAME_BEING_INIT = 0x0005
OP_GAME_ROLE_PROPERTY = 0x000A
OP_GAME_PROPERTY_UPDATE = 0x000E
OP_GAME_PROFILE = 0x001C
OP_GAME_OWNED_ITEM_REMOVE = 0x000C
OP_GAME_SYSTEM_MESSAGE = 0x0001
OP_GAME_CHAT_MESSAGE = 0x0012
OP_GAME_SKILL = 0x0022

# Confirmed client 0x000E property keys.
# The client handler for key 0x0014 stores the value in the role experience
# field and immediately refreshes the open skill-window experience label.
PROPERTY_KEY_LEVEL = 0x000C
PROPERTY_KEY_EXPERIENCE = 0x0014
PROPERTY_KEY_PLAYER_GOLD = 0x0015

LOGIN_SCENE_STATE = {
    "field00": 100000,
    "name": "TW10100",
    "field08": 10100,
    "field0A": 0,
    "field0C": 0,
    "field10": 0,
    "field14": 0,
}

LOGIN_STAGE_TRANSITION = (
    (0.50, 0x7003, lambda: b"".join((
        (1).to_bytes(2, "big"),
        (1).to_bytes(4, "big"),
        b"\x00",
        b"\x00",
    ))),
    (0.20, 0x7004, lambda: (1).to_bytes(2, "big")),
    (0.20, 0x7006, lambda: bytes((1,))),
    (0.20, 0x7008, lambda: b"".join(((1).to_bytes(2, "big"), c_string(""), c_string("")))),
)

GAME_BEING_DEFAULTS = {
    "field0C": 0,
    "field10": 0,
    "field18": 0,
    "field1A": 0,
    "field1C": 0,
    "field20": 100,
    "array1": (),
    "field2C": 0,
}

ROLE_PROPERTY_TEXT_ENCODING = "CP936"
ROLE_PROPERTY_UK1_LABEL = "00"
ROLE_PROPERTY_ZERO_AFTER_ATTRS = 0
ROLE_PROPERTY_TAIL_U16 = 0

SKILL_TABLE_XML = "skill_table.xml"
SKILL_DEFAULT_FIELD04 = 1
SKILL_DEFAULT_FIELD0C = 1
SKILL_DEFAULT_FIELD0E = 1
SKILL_DEFAULT_LEVEL = 1
SKILL_SEND_DELAY_SECONDS = 3.0
SKILL_INTER_RECORD_DELAY_SECONDS = 0.05

PROPERTY_UI_SEND_DELAY_SECONDS = 1.0
PROPERTY_UI_INTER_RECORD_DELAY_SECONDS = 0.15

# Movement speed update.
# Client 0x000E key 0x001D packs two uint16 values:
#   high word -> Being+0x150 (walk/state-3 speed)
#   low word  -> Being+0x154
# x32dbg memory testing confirmed 120 is too slow and 350 matches movement/animation well.
MOVEMENT_WALK_SPEED = 350
MOVEMENT_STATE2_SPEED = 100
MOVEMENT_SPEED_PROPERTY_KEY = 0x001D
MOVEMENT_SPEED_SEND_DELAY_SECONDS = 0.20

PROPERTY_UI_RECORDS = (
    (0x000C, "level", 1, "level"),
    (0x002A, "kill_count", 0, "kill_count"),
    (0x0034, "evil", 0, "evil"),
    (0x003E, "pk_win_count", 0, "pk_win_count"),
    (0x003F, "pk_loss_count", 0, "pk_loss_count"),
    (0x0065, "attack_power", 20, "attack_power"),
    (0x0066, "defense_power", 10, "defense_power"),
    (0x0072, "magic_attack_power", 20, "magic_attack_power"),
    (0x0045, "earth_resistance", 0, "earth_resistance"),
    (0x0046, "water_resistance", 0, "water_resistance"),
    (0x0047, "fire_resistance", 0, "fire_resistance"),
    (0x0048, "wind_resistance", 0, "wind_resistance"),
    (0x0049, "light_resistance", 0, "light_resistance"),
    (0x004A, "dark_resistance", 0, "dark_resistance"),
)

PROFILE_SEND_DELAY_SECONDS = 0.50


def now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def ascii_view(data: bytes) -> str:
    return "".join(chr(value) if 32 <= value <= 126 else "." for value in data)


def dump(label: str, data: bytes) -> None:
    print(f"[{now()}] {label}: {len(data)} bytes")
    if not data:
        print("<empty>")
        return
    print(data.hex(" "))
    print("ASCII:", ascii_view(data))


def row_value(row: Any, key: str, default: Any) -> Any:
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def clamped_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def parse_int_auto(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text, 0)
    except ValueError:
        return default


def parse_u32_array_text(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    text = str(value).strip()
    if not text:
        return ()
    values: list[int] = []
    for part in text.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part:
            values.append(parse_int_auto(part, 0) & 0xFFFFFFFF)
    return tuple(values)


def u32_array(values: Iterable[int]) -> bytes:
    value_tuple = tuple(int(value) & 0xFFFFFFFF for value in values)
    return len(value_tuple).to_bytes(4, "big") + b"".join(value.to_bytes(4, "big") for value in value_tuple)


def pack_grid_position(x: int, y: int) -> int:
    if not 0 <= int(x) <= 0xFFFF:
        raise ValueError(f"x out of range: {x}")
    if not 0 <= int(y) <= 0xFFFF:
        raise ValueError(f"y out of range: {y}")
    return (int(x) << 16) | int(y)


def profession_label(character: Any) -> str:
    profession = clamped_int(row_value(character, "profession", 0), 0, 0, 0xFFFF)
    labels = {
        0: "warrior",
        1: "warrior",
        2: "mage",
        3: "archer",
        4: "cleric",
        1000: "warrior",
        2000: "mage",
        3000: "cleric",
    }
    return labels.get(profession, f"job{profession}")


def runtime_character_stats(character: Any) -> dict[str, int | str]:
    hp = clamped_int(row_value(character, "hp", 300), 300, 0, 2_000_000_000)
    mp = clamped_int(row_value(character, "mp", 100), 100, 0, 2_000_000_000)
    sp = clamped_int(row_value(character, "sp", 200), 200, 0, 2_000_000_000)
    return {
        "name": str(row_value(character, "name", "")),
        "profession_label": profession_label(character),
        "organization_label": str(row_value(character, "organization_label", "无")),
        "country_label": str(row_value(character, "country_label", "无")),
        "reborn_label": str(row_value(character, "reborn_label", "00")),
        "level": clamped_int(row_value(character, "level", 1), 1, 1, 0x7FFF),
        "hp": hp,
        "max_hp": clamped_int(row_value(character, "max_hp", hp), hp, 0, 2_000_000_000),
        "mp": mp,
        "max_mp": clamped_int(row_value(character, "max_mp", mp), mp, 0, 2_000_000_000),
        "sp": sp,
        "max_sp": clamped_int(row_value(character, "max_sp", sp), sp, 0, 2_000_000_000),
        "hp_regen_per_second": clamped_int(row_value(character, "hp_regen_per_second", 0), 0, 0, 0xFFFFFFFF),
        "mp_regen_per_second": clamped_int(row_value(character, "mp_regen_per_second", 0), 0, 0, 0xFFFFFFFF),
        "sp_regen_per_second": clamped_int(row_value(character, "sp_regen_per_second", 0), 0, 0, 0xFFFFFFFF),
        "bag_capacity": clamped_int(row_value(character, "bag_capacity", 40), 40, 0, 255),
        "ancillary_profession": clamped_int(row_value(character, "ancillary_profession", 0), 0, 0, 255),
        "strength": clamped_int(row_value(character, "strength", 1), 1, 0, 0x7FFF),
        "wisdom": clamped_int(row_value(character, "wisdom", 1), 1, 0, 0x7FFF),
        "dexterity": clamped_int(row_value(character, "dexterity", 1), 1, 0, 0x7FFF),
        "constitution": clamped_int(row_value(character, "constitution", 1), 1, 0, 0x7FFF),
        "attack_power": clamped_int(row_value(character, "attack_power", 20), 20, 0, 0xFFFFFFFF),
        "defense_power": clamped_int(row_value(character, "defense_power", 10), 10, 0, 0xFFFFFFFF),
        "magic_attack_power": clamped_int(row_value(character, "magic_attack_power", 20), 20, 0, 0xFFFFFFFF),
        "earth_resistance": clamped_int(row_value(character, "earth_resistance", 0), 0, 0, 0xFFFFFFFF),
        "water_resistance": clamped_int(row_value(character, "water_resistance", 0), 0, 0, 0xFFFFFFFF),
        "fire_resistance": clamped_int(row_value(character, "fire_resistance", 0), 0, 0, 0xFFFFFFFF),
        "wind_resistance": clamped_int(row_value(character, "wind_resistance", 0), 0, 0, 0xFFFFFFFF),
        "light_resistance": clamped_int(row_value(character, "light_resistance", 0), 0, 0, 0xFFFFFFFF),
        "dark_resistance": clamped_int(row_value(character, "dark_resistance", 0), 0, 0, 0xFFFFFFFF),
        "reputation": clamped_int(row_value(character, "reputation", 0), 0, 0, 0xFFFFFFFF),
        "evil": clamped_int(row_value(character, "evil", 0), 0, 0, 0xFFFFFFFF),
        "kill_count": clamped_int(row_value(character, "kill_count", 0), 0, 0, 0xFFFFFFFF),
        "pk_win_count": clamped_int(row_value(character, "pk_win_count", 0), 0, 0, 0xFFFFFFFF),
        "pk_loss_count": clamped_int(row_value(character, "pk_loss_count", 0), 0, 0, 0xFFFFFFFF),
        "experience": clamped_int(row_value(character, "experience", 0), 0, 0, 0xFFFFFFFF),
        "player_gold": clamped_int(row_value(character, "player_gold", 888), 888, 0, 0xFFFFFFFF),
        "depot_gold": clamped_int(row_value(character, "depot_gold", 777), 777, 0, 0xFFFFFFFF),
    }


def player_base_type_for_gender(gender: int) -> int:
    return 0x00100002 if int(gender) == 2 else 0x00100001


def build_server_list_payload(name: str) -> bytes:
    return (1).to_bytes(2, "big") + c_string(name) + (1).to_bytes(2, "big")


def build_login_status_payload(status: int) -> bytes:
    return int(status).to_bytes(2, "big")


def build_login_scene_state_payload() -> bytes:
    state = LOGIN_SCENE_STATE
    return b"".join((
        int(state["field00"]).to_bytes(4, "big"),
        c_string(str(state["name"])),
        int(state["field08"]).to_bytes(2, "big"),
        int(state["field0A"]).to_bytes(2, "big"),
        int(state["field0C"]).to_bytes(2, "big"),
        int(state["field10"]).to_bytes(4, "big"),
        int(state["field14"]).to_bytes(4, "big"),
    ))


def build_map_init_payload(state: Any) -> bytes:
    return b"".join((
        int(state.game_map_id).to_bytes(4, "big"),
        c_string(str(state.game_map_name)),
        int(state.game_bgm_index).to_bytes(2, "big"),
        (0).to_bytes(2, "big"),
        (0).to_bytes(2, "big"),
        (0).to_bytes(4, "big"),
        int(state.packed_spawn).to_bytes(4, "big"),
    ))


def build_being_init_payload(state: Any) -> bytes:
    defaults = GAME_BEING_DEFAULTS
    return b"".join((
        c_string(state.being_name),
        c_string(state.being_string1),
        c_string(state.being_string2),
        int(defaults["field0C"]).to_bytes(1, "big"),
        int(defaults["field10"]).to_bytes(4, "big"),
        int(state.packed_spawn).to_bytes(4, "big"),
        int(defaults["field18"]).to_bytes(1, "big"),
        int(defaults["field1A"]).to_bytes(2, "big"),
        int(defaults["field1C"]).to_bytes(4, "big"),
        int(defaults["field20"]).to_bytes(1, "big"),
        u32_array(defaults["array1"]),
        int(defaults["field2C"]).to_bytes(1, "big"),
        u32_array(state.being_array2),
    ))


def normalize_npc_model_id(value: Any, default: str = "01001000") -> str:
    text = str(value or default).strip().lower()
    if text.startswith("npc_"):
        text = text[4:]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 8:
        digits = default
    return digits


def pack_npc_scale(scale_x: int, scale_y: int, scale_z: int) -> int:
    x = clamped_int(scale_x, 100, 0, 255)
    y = clamped_int(scale_y, 100, 0, 255)
    z = clamped_int(scale_z, 100, 0, 255)
    return ((z & 0xFF) << 16) | ((y & 0xFF) << 8) | (x & 0xFF)


def build_npc_mesh_array2(record: Any) -> tuple[int, ...]:
    """Build the 9-u32 NPC mesh array2 from npc_model_id/scale/ghost_mode.

    Confirmed by x32dbg + in-game tests:
      npc_01000000 -> 256,0,1,6579300,0,0,0,0,0
      npc_01001000 -> 256,1048576,1,6579300,0,0,0,0,0
      array2[3] = 0x00ZZYYXX scale bytes, 0x00646464 means 100/100/100.
      array2[4] = ghost_mode/render mode; 1 makes the NPC translucent.
    """
    model_id = normalize_npc_model_id(row_value(record, "npc_model_id", "01001000"))
    aa = int(model_id[0:2])
    bbb = int(model_id[2:5])
    ccc = int(model_id[5:8])
    return (
        (aa & 0xFF) << 8,
        ((bbb & 0xFFF) << 20) | ((ccc & 0xFFF) << 8),
        aa & 0xFF,
        pack_npc_scale(
            clamped_int(row_value(record, "scale_x", 100), 100, 0, 255),
            clamped_int(row_value(record, "scale_y", 100), 100, 0, 255),
            clamped_int(row_value(record, "scale_z", 100), 100, 0, 255),
        ),
        clamped_int(row_value(record, "ghost_mode", 0), 0, 0, 0xFFFFFFFF),
        0,
        0,
        0,
        0,
    )


def npc_array2_values(record: Any) -> tuple[int, ...]:
    manual = parse_u32_array_text(row_value(record, "array2_u32_array", ""))
    if manual:
        return manual
    return build_npc_mesh_array2(record)


def build_map_npc_being_payload(record: Any, map_id: int) -> bytes:
    npc_id = clamped_int(row_value(record, "id", 0), 0, 0, 999999)
    being_name = f"NPC{int(map_id)}_{npc_id:03d}"
    packed_position = pack_grid_position(
        clamped_int(row_value(record, "position_x", 0), 0, 0, 0xFFFF),
        clamped_int(row_value(record, "position_y", 0), 0, 0, 0xFFFF),
    )
    return b"".join((
        c_string(being_name),
        c_string(str(row_value(record, "display_name", being_name))),
        c_string(str(row_value(record, "title", ""))),
        clamped_int(row_value(record, "field0c", 0), 0, 0, 0xFF).to_bytes(1, "big"),
        clamped_int(row_value(record, "field10", 0), 0, 0, 0xFFFFFFFF).to_bytes(4, "big"),
        int(packed_position).to_bytes(4, "big"),
        clamped_int(row_value(record, "field18", 0), 0, 0, 0xFF).to_bytes(1, "big"),
        clamped_int(row_value(record, "direction", 0), 0, 0, 0xFFFF).to_bytes(2, "big"),
        clamped_int(row_value(record, "field1c", 0), 0, 0, 0xFFFFFFFF).to_bytes(4, "big"),
        clamped_int(row_value(record, "field20", 100), 100, 0, 0xFF).to_bytes(1, "big"),
        u32_array(parse_u32_array_text(row_value(record, "array1_u32_array", ""))),
        clamped_int(row_value(record, "field2c", 1), 1, 0, 0xFF).to_bytes(1, "big"),
        u32_array(npc_array2_values(record)),
    ))


def normalize_mob_model_id(value: Any) -> str:
    """Return one explicit eight-digit client Mob mesh resource ID.

    V17.43 proved that gameplay/data ID 1001000 is not a mesh mapping:
    interpreting it as 01001000 loads a winged demon.  Therefore this
    function no longer pads seven-digit values or falls back silently.
    Database maintenance must provide the real resource ID separately.
    """
    text = str(value or "").strip().lower()
    if text.startswith("mob_"):
        text = text[4:]
    if len(text) != 8 or not text.isdigit():
        raise ValueError(
            f"client_model_id must be an explicit 8-digit Mob resource ID, got {value!r}"
        )
    return text

def build_mob_mesh_array2(record: Any) -> tuple[int, ...]:
    """Build the nine-u32 mesh descriptor for a real monster model.

    Client model selection decodes the first three u32 values as follows:

        array2[0] bits 8..19: model family (0 = Mob, 1 = Npc)
        array2[1] bits 20..31: middle three decimal digits
        array2[1] bits  8..19: final three decimal digits
        array2[2] low byte:    first two decimal digits

    V17.43 directly confirmed that family 0 loads a real Mob resource, but
    also confirmed mob_01001000 is a winged demon rather than the chicken.
    The preserved v61 chicken packet uses mob_02000001, represented by
    array2=(0,256,2,6579300,0,0,0,0,0).

    A non-empty array2_u32_array in SQLite remains authoritative.
    """
    model_id = normalize_mob_model_id(
        row_value(record, "client_model_id", "")
    )
    aa = int(model_id[0:2])
    bbb = int(model_id[2:5])
    ccc = int(model_id[5:8])
    return (
        0,  # model family 0 = Mesh/Being/Mob/mob_*
        ((bbb & 0xFFF) << 20) | ((ccc & 0xFFF) << 8),
        aa & 0xFF,
        pack_npc_scale(
            clamped_int(row_value(record, "scale_x", 100), 100, 0, 255),
            clamped_int(row_value(record, "scale_y", 100), 100, 0, 255),
            clamped_int(row_value(record, "scale_z", 100), 100, 0, 255),
        ),
        clamped_int(row_value(record, "ghost_mode", 0), 0, 0, 0xFFFFFFFF),
        0,
        0,
        0,
        0,
    )


def mob_array2_values(record: Any) -> tuple[int, ...]:
    manual = parse_u32_array_text(row_value(record, "array2_u32_array", ""))
    if manual:
        return manual
    return build_mob_mesh_array2(record)


def build_map_mob_being_payload(record: Any, map_id: int) -> bytes:
    """Build one static map-monster opcode 0x0005 payload.

    map_mob_spawns supplies a stable internal_name with a #network_id suffix,
    so later 0x0004 chase/return packets can address the exact same Being.
    """
    spawn_id = clamped_int(row_value(record, "spawn_id", 0), 0, 0, 999999)
    fallback_name = f"NPC{int(map_id)}_{1000 + spawn_id:03d}"
    being_name = str(row_value(record, "internal_name", fallback_name) or fallback_name)
    packed_position = pack_grid_position(
        clamped_int(row_value(record, "position_x", 0), 0, 0, 0xFFFF),
        clamped_int(row_value(record, "position_y", 0), 0, 0, 0xFFFF),
    )
    return b"".join((
        c_string(being_name),
        c_string(str(row_value(record, "display_name", being_name))),
        c_string(""),
        clamped_int(row_value(record, "being_field0c", 0), 0, 0, 0xFF).to_bytes(1, "big"),
        clamped_int(row_value(record, "being_field10", 0), 0, 0, 0xFFFFFFFF).to_bytes(4, "big"),
        int(packed_position).to_bytes(4, "big"),
        clamped_int(row_value(record, "being_field18", 0), 0, 0, 0xFF).to_bytes(1, "big"),
        clamped_int(row_value(record, "direction", 0), 0, 0, 0xFFFF).to_bytes(2, "big"),
        clamped_int(row_value(record, "being_field1c", 0), 0, 0, 0xFFFFFFFF).to_bytes(4, "big"),
        clamped_int(row_value(record, "being_field20", 100), 100, 0, 0xFF).to_bytes(1, "big"),
        u32_array(parse_u32_array_text(row_value(record, "array1_u32_array", ""))),
        clamped_int(row_value(record, "being_field2c", 1), 1, 0, 0xFF).to_bytes(1, "big"),
        u32_array(mob_array2_values(record)),
    ))


def role_property_string(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        if b"\x00" in value:
            raise ValueError("NUL byte in role-property string")
        return value + b"\x00"
    return str(value).encode(ROLE_PROPERTY_TEXT_ENCODING, errors="strict") + b"\x00"


def build_role_property_payload(character: Any) -> bytes:
    stats = runtime_character_stats(character)
    return b"".join((
        role_property_string(stats["organization_label"]),
        role_property_string(stats["country_label"]),
        role_property_string(stats["reborn_label"]),
        role_property_string(ROLE_PROPERTY_UK1_LABEL),
        clamped_int(row_value(character, "profession", 1000), 1000, 0, 0xFFFF).to_bytes(2, "big"),
        int(stats["ancillary_profession"]).to_bytes(1, "big"),
        int(stats["bag_capacity"]).to_bytes(1, "big"),
        int(stats["hp"]).to_bytes(4, "big"),
        int(stats["max_hp"]).to_bytes(4, "big"),
        int(stats["mp"]).to_bytes(4, "big"),
        int(stats["max_mp"]).to_bytes(4, "big"),
        int(stats["sp"]).to_bytes(4, "big"),
        int(stats["max_sp"]).to_bytes(4, "big"),
        int(stats["strength"]).to_bytes(2, "big", signed=True),
        int(stats["dexterity"]).to_bytes(2, "big", signed=True),
        int(stats["wisdom"]).to_bytes(2, "big", signed=True),
        int(stats["constitution"]).to_bytes(2, "big", signed=True),
        int(ROLE_PROPERTY_ZERO_AFTER_ATTRS).to_bytes(4, "big"),
        int(stats["experience"]).to_bytes(4, "big"),
        int(stats["reputation"]).to_bytes(4, "big"),
        int(ROLE_PROPERTY_TAIL_U16).to_bytes(2, "big"),
    ))


CHAT_MESSAGE_TYPE_GREEN = 2


def build_system_message_record(message: str) -> bytes:
    """Build one confirmed opcode 0x0001 server-message record.

    The original client descriptor contains one variable-length C string.
    Its handler adds the text to UIChat2 and applies the client's fixed
    pale-gold color (#FDF597).
    """
    return c_string(message)


def build_chat_message_record(
    message_type: int,
    sender: str,
    message: str,
) -> bytes:
    """Build one confirmed opcode 0x0012 message record.

    Wire layout:
        uint8 message_type
        C string sender
        C string message

    The client only handles message types 0 through 11.  Type 2 is the
    confirmed green UIChat2 path (#42FF00).  An empty sender yields a plain
    green line without a character-name prefix.
    """
    value = int(message_type)
    if not 0 <= value <= 11:
        raise ValueError(f"message type out of range (0..11): {message_type}")
    return bytes((value,)) + c_string(sender) + c_string(message)


def build_property_update_payload(key: int, value: int) -> bytes:
    return (int(key) & 0xFFFF).to_bytes(2, "big") + (int(value) & 0xFFFFFFFF).to_bytes(4, "big")


def build_movement_speed_payload(walk_speed: int = MOVEMENT_WALK_SPEED, state2_speed: int = MOVEMENT_STATE2_SPEED) -> bytes:
    walk = clamped_int(walk_speed, MOVEMENT_WALK_SPEED, 0, 0xFFFF)
    state2 = clamped_int(state2_speed, MOVEMENT_STATE2_SPEED, 0, 0xFFFF)
    packed_value = ((walk & 0xFFFF) << 16) | (state2 & 0xFFFF)
    return build_property_update_payload(MOVEMENT_SPEED_PROPERTY_KEY, packed_value)


def iter_property_ui_records(character: Any) -> list[tuple[int, int, str, str]]:
    stats = runtime_character_stats(character)
    records: list[tuple[int, int, str, str]] = []
    for key, stat_name, fallback, label in PROPERTY_UI_RECORDS:
        records.append((key, clamped_int(stats.get(stat_name), fallback, 0, 0xFFFFFFFF), label, stat_name))
    return records


def profile_gender_flag(character: Any) -> int:
    return 1 if clamped_int(row_value(character, "gender", 1), 1, 0, 255) == 2 else 0


def build_profile_payload(character: Any) -> bytes:
    stats = runtime_character_stats(character)
    values = (
        stats["player_gold"], stats["depot_gold"], stats["kill_count"], stats["evil"],
        stats["pk_win_count"], stats["pk_loss_count"], stats["earth_resistance"],
        stats["water_resistance"], stats["fire_resistance"], stats["wind_resistance"],
        stats["light_resistance"], stats["dark_resistance"], stats["reputation"],
    )
    return b"".join((
        profile_gender_flag(character).to_bytes(1, "big"),
        int(stats["level"]).to_bytes(2, "big", signed=True),
        b"".join(clamped_int(value, 0, 0, 0xFFFFFFFF).to_bytes(4, "big") for value in values),
    ))


def resolve_local_path(path_text: str | Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def calc_skill_exp_to_next(level: int, root: ET.Element | None = None) -> int:
    """Legacy fallback retained for compatibility; DB rules are authoritative."""
    level = clamped_int(level, 1, 0, 0xFF)
    constant = 10
    quadratic = 88
    linear = 88
    shift = 1
    if root is not None:
        formula = root.find("experience_formula")
        if formula is not None:
            constant = parse_int_auto(formula.get("constant"), constant)
            quadratic = parse_int_auto(formula.get("quadratic"), quadratic)
            linear = parse_int_auto(formula.get("linear"), linear)
            shift = parse_int_auto(formula.get("shift"), shift)
    n = level - shift
    return max(0, int(constant + quadratic * (n ** 2) + linear * n)) & 0xFFFFFFFF


def load_skill_lookup_xml(xml_path: Path | str = SKILL_TABLE_XML) -> tuple[ET.Element, str, dict[int, ET.Element], tuple[int, ...]]:
    path = resolve_local_path(xml_path)
    if not path.exists():
        raise FileNotFoundError(f"skill table XML not found: {path}")
    root = ET.parse(path).getroot()
    defaults = root.find("opcode_0022_defaults")
    default_field1c = parse_u32_array_text(defaults.get("field1C") if defaults is not None else "")
    skill_by_value: dict[int, ET.Element] = {}
    skills_node = root.find("skills")
    if skills_node is not None:
        for skill_node in skills_node.findall("skill"):
            value = parse_int_auto(skill_node.get("value") or skill_node.get("id"), None)
            if value is not None:
                skill_by_value[int(value) & 0xFFFF] = skill_node
    return root, path.name, skill_by_value, default_field1c


def make_skill_record(skill_id: int, field04: int, field0c: int, field0e: int, current_level: int, field1c_values: Iterable[int], root: ET.Element, skill_by_value: dict[int, ET.Element]) -> tuple[Any, ...]:
    skill_id = int(skill_id) & 0xFFFF
    skill_node = skill_by_value.get(skill_id)
    ui_skill = parse_int_auto(skill_node.get("ui_skill") if skill_node is not None else None, 0)
    skill_type = parse_int_auto(skill_node.get("skill_type") if skill_node is not None else None, 0)
    skill_name = skill_node.get("name") if skill_node is not None and skill_node.get("name") is not None else ""
    current_level = clamped_int(current_level, 1, 0, 0xFF)
    exp_to_next = calc_skill_exp_to_next(current_level, root)
    required_level = parse_int_auto(skill_node.get("required_level") if skill_node is not None else None, current_level)
    return (skill_id, ui_skill, skill_type, field04, skill_name, field0c, field0e, current_level, exp_to_next, required_level, tuple(field1c_values or ()))


def load_skill_records_from_db(database: Any, character: Any, xml_path: Path | str = SKILL_TABLE_XML) -> list[tuple[Any, ...]]:
    character_id = int(character["id"])
    profession = int(row_value(character, "profession", 1000))
    character_level = int(row_value(character, "level", 1))
    rows = database.list_skills_for_character(character_id)
    # character_skills is authoritative.  An empty result intentionally sends
    # no skills; runtime packet building must not recreate deleted rows.
    root, xml_name, skill_by_value, default_field1c = load_skill_lookup_xml(xml_path)
    records = []
    for row in rows:
        field1c = parse_u32_array_text(row["field1c_u32_array"]) or default_field1c
        skill_id = int(row["skill_id"])
        current_level = clamped_int(
            row["skill_level"], SKILL_DEFAULT_LEVEL, 0, 0xFF
        )
        record = list(make_skill_record(
            skill_id,
            clamped_int(row["field04_unknown"], SKILL_DEFAULT_FIELD04, 0, 0xFF),
            clamped_int(row["field0c_unknown"], SKILL_DEFAULT_FIELD0C, 0, 0xFF),
            clamped_int(row["field0e_unknown"], SKILL_DEFAULT_FIELD0E, 0, 0xFFFF),
            current_level,
            field1c,
            root,
            skill_by_value,
        ))
        rule = database.get_skill_upgrade_rule(skill_id, current_level)
        if rule is None:
            # No enabled DB rule means the server will reject further upgrades.
            # Use an unreachable role-level gate rather than advertising a
            # guessed/free cost to the client.
            record[8] = 0
            record[9] = 0xFF
        else:
            record[8] = clamped_int(rule["exp_cost"], 0, 0, 0xFFFFFFFF)
            record[9] = clamped_int(
                rule["required_character_level"], 0, 0, 0xFF
            )
        records.append(tuple(record))
    print(f"[{now()}] Loaded {len(records)} opcode 0x0022 skill records from SQLite character_skills: character_id={character_id}, character_name={character['name']!r}, profession={profession}, xml={xml_name}")
    return records


def build_skill_payload(record: tuple[Any, ...]) -> bytes:
    if len(record) != 11:
        raise ValueError("skill record must have 11 fields")
    field00, field02, field03, field04, field08, field0c, field0e, field10, field14, field18, field1c = record
    field10 = clamped_int(field10, 1, 0, 0xFF)
    return b"".join((
        clamped_int(field00, 1, 0, 0xFFFF).to_bytes(2, "big"),
        clamped_int(field02, 0, 0, 0xFF).to_bytes(1, "big"),
        clamped_int(field03, 0, 0, 0xFF).to_bytes(1, "big"),
        clamped_int(field04, 1, 0, 0xFF).to_bytes(1, "big"),
        c_string(str(field08)),
        clamped_int(field0c, 1, 0, 0xFF).to_bytes(1, "big"),
        clamped_int(field0e, 1, 0, 0xFFFF).to_bytes(2, "big"),
        field10.to_bytes(1, "big"),
        clamped_int(field14, calc_skill_exp_to_next(field10), 0, 0xFFFFFFFF).to_bytes(4, "big"),
        clamped_int(field18, field10, 0, 0xFF).to_bytes(1, "big"),
        u32_array(tuple(clamped_int(v, 0, 0, 0xFFFFFFFF) for v in (field1c or ()))),
    ))
