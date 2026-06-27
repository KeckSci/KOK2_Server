from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import config
from database import Database, is_safe_character_name
from character_stat_service import CharacterStatService
from stat_engine import StatModifier
from game_handlers import GameHandlerRuntime, configure_game_handlers
from login_handlers import CreateRoleRequest
from shop_protocol import (
    configure_equipment_catalog,
    configure_shop_definitions,
)
from server_packets import (
    clamped_int,
    pack_grid_position,
    player_base_type_for_gender,
    row_value,
    runtime_character_stats,
)

DATABASE = Database(config.DATABASE_PATH)
MAP_TABLE_RUNTIME_SYNC_DONE = False


@dataclass
class RuntimeState:
    # ``character`` is a protocol-compatible merged view.  Persistent identity
    # and current resources come from SQLite; derived attributes come only from
    # ``stat_service``.
    character: dict[str, Any] | None = None
    raw_character: Any | None = None
    stat_service: CharacterStatService | None = None
    resource_clamps: tuple[str, ...] = ()
    role_tokens: list[str] = field(default_factory=list)
    role_names: set[str] = field(default_factory=set)
    active_account_username: str = ""
    selected_character_name: str = ""
    login_authenticated: bool = False
    game_map_id: int = 0
    game_map_name: str = ""
    game_bgm_index: int = 8
    game_spawn_x: int = 0
    game_spawn_y: int = 0
    packed_spawn: int = 0
    being_name: str = ""
    being_string1: str = ""
    being_string2: str = ""
    being_array2: tuple[int, ...] = ()


STATE = RuntimeState()


def role_map_label(character: Any) -> str:
    display_name = str(row_value(character, "map_display_name", "")).strip()
    if display_name:
        return display_name
    last_logout_area = str(row_value(character, "last_logout_area", "")).strip()
    if last_logout_area:
        return last_logout_area
    return str(row_value(character, "map_id", ""))




def build_role_token(character: Any) -> str:
    return (
        f"{character['name']}#"
        f"{int(character['level'])}#"
        f"{int(character['gender'])}#"
        f"{int(character['body'])}#"
        f"{int(character['hair'])}#"
        f"{int(character['head'])}#"
        f"{int(character['hand_r'])}#"
        f"{int(character['hand_l'])}#"
        f"{int(character['pants'])}#"
        f"{int(character['foot_r'])}#"
        f"{int(character['foot_l'])}#"
        f"0#0#0#0#0#"
        f"{role_map_label(character)}#"
        f"{int(character['profession'])}#0#{row_value(character, 'country_label', '无')}#0"
    )


def password_hash_for_plaintext(password: str) -> str:
    return "sha256$" + hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    if password_hash.startswith("sha256$"):
        return password_hash_for_plaintext(password) == password_hash
    return password == password_hash


def reset_login_session() -> None:
    STATE.active_account_username = ""
    STATE.selected_character_name = ""
    STATE.login_authenticated = False


def current_account_username() -> str:
    if not STATE.login_authenticated or not STATE.active_account_username:
        raise RuntimeError("No authenticated account is bound to this login connection")
    return STATE.active_account_username


def client_username_candidates(username: str) -> list[str]:
    text = str(username or "")
    candidates = [text]
    if len(text) > 1:
        for start in range(1, len(text)):
            suffix = text[start:]
            if suffix and suffix not in candidates:
                candidates.append(suffix)
    return candidates


def bind_account_login_from_credentials(username: str, password: str) -> tuple[bool, str, list[str], set[str]]:
    for candidate in client_username_candidates(username):
        account = DATABASE.get_account_by_username(candidate)
        if account is None:
            continue
        if not verify_password(password, str(account["password_hash"])):
            continue
        DATABASE.update_account_last_login(int(account["id"]))
        STATE.login_authenticated = True
        STATE.active_account_username = str(account["username"])
        tokens, names = load_role_tokens_for_account(STATE.active_account_username)
        STATE.role_tokens = tokens
        STATE.role_names = names
        print(f"[runtime] Account authenticated: account={STATE.active_account_username!r}, role_count={len(tokens)}")
        return True, STATE.active_account_username, tokens, names
    STATE.login_authenticated = False
    STATE.active_account_username = "<auth-failed>"
    return False, "<auth-failed>", [], set()


def load_role_tokens_for_account(account_username: str) -> tuple[list[str], set[str]]:
    ensure_map_table_runtime_synced()
    account = DATABASE.get_account_by_username(account_username)
    if account is None:
        raise RuntimeError(f"Account not found in database: {account_username!r}")
    characters = DATABASE.list_characters_for_account(int(account["id"]))
    tokens: list[str] = []
    names: set[str] = set()
    skipped: list[str] = []
    for character in characters:
        role_name = str(character["name"])
        if not is_safe_character_name(role_name):
            skipped.append(repr(role_name))
            continue
        tokens.append(build_role_token(character))
        names.add(role_name)
    if skipped:
        print(f"[runtime] skipped unsafe role rows for account={account_username!r}: {skipped!r}")
    return tokens, names


def select_character_for_login(role_name: str) -> bool:
    account_username = current_account_username()
    character = DATABASE.load_runtime_character(account_username, role_name)
    STATE.selected_character_name = role_name
    STATE.character = character
    print(f"[runtime] Selected character: account={account_username!r}, role={role_name!r}, map_id={int(character['map_id'])}")
    return True


def create_character_for_login(request: CreateRoleRequest) -> tuple[bool, str, list[str], set[str]]:
    if not STATE.login_authenticated:
        return False, "not-authenticated", [], set()
    account_username = current_account_username()
    ok, reason, character = DATABASE.create_character_for_account(
        account_username=account_username,
        name=request.name,
        gender=request.gender,
        profession=request.profession,
        map_id=config.NEW_CHARACTER_MAP_ID,
        position_x=config.NEW_CHARACTER_SPAWN_X,
        position_y=config.NEW_CHARACTER_SPAWN_Y,
        direction=0,
        body=request.body,
        hair=request.hair,
        head=request.head,
        hand_r=request.hand_r,
        hand_l=request.hand_l,
        pants=request.pants,
        foot_r=request.foot_r,
        foot_l=request.foot_l,
        bag_capacity=40,
        ancillary_profession=0,
    )
    tokens, names = load_role_tokens_for_account(account_username)
    STATE.role_tokens = tokens
    STATE.role_names = names
    if ok:
        STATE.selected_character_name = request.name
        print(f"[runtime] Created character: account={account_username!r}, role={request.name!r}, id={int(character['id']) if character is not None else '<unknown>'}")
    return ok, reason, tokens, names


def delete_character_for_login(role_name: str) -> tuple[bool, str, list[str], set[str]]:
    if not STATE.login_authenticated:
        return False, "not-authenticated", [], set()
    account_username = current_account_username()
    ok, reason = DATABASE.delete_character_for_account(account_username, role_name)
    if STATE.selected_character_name == role_name:
        STATE.selected_character_name = ""
    tokens, names = load_role_tokens_for_account(account_username)
    STATE.role_tokens = tokens
    STATE.role_names = names
    print(f"[runtime] Delete character result: account={account_username!r}, role={role_name!r}, ok={ok}, reason={reason!r}")
    return ok, reason, tokens, names


def ensure_map_table_runtime_synced() -> None:
    """Keep historical call sites, but never overwrite maps from XML at runtime."""
    global MAP_TABLE_RUNTIME_SYNC_DONE
    if MAP_TABLE_RUNTIME_SYNC_DONE:
        return
    with DATABASE.session() as connection:
        map_count = int(connection.execute("SELECT COUNT(*) FROM maps").fetchone()[0])
    if map_count == 0:
        raise RuntimeError(
            "maps table is empty. Run init_database.py once, or insert map rows manually."
        )
    print(f"[runtime] using SQLite maps table without XML synchronization: rows={map_count}")
    MAP_TABLE_RUNTIME_SYNC_DONE = True


def _clamp_current_resources(data: dict[str, Any]) -> tuple[str, ...]:
    updates: dict[str, int] = {}
    for current_name, maximum_name in (
        ("hp", "max_hp"),
        ("mp", "max_mp"),
        ("sp", "max_sp"),
    ):
        current = max(0, int(data.get(current_name, 0) or 0))
        maximum = max(0, int(data.get(maximum_name, 0) or 0))
        clamped = min(current, maximum)
        if clamped != current:
            data[current_name] = clamped
            updates[current_name] = clamped
    if updates:
        DATABASE.update_character_resources(
            int(data["id"]),
            hp=updates.get("hp"),
            mp=updates.get("mp"),
            sp=updates.get("sp"),
        )
    return tuple(updates)


def build_effective_character_view(character: Any) -> dict[str, Any]:
    """Merge one raw character row with the authoritative stat snapshot."""
    character_id = int(character["id"])
    profession = int(character["profession"])
    service = STATE.stat_service
    if (
        service is None
        or service.character_id != character_id
        or service.profession != profession
    ):
        service = CharacterStatService(DATABASE, character_id, profession)
        STATE.stat_service = service
    snapshot, changed = service.recalculate()
    data = dict(character)
    data.update(snapshot.as_dict())
    STATE.resource_clamps = _clamp_current_resources(data)
    STATE.raw_character = character
    print(
        f"[runtime] Effective stat snapshot: character_id={character_id}, "
        f"changed={','.join(changed) or '<none>'}, "
        f"attack={data['attack_power']}, defense={data['defense_power']}, "
        f"magic={data['magic_attack_power']}, "
        f"hp_max={data['max_hp']}, mp_max={data['max_mp']}, "
        f"sp_max={data['max_sp']}, attack_interval_ms={data['normal_attack_interval_ms']}"
    )
    return data


def refresh_runtime_character_stats() -> tuple[dict[str, Any], tuple[str, ...]]:
    """Reload persistent state and recompute only the derived snapshot."""
    if STATE.character is None or STATE.stat_service is None:
        raise RuntimeError("No runtime character/stat service is loaded")
    character_id = int(STATE.character["id"])
    account_username = str(STATE.character.get("account_username", ""))
    character_name = str(STATE.character["name"])
    raw = DATABASE.load_runtime_character(account_username, character_name)
    snapshot, changed = STATE.stat_service.recalculate()
    data = dict(raw)
    data.update(snapshot.as_dict())
    STATE.resource_clamps = _clamp_current_resources(data)
    STATE.raw_character = raw
    STATE.character = data
    return data, changed


def _apply_snapshot_to_runtime(snapshot) -> tuple[dict[str, Any], tuple[str, ...]]:
    if STATE.character is None:
        raise RuntimeError("No runtime character is loaded")
    data = dict(STATE.character)
    data.update(snapshot.as_dict())
    STATE.resource_clamps = _clamp_current_resources(data)
    STATE.character = data
    return data, STATE.resource_clamps


def set_temporary_stat_effect(
    effect_id: str,
    modifiers: list[StatModifier] | tuple[StatModifier, ...],
    *,
    duration_seconds: float | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if STATE.stat_service is None:
        raise RuntimeError("No runtime stat service is loaded")
    snapshot, changed = STATE.stat_service.set_temporary_effect(
        effect_id,
        modifiers,
        duration_seconds=duration_seconds,
    )
    data, _ = _apply_snapshot_to_runtime(snapshot)
    return data, changed


def remove_temporary_stat_effect(
    effect_id: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if STATE.stat_service is None:
        raise RuntimeError("No runtime stat service is loaded")
    snapshot, changed = STATE.stat_service.remove_temporary_effect(effect_id)
    data, _ = _apply_snapshot_to_runtime(snapshot)
    return data, changed


def prune_expired_temporary_stat_effects(
) -> tuple[dict[str, Any], tuple[str, ...]] | None:
    if STATE.stat_service is None:
        return None
    result = STATE.stat_service.prune_expired_effects()
    if result is None:
        return None
    snapshot, changed = result
    data, _ = _apply_snapshot_to_runtime(snapshot)
    return data, changed


def load_runtime_configuration(character_name: str | None = None) -> RuntimeState:
    ensure_map_table_runtime_synced()
    configure_equipment_catalog(DATABASE.list_equipment_templates())
    configure_shop_definitions(
        DATABASE.list_npc_shops(),
        DATABASE.list_shop_items(),
    )
    if STATE.login_authenticated:
        account_username = STATE.active_account_username
        target_name = character_name if character_name is not None else STATE.selected_character_name
        if target_name:
            character = DATABASE.load_runtime_character(account_username, target_name)
        else:
            character = DATABASE.first_runtime_character_for_account(account_username)
            if character is None:
                raise RuntimeError(f"No character for account {account_username!r}")
    else:
        character = DATABASE.first_runtime_character()
        if character is None:
            raise RuntimeError(f"No character found in database: {DATABASE.db_path}")
        account_username = str(character["account_username"])
    character = build_effective_character_view(character)
    STATE.character = character
    STATE.role_tokens, STATE.role_names = load_role_tokens_for_account(account_username)
    STATE.game_map_id = int(character["map_id"])
    # The 0x0002 string field is also used by the client UI as the visible map
    # label.  Keep it database-driven instead of hardcoding TWxxxxx strings.
    STATE.game_map_name = str(row_value(character, "map_display_name", row_value(character, "terrain_name", "")))
    STATE.game_bgm_index = clamped_int(row_value(character, "map_bgm_index", 8), 8, 0, 0xFFFF)
    STATE.game_spawn_x = int(character["position_x"])
    STATE.game_spawn_y = int(character["position_y"])
    STATE.packed_spawn = pack_grid_position(STATE.game_spawn_x, STATE.game_spawn_y)
    stats = runtime_character_stats(character)
    STATE.being_name = str(character["name"])
    STATE.being_string1 = str(stats["name"])
    STATE.being_string2 = str(stats["profession_label"])
    STATE.being_array2 = (
        player_base_type_for_gender(int(character["gender"])),
        int(character["body"]),
        int(character["hair"]),
        int(character["head"]),
        int(character["hand_r"]),
        int(character["hand_l"]),
        int(character["pants"]),
        int(character["foot_r"]),
        int(character["foot_l"]),
    )
    print(
        f"[runtime] Loaded character: account={account_username!r}, name={character['name']!r}, "
        f"map_id={STATE.game_map_id}, map_name={STATE.game_map_name!r}, bgm_index={STATE.game_bgm_index}, "
        f"spawn=({STATE.game_spawn_x}, {STATE.game_spawn_y}), scene_file={character['scene_file']!r}"
    )
    return STATE


def reload_runtime_character_for_game_login() -> RuntimeState:
    load_runtime_configuration()
    if STATE.character is None:
        raise RuntimeError("Runtime character was not loaded")
    DATABASE.update_character_last_login(int(STATE.character["id"]))
    configure_game_handlers(GameHandlerRuntime(
        database=DATABASE,
        character_id=int(STATE.character["id"]),
        map_id=int(STATE.character["map_id"]),
        character_name=str(STATE.character["name"]),
    ))
    return STATE


def validate_role_name(role_name: str) -> bool:
    return is_safe_character_name(role_name)
