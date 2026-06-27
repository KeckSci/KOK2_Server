from __future__ import annotations

import socket
import threading
import time

import config
import game_handlers
import login_handlers
from mob_combat import MobCombatRuntime
from reward_policy import calculate_monster_reward_awards
from side_message_protocol import (
    SideMessageConfigurationError,
    build_side_message_packet,
    render_configured_message,
)
from inventory_protocol import (
    CLIENT_OP_INVENTORY_MOVE,
    decode_inventory_move_request,
)
from crypto import (
    StatefulCryptoContext,
    build_inverse_tables,
    decode_initial_packet,
    decode_stateful_packet,
    encode_initial_packet_with_context,
    encode_stateful_packet,
    load_crypto_table,
    verify_known_stateful_vector,
)
from dispatcher import ConnectionControl, GAME_DISPATCHER, LOGIN_DISPATCHER, PacketContext
from login_handlers import LoginHandlerRuntime
from protocol import build_type1_plain, build_type2_plain, build_type4_plain, c_string, describe_plain_frame, extract_plain_frames
from shop_protocol import (
    CLIENT_OP_NPC_INTERACT,
    CLIENT_OP_SHOP_CATEGORY,
    CLIENT_OP_SHOP_TRANSACTION,
    OP_GAME_NPC_UI_TEXT,
    OP_GAME_SHOP_ITEM_LIST,
    BACKPACK_MAX_SLOT_COUNT,
    SHOP_ACTION_PURCHASE,
    SHOP_ACTION_RECYCLE,
    SHOP_ACTION_REPAIR,
    SHOP_ACTION_REPAIR_ALT,
    SHOP_BUY_RATE,
    SHOP_SELL_RATE,
    SHOP_FUNCTION_MASK,
    SHOP_TAIL_RESERVED,
    ShopDefinition,
    calculate_purchase_price,
    decode_ascii_c_string,
    decode_shop_category_request,
    decode_shop_transaction_request,
    find_shop_item_by_item_id,
    find_shop_for_npc,
    find_shop_for_npc_identity,
    parse_owned_instance_key,
    parse_npc_being_name,
    parse_attribute_field,
    is_equipment_slot,
)
from server_packets import (
    LOGIN_STAGE_TRANSITION,
    OP_GAME_BEING_INIT,
    OP_GAME_MAP_INIT,
    OP_GAME_PROFILE,
    OP_GAME_OWNED_ITEM_REMOVE,
    OP_GAME_PROPERTY_UPDATE,
    OP_GAME_ROLE_PROPERTY,
    OP_GAME_SKILL,
    PROPERTY_KEY_EXPERIENCE,
    PROPERTY_KEY_LEVEL,
    PROPERTY_KEY_PLAYER_GOLD,
    OP_LOGIN_SCENE_STATE,
    OP_LOGIN_SERVER_LIST,
    OP_LOGIN_STATUS,
    PROFILE_SEND_DELAY_SECONDS,
    MOVEMENT_SPEED_SEND_DELAY_SECONDS,
    PROPERTY_UI_INTER_RECORD_DELAY_SECONDS,
    PROPERTY_UI_SEND_DELAY_SECONDS,
    SKILL_INTER_RECORD_DELAY_SECONDS,
    SKILL_SEND_DELAY_SECONDS,
    build_being_init_payload,
    build_login_scene_state_payload,
    build_login_status_payload,
    build_map_init_payload,
    build_map_mob_being_payload,
    build_map_npc_being_payload,
    build_movement_speed_payload,
    build_profile_payload,
    build_property_update_payload,
    build_role_property_payload,
    build_server_list_payload,
    build_skill_payload,
    dump,
    iter_property_ui_records,
    load_skill_records_from_db,
    mob_array2_values,
    now,
)
from server_runtime import (
    DATABASE,
    STATE,
    bind_account_login_from_credentials,
    create_character_for_login,
    delete_character_for_login,
    load_runtime_configuration,
    prune_expired_temporary_stat_effects,
    refresh_runtime_character_stats,
    reload_runtime_character_for_game_login,
    reset_login_session,
    select_character_for_login,
)

HOST = config.HOST
LOGIN_PORT = config.LOGIN_PORT
GAME_PORT = config.GAME_PORT
SERVER_NAME = config.SERVER_NAME
ROLE_LIST_DELAY_SECONDS = config.ROLE_LIST_DELAY_SECONDS
ENTER_GAME_DELAY_SECONDS = config.ENTER_GAME_REDIRECT_DELAY_SECONDS
ENTER_GAME_CONNECT_INFO = f"{config.ENTER_GAME_HOST}#{config.ENTER_GAME_PORT}"
MAP_TO_PLAYER_DELAY_SECONDS = config.MAP_TO_PLAYER_DELAY_SECONDS
ENTITY_VISIBILITY_ENTER_RADIUS_TILES = float(
    config.ENTITY_VISIBILITY_ENTER_RADIUS_TILES
)
ENTITY_VISIBILITY_LEAVE_RADIUS_TILES = max(
    ENTITY_VISIBILITY_ENTER_RADIUS_TILES,
    float(config.ENTITY_VISIBILITY_LEAVE_RADIUS_TILES),
)
GAME_OPCODE_0005_TO_000A_DELAY_SECONDS = 0.10
# A short receive timeout lets the single connection thread perform
# authoritative HP/MP/SP regeneration without a second sender thread racing
# the stateful encryption context.  The recovery amount per tick always comes
# from the character attributes; combat only slows the tick cadence.
GAME_POST_REPLY_RECV_TIMEOUT_SECONDS = 0.25
RESOURCE_REGEN_IDLE_INTERVAL_SECONDS = 1.0
RESOURCE_REGEN_COMBAT_INTERVAL_SECONDS = 5.0

# Static monster creation follows the previously verified V19 path: finish the
# normal map/player/NPC/property bundle, wait briefly for the scene to settle,
# then send one type-1 opcode 0x0005 record per enabled map_mob_spawns row.
MAP_MOB_SPAWN_DELAY_SECONDS = 2.0
MAP_MOB_INTER_RECORD_DELAY_SECONDS = 0.05

# V17.47: send every enabled spawn whose client Mob model is explicitly
# verified in SQLite.  Templates without a real client_model_id are skipped
# rather than guessed or replaced with another creature.
MAP_MOB_SEND_VERIFIED_ONLY = True

# Combat timers can send 0x0032/0x0036/0x0004/0x0005/0x0007 while the
# main receive loop is also sending regeneration, inventory or shop packets.
# Stateful encryption must therefore be serialized per process/connection.
SERVER_SEND_LOCK = threading.RLock()

# Confirmed 0x000E property keys.  Only the protocol adapter knows these
# numbers; the stat engine itself is protocol-agnostic.
STAT_PROPERTY_KEYS = {
    "strength": 0x0010,
    # Client attribute order matches opcode 0x000A:
    # strength, dexterity, wisdom, constitution.  The earlier mapping had
    # dexterity/wisdom reversed, so upgrading dexterity also changed the
    # client's displayed wisdom value even though the database was correct.
    "dexterity": 0x0011,
    "wisdom": 0x0012,
    "constitution": 0x0013,
    "max_hp": 0x0017,
    "max_mp": 0x0019,
    "max_sp": 0x001B,
    "attack_power": 0x0065,
    "defense_power": 0x0066,
    "magic_attack_power": 0x0072,
    "earth_resistance": 0x0045,
    "water_resistance": 0x0046,
    "fire_resistance": 0x0047,
    "wind_resistance": 0x0048,
    "light_resistance": 0x0049,
    "dark_resistance": 0x004A,
}
CURRENT_RESOURCE_PROPERTY_KEYS = {
    "hp": 0x0016,
    "mp": 0x0018,
    "sp": 0x001A,
}


def lookup_npc_record_by_being_name(target_being_name: str):
    """Resolve NPCxxxxxx_NNN against the current per-map SQLite table."""
    map_id, npc_id = parse_npc_being_name(target_being_name)
    if map_id is None or npc_id is None:
        return None
    for row in DATABASE.list_map_npcs_for_map(map_id):
        if int(row["id"]) == npc_id:
            return row
    return None


def shop_window_title(
    target_being_name: str,
    shop: ShopDefinition,
    npc_record=None,
) -> str:
    """Use the current database NPC name/title for the shop window."""
    row = npc_record or lookup_npc_record_by_being_name(target_being_name)
    if row is None:
        return shop.default_title
    display_name = str(row["display_name"] or target_being_name)
    npc_title = str(row["title"] or "").strip()
    return (
        f"{display_name} / {npc_title}"
        if npc_title
        else display_name
    )


def current_viewer_profession() -> int | None:
    if STATE.character is None:
        return None
    try:
        return int(STATE.character["profession"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def send_npc_shop_open_response(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    request_payload: bytes,
) -> ShopDefinition | None:
    target_being_name = decode_ascii_c_string(request_payload)
    npc_record = lookup_npc_record_by_being_name(target_being_name)
    shop = find_shop_for_npc(target_being_name)
    if shop is None and npc_record is not None:
        map_id, _ = parse_npc_being_name(target_being_name)
        if map_id is not None:
            shop = find_shop_for_npc_identity(
                map_id,
                str(npc_record["display_name"] or ""),
            )
            if shop is not None:
                print(
                    f"[{now()}] Resolved shop by NPC identity fallback: "
                    f"target={target_being_name!r}, "
                    f"display_name={npc_record['display_name']!r}"
                )
    if shop is None:
        print(
            f"[{now()}] 0x8009 NPC has no configured shop: "
            f"target={target_being_name!r}"
        )
        return None

    title = shop_window_title(target_being_name, shop, npc_record)
    send_type1(
        conn,
        table,
        server_context,
        OP_GAME_NPC_UI_TEXT,
        shop.build_open_payload(title),
        f"game 0x0006 shop-open target={target_being_name} shop={shop.key}",
    )

    # Preserve the previously verified behavior: populate the default category
    # immediately, then resend it if the client emits 0x8018 after clicking the
    # category button.
    send_type2(
        conn,
        table,
        server_context,
        OP_GAME_SHOP_ITEM_LIST,
        shop.build_item_records(
            shop.category_id, viewer_profession=current_viewer_profession()
        ),
        f"game 0x000B shop-items type=2 shop={shop.key} category={shop.category_id}",
    )
    print(
        f"[{now()}] Shop opened: target={target_being_name!r}, "
        f"shop={shop.key!r}, category={shop.category_id}, "
        f"items={len(shop.items)}, "
        f"tail={SHOP_BUY_RATE},{SHOP_SELL_RATE},"
        f"{SHOP_FUNCTION_MASK},{SHOP_TAIL_RESERVED}"
    )
    return shop


def send_shop_category_response(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    active_shop: ShopDefinition | None,
    request_payload: bytes,
) -> None:
    action, category = decode_shop_category_request(request_payload)
    if active_shop is None:
        print(
            f"[{now()}] Ignoring 0x8018 without an active shop: "
            f"action={action}, category={category!r}"
        )
        return

    try:
        item_records = active_shop.build_item_records(
            category, viewer_profession=current_viewer_profession()
        )
    except ValueError as error:
        print(
            f"[{now()}] Rejecting 0x8018 shop category: "
            f"shop={active_shop.key!r}, action={action}, "
            f"category={category!r}, error={error}"
        )
        return

    if action != 4:
        print(
            f"[{now()}] Ignoring 0x8018 with unsupported action: "
            f"shop={active_shop.key!r}, action={action}, category={category!r}"
        )
        return

    send_type2(
        conn,
        table,
        server_context,
        OP_GAME_SHOP_ITEM_LIST,
        item_records,
        f"game 0x000B shop-items type=2 shop={active_shop.key} category={category}",
    )


def send_backpack_inventory_sync(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    label: str,
) -> None:
    """Rebuild all visible bag objects plus the active equipment set."""
    if STATE.character is None:
        raise RuntimeError("game character was not loaded")

    character_id = int(STATE.character["id"])
    item_rows = DATABASE.list_items_for_character(character_id)
    object_records: list[bytes] = []
    occupied: list[str] = []

    for row in item_rows:
        item_id = int(row["item_id"])
        item = find_shop_item_by_item_id(item_id)
        if item is None:
            print(f"[{now()}] Owned object skipped: unknown equip.id=0x{item_id:06X}")
            continue
        instance_id = int(row["id"])
        # Equipment instances are independent objects.  Original client
        # records use field2=0 (no stack badge / whole-object move) and
        # field5=1 (cannot merge into a stack).
        current_durability = int(row["current_durability"] or 0)
        instance_attrs: list[int] = []
        for index in range(1, 6):
            instance_attrs.extend(parse_attribute_field(row[f"instance_attr{index}"]))

        bag_slot_raw = row["bag_slot"]
        equipped_slot_raw = row["equipped_slot"]
        if bag_slot_raw is not None and equipped_slot_raw is None:
            bag_slot = int(bag_slot_raw)
            object_records.append(item.build_inventory_record(
                instance_id=instance_id,
                bag_slot=bag_slot,
                stack_count=0,
                current_durability=current_durability,
                instance_attrs=instance_attrs,
                viewer_profession=current_viewer_profession(),
            ))
            occupied.append(f"bag:{bag_slot}=inv{instance_id:08x}/0x{item_id:06X}")
            continue

        if bag_slot_raw is None and equipped_slot_raw is not None:
            if not bool(row["active_equipment"]):
                occupied.append(
                    f"hidden-set{int(row['weapon_set'] or 0)}:{int(equipped_slot_raw)}="
                    f"inv{instance_id:08x}/0x{item_id:06X}"
                )
                continue
            equipped_slot = int(equipped_slot_raw)
            try:
                record = item.build_equipment_record(
                    instance_id=instance_id,
                    equipped_slot=equipped_slot,
                    stack_count=0,
                    current_durability=current_durability,
                    instance_attrs=instance_attrs,
                    viewer_profession=current_viewer_profession(),
                )
            except ValueError as error:
                print(
                    f"[{now()}] Equipped object skipped: item_id=0x{item_id:06X}, "
                    f"slot={equipped_slot}, error={error}"
                )
                continue
            object_records.append(record)
            occupied.append(
                f"equip:{equipped_slot}/set{int(row['weapon_set'] or 0)}="
                f"inv{instance_id:08x}/0x{item_id:06X}"
            )
            continue

        print(
            f"[{now()}] Owned object skipped: invalid locations "
            f"id={instance_id}, bag={bag_slot_raw}, equip={equipped_slot_raw}"
        )

    if object_records:
        send_type2(
            conn, table, server_context, OP_GAME_SHOP_ITEM_LIST, object_records,
            f"{label} game 0x000B owned item instances type=2",
        )
    print(
        f"[{now()}] Owned-item sync complete: character_id={character_id}, "
        f"objects={len(object_records)}, locations={', '.join(occupied) or '<empty>'}; "
        "opcode 0x0049 shortcut binding intentionally omitted"
    )


def resend_active_shop_stock(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    active_shop: ShopDefinition,
    label: str,
) -> None:
    """Reassert the full server-authoritative shop catalogue after a buy.

    Client purchase handling optimistically removes the selected shop object.
    Shops are unlimited, so every purchase response republishes all template
    records.  Owned item instances use different field0 keys and cannot
    overwrite these templates.
    """
    send_type2(
        conn,
        table,
        server_context,
        OP_GAME_SHOP_ITEM_LIST,
        active_shop.build_item_records(
            active_shop.category_id, viewer_profession=current_viewer_profession()
        ),
        f"{label} game 0x000B unlimited shop restock type=2",
    )

def send_owned_item_remove(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    instance_id: int,
    label: str,
) -> None:
    """Remove one owned-item object from the live client inventory.

    Client opcode 0x000B only creates or updates item objects.  Omitting an
    object from a later 0x000B list does not delete the old client object.
    Opcode 0x000C is the confirmed object-removal path and carries one or
    more owned resource keys as C-string records.
    """
    resource_key = f"inv{int(instance_id):08x}"
    send_type2(
        conn,
        table,
        server_context,
        OP_GAME_OWNED_ITEM_REMOVE,
        [c_string(resource_key)],
        f"{label} game 0x000C remove owned item {resource_key}",
    )


def send_character_gold_sync(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    label: str,
) -> None:
    """Reload and send only currency/profile state; never recalculate stats."""
    if STATE.character is None:
        raise RuntimeError("game character was not loaded")
    raw = DATABASE.load_runtime_character(
        str(STATE.character.get("account_username", "")),
        str(STATE.character["name"]),
    )
    for field_name in ("player_gold", "depot_gold", "experience", "level"):
        STATE.character[field_name] = int(raw[field_name])
    send_type1(
        conn,
        table,
        server_context,
        OP_GAME_PROPERTY_UPDATE,
        build_property_update_payload(0x0015, int(STATE.character["player_gold"])),
        f"{label} game 0x000E player_gold={int(STATE.character['player_gold'])}",
    )
    send_type1(
        conn,
        table,
        server_context,
        OP_GAME_PROFILE,
        build_profile_payload(STATE.character),
        f"{label} game 0x001C profile gold refresh",
    )


def handle_shop_transaction_request(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    active_shop: ShopDefinition | None,
    request_payload: bytes,
) -> None:
    try:
        request = decode_shop_transaction_request(request_payload)
    except (ValueError, UnicodeDecodeError) as error:
        print(f"[{now()}] Rejecting malformed 0x8019 shop transaction: {error}")
        return

    if active_shop is None:
        print(
            f"[{now()}] Rejecting 0x8019 without active shop: "
            f"action={request.action}, item={request.resource_key!r}"
        )
        return
    if STATE.character is None:
        raise RuntimeError("game character was not loaded")

    character_id = int(STATE.character["id"])
    instance_id = parse_owned_instance_key(request.resource_key)
    client_fields = (
        f"client_u16=({request.client_value1},{request.client_value2})"
    )

    if request.action == SHOP_ACTION_PURCHASE:
        item = active_shop.find_item(request.resource_key)
        if item is None or instance_id is not None:
            print(
                f"[{now()}] Rejecting unknown purchase item: "
                f"shop={active_shop.key!r}, item={request.resource_key!r}, "
                f"{client_fields}"
            )
            send_character_gold_sync(
                conn, table, server_context, "shop purchase rejected"
            )
            send_backpack_inventory_sync(
                conn, table, server_context, "shop purchase rejected"
            )
            resend_active_shop_stock(
                conn, table, server_context, active_shop,
                "shop purchase rejected",
            )
            return

        unit_price = calculate_purchase_price(item.price)
        ok, reason, result = DATABASE.purchase_shop_item(
            character_id=character_id,
            item_id=item.item_id,
            unit_price=unit_price,
            quantity=1,
            client_slot_limit=BACKPACK_MAX_SLOT_COUNT,
            durability=item.durability,
        )
        label = "shop purchase success" if ok else "shop purchase rejected"
        print(
            f"[{now()}] Shop purchase {'committed' if ok else 'rejected'}: "
            f"character_id={character_id}, shop={active_shop.key!r}, "
            f"item={item.name!r}, resource_key={item.resource_key!r}, "
            f"base_price={item.price}, buy_rate={SHOP_BUY_RATE}, "
            f"unit_price={unit_price}, {client_fields}, "
            f"reason={reason}, result={result}"
        )
        send_character_gold_sync(conn, table, server_context, label)
        send_backpack_inventory_sync(conn, table, server_context, label)
        resend_active_shop_stock(conn, table, server_context, active_shop, label)
        return

    if request.action == SHOP_ACTION_RECYCLE:
        if instance_id is None:
            print(
                f"[{now()}] Rejecting recycle without owned instance key: "
                f"shop={active_shop.key!r}, item={request.resource_key!r}, "
                f"{client_fields}"
            )
            send_character_gold_sync(
                conn, table, server_context, "shop recycle rejected"
            )
            send_backpack_inventory_sync(
                conn, table, server_context, "shop recycle rejected"
            )
            return
        ok, reason, result = DATABASE.recycle_shop_item(
            character_id=character_id,
            instance_id=instance_id,
            recycle_rate=SHOP_SELL_RATE,
        )
        label = "shop recycle success" if ok else "shop recycle rejected"
        print(
            f"[{now()}] Shop recycle {'committed' if ok else 'rejected'}: "
            f"character_id={character_id}, shop={active_shop.key!r}, "
            f"instance=inv{instance_id:08x}, sell_rate={SHOP_SELL_RATE}, "
            f"{client_fields}, reason={reason}, result={result}"
        )
        if ok:
            # Every successfully recycled instance needs an explicit 0x000C
            # removal.  A full 0x000B inventory re-sync is additive and does
            # not clear stale client objects that are absent from the list.
            send_owned_item_remove(
                conn, table, server_context, instance_id, label
            )
        send_character_gold_sync(conn, table, server_context, label)
        send_backpack_inventory_sync(conn, table, server_context, label)
        return

    if request.action in (SHOP_ACTION_REPAIR, SHOP_ACTION_REPAIR_ALT):
        if instance_id is None:
            print(
                f"[{now()}] Rejecting repair without owned instance key: "
                f"shop={active_shop.key!r}, item={request.resource_key!r}, "
                f"action={request.action}, {client_fields}"
            )
            send_character_gold_sync(
                conn, table, server_context, "shop repair rejected"
            )
            send_backpack_inventory_sync(
                conn, table, server_context, "shop repair rejected"
            )
            return
        ok, reason, result = DATABASE.repair_shop_item(
            character_id=character_id,
            instance_id=instance_id,
            purchase_rate=SHOP_BUY_RATE,
        )
        label = "shop repair success" if ok else "shop repair rejected"
        print(
            f"[{now()}] Shop repair {'committed' if ok else 'rejected'}: "
            f"character_id={character_id}, shop={active_shop.key!r}, "
            f"instance=inv{instance_id:08x}, action={request.action}, "
            f"buy_rate={SHOP_BUY_RATE}, {client_fields}, "
            f"reason={reason}, result={result}"
        )
        send_character_gold_sync(conn, table, server_context, label)
        send_backpack_inventory_sync(conn, table, server_context, label)
        return

    print(
        f"[{now()}] Rejecting unsupported 0x8019 action: "
        f"shop={active_shop.key!r}, action={request.action}, "
        f"item={request.resource_key!r}, {client_fields}"
    )


def send_equipment_stat_sync(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    label: str,
) -> tuple[str, ...]:
    """Recalculate once because the equipment source actually changed."""
    character, changed = refresh_runtime_character_stats()
    send_changed_stat_properties(conn, table, server_context, changed, label)
    if STATE.resource_clamps:
        send_current_resource_properties(
            conn, table, server_context, STATE.resource_clamps,
            f"{label} resource clamp",
        )
    print(
        f"[{now()}] equipment stat source changed: "
        f"character_id={character['id']}, changed={','.join(changed) or '<none>'}"
    )
    return changed


def handle_inventory_move_request(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    payload: bytes,
    combat_runtime: MobCombatRuntime | None = None,
) -> None:
    """Persist confirmed 0x800B bag moves, equips, and basic unequips."""
    if STATE.character is None:
        print(f"[{now()}] Ignoring 0x800B: no active character")
        return

    try:
        request = decode_inventory_move_request(payload)
    except ValueError as error:
        print(
            f"[{now()}] Invalid 0x800B inventory move payload: "
            f"error={error}, payload={payload.hex(' ')}"
        )
        send_backpack_inventory_sync(
            conn, table, server_context, "invalid 0x800B inventory move"
        )
        return

    # The third 0x800B field is the quantity copied from item+0x174.
    # For independent non-stackable equipment the canonical value is 0.
    # Value 1 is accepted for compatibility with older development records;
    # larger values would imply a stack/split operation that this equipment
    # transaction path does not implement.
    if request.client_quantity not in (0, 1):
        print(
            f"[{now()}] Unsupported 0x800B move quantity: "
            f"source={request.source_slot}, target={request.target_slot}, "
            f"client_quantity={request.client_quantity}"
        )
        send_backpack_inventory_sync(
            conn, table, server_context, "unsupported 0x800B move quantity"
        )
        return

    character_id = int(STATE.character["id"])
    source_is_equipment = is_equipment_slot(request.source_slot)
    target_is_equipment = is_equipment_slot(request.target_slot)

    if not source_is_equipment and target_is_equipment:
        source_row = DATABASE.get_backpack_item_at_slot(
            character_id, request.source_slot
        )
        if source_row is None:
            ok, reason, result = False, "source-empty", None
        else:
            item_id = int(source_row["item_id"])
            item = find_shop_item_by_item_id(item_id)
            if item is None:
                ok, reason, result = False, "unknown-item-template", {
                    "item_id": item_id
                }
            else:
                allowed_slots = item.allowed_equipment_slots
                if request.target_slot not in allowed_slots:
                    ok, reason, result = False, "wrong-equipment-slot", {
                        "item_id": item_id,
                        "mfpart": item.equipment_part,
                        "allowed_slots": sorted(allowed_slots),
                        "requested_slot": request.target_slot,
                    }
                else:
                    requirement_failures = item.requirement_failures(STATE.character)
                    if requirement_failures:
                        ok, reason, result = False, "requirements-not-met", {
                            "item_id": item_id,
                            "item_name": item.name,
                            "failures": list(requirement_failures),
                        }
                    else:
                        ok, reason, result = DATABASE.equip_character_item(
                            character_id=character_id,
                            source_bag_slot=request.source_slot,
                            target_equipped_slot=request.target_slot,
                            expected_item_id=item_id,
                            client_slot_limit=BACKPACK_MAX_SLOT_COUNT,
                        )
        operation = "equipment"
    elif source_is_equipment and not target_is_equipment:
        ok, reason, result = DATABASE.unequip_character_item(
            character_id=character_id,
            source_equipped_slot=request.source_slot,
            target_bag_slot=request.target_slot,
            client_slot_limit=BACKPACK_MAX_SLOT_COUNT,
        )
        operation = "unequip"
    elif not source_is_equipment and not target_is_equipment:
        ok, reason, result = DATABASE.move_character_item(
            character_id=character_id,
            source_slot=request.source_slot,
            target_slot=request.target_slot,
            client_slot_limit=BACKPACK_MAX_SLOT_COUNT,
        )
        operation = "backpack"
    else:
        ok, reason, result = False, "equipment-to-equipment-not-supported", {
            "source_slot": request.source_slot,
            "target_slot": request.target_slot,
        }
        operation = "equipment"

    print(
        f"[{now()}] Inventory {operation} {'committed' if ok else 'rejected'}: "
        f"character_id={character_id}, source={request.source_slot}, "
        f"target={request.target_slot}, client_quantity={request.client_quantity}, "
        f"reason={reason}, result={result}"
    )

    # Re-emit every owned object with the same stable instance key.  A bag
    # item uses field1=bag_slot; an equipped item uses field1=200..205.
    # This is the authoritative acknowledgement for both arrays.
    send_backpack_inventory_sync(
        conn,
        table,
        server_context,
        f"inventory {operation} success" if ok else f"inventory {operation} rejected",
    )
    if ok and operation in {"equipment", "unequip"}:
        changed = send_equipment_stat_sync(
            conn,
            table,
            server_context,
            f"inventory {operation} success",
        )
        if (
            combat_runtime is not None
            and "normal_attack_interval_ms" in changed
            and STATE.character is not None
        ):
            combat_runtime.set_player_attack_interval_ms(
                int(STATE.character["normal_attack_interval_ms"])
            )



def send_initial_plain(conn: socket.socket, table: bytes, plain: bytes, label: str) -> StatefulCryptoContext:
    encrypted, key1, key2, context = encode_initial_packet_with_context(plain=plain, table=table)
    dump(f"{label} plain", plain)
    dump(f"{label} encrypted", encrypted)
    print(f"[{now()}] {label} initial keys: key1=0x{key1:02X}, key2=0x{key2:02X}, row=0x{context.table_row:02X}, state=0x{context.state:02X}")
    conn.sendall(encrypted)
    return context


def send_stateful_plain(conn: socket.socket, table: bytes, context: StatefulCryptoContext, plain: bytes, label: str) -> None:
    with SERVER_SEND_LOCK:
        state_before = context.state
        encrypted = encode_stateful_packet(plain=plain, table=table, context=context)
        dump(f"{label} plain", plain)
        dump(f"{label} encrypted", encrypted)
        print(f"[{now()}] {label} stateful send: state_before=0x{state_before:02X}, state_after=0x{context.state:02X}, row=0x{context.table_row:02X}")
        conn.sendall(encrypted)


def send_type1(conn: socket.socket, table: bytes, context: StatefulCryptoContext, opcode: int, payload: bytes, label: str) -> None:
    send_stateful_plain(conn, table, context, build_type1_plain(opcode, payload), label)


def send_configured_side_message(
    conn: socket.socket,
    table: bytes,
    context: StatefulCryptoContext,
    message_key: str,
    label: str,
    **values: object,
) -> bool:
    """Render and send one display-only message from config.py.

    Only client formatting paths supported by ``side_message_protocol`` are
    accepted.  Configuration errors are logged and do not affect gameplay
    state or reward settlement.
    """
    if not bool(config.ENABLE_SERVER_SIDE_MESSAGES):
        return False
    try:
        enabled, style, message = render_configured_message(
            config.SERVER_SIDE_MESSAGES,
            message_key,
            **values,
        )
        if not enabled:
            return False
        packet = build_side_message_packet(style, message)
    except SideMessageConfigurationError as error:
        print(
            f"[{now()}] side-message configuration rejected: "
            f"key={message_key!r}, error={error}"
        )
        return False

    send_type1(
        conn,
        table,
        context,
        packet.opcode,
        packet.payload,
        (
            f"{label} key={message_key} style={packet.style} "
            f"color={packet.color_hex}"
        ),
    )
    return True

def send_type2(conn: socket.socket, table: bytes, context: StatefulCryptoContext, opcode: int, records: list[bytes], label: str) -> None:
    # Opcode 0x000B is a confirmed type-2 multi-record frame.
    # Each item must retain its own descriptor
    # boundary; concatenating the records into a type-1 payload is invalid.
    send_stateful_plain(conn, table, context, build_type2_plain(opcode, records), label)


def send_changed_stat_properties(
    conn: socket.socket,
    table: bytes,
    context: StatefulCryptoContext,
    changed_keys: tuple[str, ...] | list[str] | set[str],
    label: str,
) -> None:
    """Send only effective stats that changed and have confirmed client keys."""
    if STATE.character is None:
        return
    changed = set(changed_keys)
    for stat_name, property_key in STAT_PROPERTY_KEYS.items():
        if stat_name not in changed:
            continue
        value = int(STATE.character[stat_name])
        send_type1(
            conn,
            table,
            context,
            OP_GAME_PROPERTY_UPDATE,
            build_property_update_payload(property_key, value),
            f"{label} game 0x000E {stat_name}={value}",
        )
    unsupported = sorted(
        key for key in changed
        if key not in STAT_PROPERTY_KEYS
        and key not in {"normal_attack_interval_ms", "hp_regen_per_second", "mp_regen_per_second", "sp_regen_per_second"}
    )
    if unsupported:
        print(f"[{now()}] changed stats without a protocol mapping: {unsupported}")


def send_current_resource_properties(
    conn: socket.socket,
    table: bytes,
    context: StatefulCryptoContext,
    resource_names: tuple[str, ...] | list[str] | set[str],
    label: str,
) -> None:
    if STATE.character is None:
        return
    for resource_name in ("hp", "mp", "sp"):
        if resource_name not in resource_names:
            continue
        value = int(STATE.character[resource_name])
        send_type1(
            conn,
            table,
            context,
            OP_GAME_PROPERTY_UPDATE,
            build_property_update_payload(
                CURRENT_RESOURCE_PROPERTY_KEYS[resource_name], value
            ),
            f"{label} game 0x000E {resource_name}={value}",
        )


def send_type4(conn: socket.socket, table: bytes, context: StatefulCryptoContext, opcode: int, payload: bytes, label: str) -> None:
    send_stateful_plain(conn, table, context, build_type4_plain(opcode, payload), label)


def send_server_list_initial(conn: socket.socket, table: bytes) -> StatefulCryptoContext:
    payload = build_server_list_payload(SERVER_NAME)
    plain = build_type1_plain(OP_LOGIN_SERVER_LIST, payload)
    return send_initial_plain(conn, table, plain, "login 0x7009 server-list")


def send_login_reject_initial(conn: socket.socket, table: bytes) -> StatefulCryptoContext:
    payload = build_login_status_payload(login_handlers.LOGIN_STATUS_ACCOUNT_PASSWORD_ERROR)
    plain = build_type1_plain(OP_LOGIN_STATUS, payload)
    return send_initial_plain(conn, table, plain, "login 0x7000 reject")


def send_login_stage_transition(conn: socket.socket, table: bytes, context: StatefulCryptoContext) -> None:
    send_type4(conn, table, context, OP_LOGIN_SCENE_STATE, build_login_scene_state_payload(), "login-stage 0x0002 scene-state")
    for delay, opcode, payload_builder in LOGIN_STAGE_TRANSITION:
        if delay > 0:
            time.sleep(delay)
        send_type4(conn, table, context, opcode, payload_builder(), f"login-stage 0x{opcode:04X}")


def configure_login_runtime(conn: socket.socket, table: bytes, context: StatefulCryptoContext, authenticated: bool, account_username: str, role_tokens: list[str], role_names: set[str]) -> None:
    def send_runtime_type4(opcode: int, data: bytes, label: str) -> None:
        send_type4(conn, table, context, opcode, data, label)

    def send_runtime_raw(plain: bytes, label: str) -> None:
        send_stateful_plain(conn, table, context, plain, label)

    def send_runtime_server_list() -> None:
        send_runtime_raw(build_type1_plain(OP_LOGIN_SERVER_LIST, build_server_list_payload(SERVER_NAME)), "login 0x7009 server-list stateful")

    login_handlers.configure_login_handlers(LoginHandlerRuntime(
        send_type4=send_runtime_type4,
        send_raw_stateful=send_runtime_raw,
        bind_account_login=bind_account_login_from_credentials,
        role_tokens=role_tokens,
        role_names=role_names,
        role_list_delay_seconds=ROLE_LIST_DELAY_SECONDS,
        send_server_list=send_runtime_server_list,
        select_role=select_character_for_login,
        create_role=create_character_for_login,
        delete_role=delete_character_for_login,
        authenticated=authenticated,
        account_username=account_username,
        enter_game_delay_seconds=ENTER_GAME_DELAY_SECONDS,
        enter_game_status=0,
        enter_game_ticket=b"\x00",
        enter_game_connect_info=ENTER_GAME_CONNECT_INFO,
    ))


def decode_initial_frame(packet: bytes, inverse_tables: list[list[int]]) -> tuple[StatefulCryptoContext, int, int, bytes, bytes]:
    key1, key2, plain, final_state = decode_initial_packet(packet=packet, inverse_tables=inverse_tables)
    context = StatefulCryptoContext(state=final_state, table_row=key1 % 10)
    stream = bytearray(plain)
    frames = extract_plain_frames(stream)
    if len(frames) != 1 or stream:
        raise ValueError(f"initial packet is not exactly one frame: frames={len(frames)}, remaining={len(stream)}")
    frame_type, opcode, payload = describe_plain_frame(frames[0], 0)
    print(f"[{now()}] initial keys: key1=0x{key1:02X}, key2=0x{key2:02X}, row=0x{context.table_row:02X}, state=0x{context.state:02X}")
    return context, frame_type, opcode, payload, plain


def handle_login_client(conn: socket.socket, addr: tuple[str, int], table: bytes, inverse_tables: list[list[int]]) -> None:
    print("=" * 80)
    print(f"[{now()}] Login client connected: {addr}")
    conn.settimeout(None)
    reset_login_session()
    login_control = ConnectionControl()
    try:
        initial_packet = conn.recv(4096)
        if not initial_packet:
            print(f"[{now()}] Login client sent no initial data")
            return
        dump("login initial encrypted", initial_packet)
        try:
            client_context, frame_type, opcode, payload, plain = decode_initial_frame(initial_packet, inverse_tables)
        except Exception as error:
            print(f"[{now()}] Initial login decode failed: {type(error).__name__}: {error}")
            return
        dump("login initial plain", plain)
        if frame_type != 1 or opcode != 0x9000:
            send_login_reject_initial(conn, table)
            return
        ok, account, role_tokens, role_names, selected_candidate, _ = login_handlers.bind_account_login_from_payload(payload, bind_account_login_from_credentials)
        print(f"[{now()}] Initial account bind: ok={ok}, account={account!r}, selected={selected_candidate[0] if selected_candidate else None!r}, role_count={len(role_tokens)}")
        if not ok:
            send_login_reject_initial(conn, table)
            return
        server_context = send_server_list_initial(conn, table)
        configure_login_runtime(conn, table, server_context, ok, account, role_tokens, role_names)
        stream = bytearray()
        transition_sent = False
        packet_number = 0
        while True:
            ciphertext = conn.recv(4096)
            if not ciphertext:
                print(f"[{now()}] Login client disconnected")
                return
            packet_number += 1
            dump(f"login follow-up encrypted #{packet_number}", ciphertext)
            state_before = client_context.state
            plain = decode_stateful_packet(ciphertext=ciphertext, inverse_tables=inverse_tables, context=client_context)
            print(f"[{now()}] login follow-up decode #{packet_number}: state_before=0x{state_before:02X}, state_after=0x{client_context.state:02X}, row=0x{client_context.table_row:02X}")
            dump(f"login follow-up plain #{packet_number}", plain)
            stream.extend(plain)
            for frame in extract_plain_frames(stream):
                frame_type, opcode, payload = describe_plain_frame(frame, packet_number)
                LOGIN_DISPATCHER.dispatch(PacketContext("login", frame_type, opcode, payload, addr, login_control))
                if login_control.close_requested:
                    print(f"[{now()}] Login close requested: {login_control.close_reason}")
                    return
                if frame_type == 1 and opcode == 0x9006 and payload == b"\x00\x01" and not transition_sent:
                    send_login_stage_transition(conn, table, server_context)
                    transition_sent = True
    except ConnectionResetError:
        print(f"[{now()}] Login connection reset by client")
    except BrokenPipeError:
        print(f"[{now()}] Login broken pipe")
    except KeyboardInterrupt:
        raise
    except Exception as error:
        print(f"[{now()}] Login connection error: {type(error).__name__}: {error}")
    finally:
        try:
            conn.close()
        except OSError:
            pass
        print(f"[{now()}] Login connection closed")


def _record_within_radius(
    record,
    player_x: int,
    player_y: int,
    radius: float,
) -> bool:
    dx = int(record["position_x"]) - int(player_x)
    dy = int(record["position_y"]) - int(player_y)
    return dx * dx + dy * dy <= float(radius) * float(radius)


def _npc_being_name(record, map_id: int) -> str:
    return f"NPC{int(map_id)}_{int(record['id']):03d}"


def send_map_npcs(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    map_id: int,
    *,
    player_x: int,
    player_y: int,
    visibility_radius: float = ENTITY_VISIBILITY_ENTER_RADIUS_TILES,
) -> tuple[list[dict[str, object]], set[str]]:
    records = [dict(row) for row in DATABASE.list_map_npcs_for_map(int(map_id))]
    if not records:
        print(f"[{now()}] Loaded 0 map NPC record(s): map_id={int(map_id)}")
        return [], set()
    print(f"[{now()}] Loaded {len(records)} map NPC record(s) from SQLite map_npcs_{int(map_id)}")
    visible_records = [
        record
        for record in records
        if _record_within_radius(
            record, int(player_x), int(player_y), float(visibility_radius)
        )
    ]
    visible_names: set[str] = set()
    print(
        f"[{now()}] NPC visibility initial set: "
        f"player=({int(player_x)},{int(player_y)}), "
        f"radius={float(visibility_radius):.1f}, "
        f"send={len(visible_records)}/{len(records)}"
    )
    for index, record in enumerate(visible_records, 1):
        being_name = _npc_being_name(record, int(map_id))
        visible_names.add(being_name)
        label = f"game 0x0005 map-npc {index}/{len(records)} {record['display_name']}"
        payload = build_map_npc_being_payload(record, int(map_id))
        configured_shop = find_shop_for_npc_identity(
            int(map_id),
            str(record["display_name"] or ""),
        )
        if configured_shop is not None:
            print(
                f"[{now()}] Shop NPC spawn check: "
                f"map={int(map_id)}, id={int(record['id'])}, "
                f"name={record['display_name']!r}, "
                f"field0c={int(record['field0c'])}, "
                f"shop={configured_shop.key!r}"
            )
        send_type1(conn, table, server_context, OP_GAME_BEING_INIT, payload, label)
        time.sleep(0.05)
    return records, visible_names


def sync_map_npc_visibility(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    *,
    map_id: int,
    records: list[dict[str, object]],
    visible_names: set[str],
    player_x: int,
    player_y: int,
    enter_radius: float = ENTITY_VISIBILITY_ENTER_RADIUS_TILES,
    leave_radius: float = ENTITY_VISIBILITY_LEAVE_RADIUS_TILES,
) -> tuple[int, int]:
    to_show: list[dict[str, object]] = []
    to_hide: list[str] = []
    for record in records:
        being_name = _npc_being_name(record, int(map_id))
        is_visible = being_name in visible_names
        radius = float(leave_radius if is_visible else enter_radius)
        should_be_visible = _record_within_radius(
            record, int(player_x), int(player_y), radius
        )
        if should_be_visible and not is_visible:
            to_show.append(record)
        elif is_visible and not should_be_visible:
            to_hide.append(being_name)

    for being_name in to_hide:
        send_type1(
            conn,
            table,
            server_context,
            0x0007,
            c_string(being_name),
            f"game 0x0007 npc-leave-view {being_name!r}",
        )
        visible_names.discard(being_name)
    for record in to_show:
        being_name = _npc_being_name(record, int(map_id))
        send_type1(
            conn,
            table,
            server_context,
            OP_GAME_BEING_INIT,
            build_map_npc_being_payload(record, int(map_id)),
            f"game 0x0005 npc-enter-view {being_name!r} {record['display_name']!r}",
        )
        visible_names.add(being_name)

    if to_show or to_hide:
        print(
            f"[{now()}] NPC visibility update: "
            f"player=({int(player_x)},{int(player_y)}), "
            f"enter={float(enter_radius):.1f}, leave={float(leave_radius):.1f}, "
            f"show={len(to_show)}, hide={len(to_hide)}, "
            f"visible={len(visible_names)}"
        )
    return len(to_show), len(to_hide)


def send_map_mobs(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    map_id: int,
    delay_before: float = 0.0,
    *,
    player_x: int,
    player_y: int,
    visibility_radius: float = ENTITY_VISIBILITY_ENTER_RADIUS_TILES,
) -> tuple[list[dict[str, object]], set[str]]:
    records = DATABASE.list_map_mob_spawns(int(map_id))
    if not records:
        print(f"[{now()}] Loaded 0 enabled map monster spawn(s): map_id={int(map_id)}")
        return [], set()

    print(
        f"[{now()}] Loaded {len(records)} enabled map monster spawn(s) "
        f"from SQLite map_mob_spawns for map_id={int(map_id)}"
    )
    if delay_before > 0:
        print(
            f"[{now()}] Delaying map monster 0x0005 creation by "
            f"{float(delay_before):.2f}s after normal map initialization"
        )
        time.sleep(float(delay_before))

    selected_records: list[tuple[dict[str, object], tuple[int, ...]]] = []
    skipped_by_template: dict[tuple[str, str], int] = {}

    for record in records:
        try:
            array2 = mob_array2_values(record)
            model_id = str(record.get("client_model_id", "") or "").strip()
            if not model_id:
                raise ValueError("client_model_id is empty")
        except (TypeError, ValueError) as error:
            key = (
                str(record.get("template_key", "") or ""),
                str(record.get("display_name", "") or ""),
            )
            skipped_by_template[key] = skipped_by_template.get(key, 0) + 1
            print(
                f"[{now()}] Skip unverified map monster spawn: "
                f"spawn_id={int(record['spawn_id'])}, "
                f"name={record.get('display_name', '')!r}, "
                f"client_model_id={record.get('client_model_id', '')!r}, "
                f"reason={error}"
            )
            continue
        selected_records.append((record, array2))

    if not selected_records:
        print(
            f"[{now()}] sent 0/{len(records)} map monster(s): "
            "no enabled spawn has a verified client Mob model"
        )
        return [], set()

    visible_records = [
        (record, array2)
        for record, array2 in selected_records
        if _record_within_radius(
            record, int(player_x), int(player_y), float(visibility_radius)
        )
    ]
    visible_names: set[str] = {
        str(record["internal_name"])
        for record, _ in visible_records
    }

    print(
        f"[{now()}] ALL VERIFIED MOBS: registered "
        f"{len(selected_records)}/{len(records)} record(s), "
        f"initially sending {len(visible_records)}, "
        f"player=({int(player_x)},{int(player_y)}), "
        f"visibility_radius={float(visibility_radius):.1f}, "
        f"verified_only={MAP_MOB_SEND_VERIFIED_ONLY}, "
        f"network_suffix=True, template_behavior_from_database=True"
    )
    if skipped_by_template:
        skipped_summary = ", ".join(
            f"{display_name or template_key}:{count}"
            for (template_key, display_name), count in sorted(skipped_by_template.items())
        )
        print(f"[{now()}] Unverified templates skipped: {skipped_summary}")

    for index, (record, array2) in enumerate(visible_records, 1):
        model_id = str(record["client_model_id"])
        label = (
            f"game 0x0005 map-mob {index}/{len(visible_records)} "
            f"spawn_id={int(record['spawn_id'])} "
            f"being={record['internal_name']!r} "
            f"name={record['display_name']!r} "
            f"client_model_id={model_id!r} "
            f"resource=mob_{model_id} "
            f"pos=({int(record['position_x'])},{int(record['position_y'])}) "
            f"field0c={int(record['being_field0c'])} "
            f"field2c={int(record['being_field2c'])} "
            f"array2={array2}"
        )
        payload = build_map_mob_being_payload(record, int(map_id))
        send_type1(
            conn,
            table,
            server_context,
            OP_GAME_BEING_INIT,
            payload,
            label,
        )
        if index < len(visible_records):
            time.sleep(MAP_MOB_INTER_RECORD_DELAY_SECONDS)

    return [dict(record) for record, _ in selected_records], visible_names


def send_game_initialization(
    conn: socket.socket,
    table: bytes,
) -> tuple[
    StatefulCryptoContext,
    list[dict[str, object]],
    set[str],
    list[dict[str, object]],
    set[str],
]:
    state = reload_runtime_character_for_game_login()
    payload_0002 = build_map_init_payload(state)
    plain_0002 = build_type1_plain(OP_GAME_MAP_INIT, payload_0002)
    encrypted_0002, key1, key2, server_context = encode_initial_packet_with_context(plain=plain_0002, table=table)
    dump("game 0x0002 map-init payload", payload_0002)
    dump("game 0x0002 map-init encrypted", encrypted_0002)
    print(f"[{now()}] game 0x0002 fields: map_id={state.game_map_id}, map_name={state.game_map_name!r}, bgm_index={state.game_bgm_index}, spawn=0x{state.packed_spawn:08X}")
    conn.sendall(encrypted_0002)
    time.sleep(MAP_TO_PLAYER_DELAY_SECONDS)
    send_type1(conn, table, server_context, OP_GAME_BEING_INIT, build_being_init_payload(state), "game 0x0005 player-being-init")
    npc_records, visible_npc_names = send_map_npcs(
        conn,
        table,
        server_context,
        int(state.game_map_id),
        player_x=int(state.game_spawn_x),
        player_y=int(state.game_spawn_y),
    )
    # After NPC creation, re-send the player being with the same unique name.  The
    # client uses the most recent 0x0005 string for some hero UI labels; this
    # keeps the upper-left role name on the player instead of the last NPC.
    send_type1(conn, table, server_context, OP_GAME_BEING_INIT, build_being_init_payload(state), "game 0x0005 player-being-reassert")
    time.sleep(GAME_OPCODE_0005_TO_000A_DELAY_SECONDS)
    if state.character is None:
        raise RuntimeError("game character was not loaded")
    send_type1(conn, table, server_context, OP_GAME_ROLE_PROPERTY, build_role_property_payload(state.character), "game 0x000A role-property")
    if MOVEMENT_SPEED_SEND_DELAY_SECONDS > 0:
        time.sleep(MOVEMENT_SPEED_SEND_DELAY_SECONDS)
    send_type1(conn, table, server_context, OP_GAME_PROPERTY_UPDATE, build_movement_speed_payload(), "game 0x000E movement-speed")
    mob_records, visible_mob_names = send_map_mobs(
        conn, table, server_context, int(state.game_map_id),
        MAP_MOB_SPAWN_DELAY_SECONDS,
        player_x=int(state.game_spawn_x),
        player_y=int(state.game_spawn_y),
    )
    return (
        server_context,
        mob_records,
        visible_mob_names,
        npc_records,
        visible_npc_names,
    )



def send_game_teleport_bundle(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    teleport_row,
) -> tuple[
    list[dict[str, object]],
    set[str],
    list[dict[str, object]],
    set[str],
]:
    """Send a database-driven map transition without reconnecting the client."""
    state = load_runtime_configuration()
    payload_0002 = build_map_init_payload(state)
    print(
        f"[{now()}] game teleport bundle: "
        f"trigger=({int(teleport_row['source_map_id'])}, {int(teleport_row['trigger_x'])}, {int(teleport_row['trigger_y'])}) -> "
        f"target=({state.game_map_id}, {state.game_spawn_x}, {state.game_spawn_y}), "
        f"map_name={state.game_map_name!r}, bgm_index={state.game_bgm_index}, "
        f"note={str(teleport_row['note'])!r}"
    )
    send_type1(
        conn,
        table,
        server_context,
        OP_GAME_MAP_INIT,
        payload_0002,
        "game 0x0002 teleport map-init",
    )
    time.sleep(MAP_TO_PLAYER_DELAY_SECONDS)
    send_type1(
        conn,
        table,
        server_context,
        OP_GAME_BEING_INIT,
        build_being_init_payload(state),
        "game 0x0005 teleport player-being-init",
    )
    npc_records, visible_npc_names = send_map_npcs(
        conn,
        table,
        server_context,
        int(state.game_map_id),
        player_x=int(state.game_spawn_x),
        player_y=int(state.game_spawn_y),
    )
    send_type1(
        conn,
        table,
        server_context,
        OP_GAME_BEING_INIT,
        build_being_init_payload(state),
        "game 0x0005 teleport player-being-reassert",
    )
    if state.character is None:
        raise RuntimeError("teleport character was not loaded")
    send_type1(
        conn,
        table,
        server_context,
        OP_GAME_ROLE_PROPERTY,
        build_role_property_payload(state.character),
        "game 0x000A teleport role-property",
    )
    send_type1(
        conn,
        table,
        server_context,
        OP_GAME_PROPERTY_UPDATE,
        build_movement_speed_payload(),
        "game 0x000E teleport movement-speed",
    )
    mob_records, visible_mob_names = send_map_mobs(
        conn, table, server_context, int(state.game_map_id),
        MAP_MOB_SPAWN_DELAY_SECONDS,
        player_x=int(state.game_spawn_x),
        player_y=int(state.game_spawn_y),
    )
    return mob_records, visible_mob_names, npc_records, visible_npc_names


def send_game_post_map_ready_bundle(conn: socket.socket, table: bytes, server_context: StatefulCryptoContext) -> None:
    if STATE.character is None:
        raise RuntimeError("game character was not loaded")

    # The client emits 0x8015 category=10 only after its map-load state
    # machine reaches the final "PlayerLoc-Final" stage.  This is the grounded
    # readiness signal for UI/model data that should be sent once after login.
    # It is deliberately not tied to an arbitrary first 0x8015, because the
    # same opcode is a generic category/value channel used by unrelated client
    # features (telemetry, UI state persistence and lazy data requests).
    send_backpack_inventory_sync(
        conn, table, server_context,
        "game initial post-map-ready owned-item sync",
    )

    if SKILL_SEND_DELAY_SECONDS > 0:
        time.sleep(SKILL_SEND_DELAY_SECONDS)
    records = load_skill_records_from_db(DATABASE, STATE.character)
    for index, record in enumerate(records, 1):
        send_type1(conn, table, server_context, OP_GAME_SKILL, build_skill_payload(record), f"game 0x0022 skill {index}/{len(records)}")
        if index < len(records) and SKILL_INTER_RECORD_DELAY_SECONDS > 0:
            time.sleep(SKILL_INTER_RECORD_DELAY_SECONDS)
    if PROPERTY_UI_SEND_DELAY_SECONDS > 0:
        time.sleep(PROPERTY_UI_SEND_DELAY_SECONDS)
    for key, value, label, stat_name in iter_property_ui_records(STATE.character):
        send_type1(conn, table, server_context, OP_GAME_PROPERTY_UPDATE, build_property_update_payload(key, value), f"game 0x000E {label}")
        if PROPERTY_UI_INTER_RECORD_DELAY_SECONDS > 0:
            time.sleep(PROPERTY_UI_INTER_RECORD_DELAY_SECONDS)
    if PROFILE_SEND_DELAY_SECONDS > 0:
        time.sleep(PROFILE_SEND_DELAY_SECONDS)
    send_type1(conn, table, server_context, OP_GAME_PROFILE, build_profile_payload(STATE.character), "game 0x001C profile")


def send_game_skill_upgrade_sync(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    result: dict[str, object],
    combat_runtime: MobCombatRuntime | None = None,
) -> None:
    """Synchronize one skill-upgrade transaction and redraw the skill model.

    The open skill window is backed by the complete 0x0022 record set.  A
    successful upgrade may change not only the selected row, but also the
    shared experience value and (for skill 0x0001) the eligibility of every
    other skill.  Therefore all authoritative scalar/stat updates are sent
    first, and the complete skill record set is resent last as the UI redraw
    boundary.  Experience uses confirmed 0x000E key 0x0014, whose client
    handler updates the experience field and refreshes the open skill window.
    No 0x000A role reinitialization and no guessed 0x0021 packet is used here.
    """
    accepted = bool(result.get("accepted"))
    if accepted:
        character, changed = refresh_runtime_character_stats()
    else:
        if STATE.character is None:
            raise RuntimeError("skill-upgrade sync without a runtime character")
        character, changed = STATE.character, ()
    status = "accepted" if accepted else "rejected"
    print(
        f"[{now()}] game 0x8021 {status}; source-based stat sync: "
        f"skill_id={result.get('skill_id')}, reason={result.get('reason')!r}, "
        f"changed={','.join(changed) or '<none>'}"
    )

    requested_skill_id = int(result.get("skill_id", -1))

    # The database transaction is authoritative for persistent costs.  Apply
    # its returned after-values to the runtime view before emitting packets.
    # This avoids a stale secondary read and makes the packet contents directly
    # match the committed transaction.
    persistent_properties: list[tuple[int, str, int]] = []
    if accepted:
        for property_key, field_name, result_key in (
            (PROPERTY_KEY_EXPERIENCE, "experience", "experience_after"),
            (PROPERTY_KEY_PLAYER_GOLD, "player_gold", "gold_after"),
        ):
            if result_key not in result:
                raise RuntimeError(
                    f"accepted skill upgrade missing {result_key}: {result!r}"
                )
            authoritative_value = int(result[result_key])
            character[field_name] = authoritative_value
            if STATE.character is not None:
                STATE.character[field_name] = authoritative_value
            persistent_properties.append(
                (property_key, field_name, authoritative_value)
            )
        # Only skill 0x0001 changes character level.  The freshly reloaded
        # character already contains the committed level.
        persistent_properties.append(
            (PROPERTY_KEY_LEVEL, "level", int(character["level"]))
        )
    else:
        persistent_properties.extend((
            (PROPERTY_KEY_EXPERIENCE, "experience", int(character["experience"])),
            (PROPERTY_KEY_PLAYER_GOLD, "player_gold", int(character["player_gold"])),
            (PROPERTY_KEY_LEVEL, "level", int(character["level"])),
        ))

    # Update the client's shared character state first.  The complete 0x0022
    # list sent at the end is the only UI redraw boundary in this function.
    for key, field_name, value in persistent_properties:
        send_type1(
            conn, table, server_context, OP_GAME_PROPERTY_UPDATE,
            build_property_update_payload(key, value),
            f"game 0x000E skill-upgrade {status} {field_name}={value}",
        )

    send_changed_stat_properties(
        conn, table, server_context, changed,
        f"skill-upgrade {status}",
    )
    direct_resource_updates = tuple(
        key
        for key in ("hp", "mp", "sp")
        if key in dict(result.get("character_updates", {}))
    )
    resource_updates = tuple(dict.fromkeys(
        (*STATE.resource_clamps, *direct_resource_updates)
    ))
    if resource_updates:
        send_current_resource_properties(
            conn, table, server_context, resource_updates,
            f"skill-upgrade {status} resource update",
        )
    if (
        combat_runtime is not None
        and "normal_attack_interval_ms" in changed
    ):
        combat_runtime.set_player_attack_interval_ms(
            int(character["normal_attack_interval_ms"])
        )

    send_type1(
        conn, table, server_context, OP_GAME_PROFILE,
        build_profile_payload(character),
        f"game 0x001C skill-upgrade {status} profile",
    )

    # A successful role-level upgrade changes the eligibility of all seven
    # attribute skills.  Experience is also shared by every row.  Resending
    # only the selected 0x0022 record leaves the already-open window with a
    # partially stale model, while closing/reopening the window rebuilds it
    # from the complete record set.  Mirror that complete rebuild here.
    all_records = load_skill_records_from_db(DATABASE, character)
    if accepted:
        records_to_send = all_records
    else:
        records_to_send = tuple(
            record for record in all_records
            if int(record[0]) == requested_skill_id
        )
    for index, record in enumerate(records_to_send, 1):
        send_type1(
            conn, table, server_context, OP_GAME_SKILL,
            build_skill_payload(record),
            (
                f"game 0x0022 skill-window-refresh {status} "
                f"{index}/{len(records_to_send)} skill_id={int(record[0])}"
            ),
        )
        if (
            index < len(records_to_send)
            and SKILL_INTER_RECORD_DELAY_SECONDS > 0
        ):
            time.sleep(SKILL_INTER_RECORD_DELAY_SECONDS)


def send_game_regeneration_sync(
    conn: socket.socket,
    table: bytes,
    server_context: StatefulCryptoContext,
    result: dict[str, object],
) -> None:
    """Push authoritative current HP/MP/SP after one or more regen seconds."""
    if STATE.character is None or not bool(result.get("changed")):
        return
    for column in ("hp", "mp", "sp"):
        if column in result:
            STATE.character[column] = int(result[column])
    changed_resources = tuple(
        column for column in ("hp", "mp", "sp") if column in result
    )
    send_current_resource_properties(
        conn,
        table,
        server_context,
        changed_resources,
        "resource-regeneration",
    )
    print(
        f"[{now()}] resource regeneration: "
        f"mode={str(result.get('regen_mode', 'IDLE'))}, "
        f"interval={float(result.get('regen_interval_seconds', 1.0)):.1f}s, "
        f"ticks={int(result.get('regeneration_ticks', result.get('elapsed_seconds', 0)))}, "
        f"hp={STATE.character.get('hp')}/{STATE.character.get('max_hp')}, "
        f"mp={STATE.character.get('mp')}/{STATE.character.get('max_mp')}, "
        f"sp={STATE.character.get('sp')}/{STATE.character.get('max_sp')}, "
        f"amounts_per_tick={int(result.get('hp_regen_per_second', 0))}/"
        f"{int(result.get('mp_regen_per_second', 0))}/"
        f"{int(result.get('sp_regen_per_second', 0))}"
    )


def handle_game_client(conn: socket.socket, addr: tuple[str, int], table: bytes, inverse_tables: list[list[int]], port: int) -> None:
    print("=" * 80)
    print(f"[{now()}] Game client connected on port {port}: {addr}")
    control = ConnectionControl()
    combat_runtime: MobCombatRuntime | None = None
    try:
        initial_packet = conn.recv(4096)
        if not initial_packet:
            print(f"[{now()}] Game client sent no initial data")
            return
        dump("game initial encrypted", initial_packet)
        client_context, frame_type, opcode, payload, plain = decode_initial_frame(initial_packet, inverse_tables)
        dump("game initial plain", plain)
        GAME_DISPATCHER.dispatch(PacketContext("game", frame_type, opcode, payload, addr, control))
        if frame_type != 1 or opcode != 0x9007:
            print(f"[{now()}] Game first frame is not 0x9007; no initialization sent")
            return
        (
            server_context,
            initial_mob_records,
            visible_mob_names,
            npc_records,
            visible_npc_names,
        ) = send_game_initialization(conn, table)

        # Monster attack timers and the main receive loop can both change the
        # authoritative player resources.  Serialize SQLite HP writes, the
        # in-memory STATE.character update, and the matching 0x000A sync.
        player_resource_lock = threading.RLock()

        def apply_runtime_mob_damage(
            mob_state: dict[str, object],
            attack_roll: int,
        ) -> dict[str, object]:
            with player_resource_lock:
                if STATE.character is None:
                    return {
                        "changed": False,
                        "reason": "no-runtime-character",
                    }

                defense = max(
                    0, int(STATE.character.get("defense_power", 0) or 0)
                )
                rolled_attack = max(0, int(attack_roll))
                requested_damage = max(1, rolled_attack - defense)
                result = DATABASE.apply_character_damage(
                    int(STATE.character["id"]),
                    requested_damage,
                    minimum_hp=1,
                    max_hp_override=int(STATE.character["max_hp"]),
                )
                result.update({
                    "attack_roll": rolled_attack,
                    "player_defense": defense,
                    "requested_damage_after_defense": requested_damage,
                    "mob_being_name": str(mob_state.get("being_name", "")),
                    "mob_display_name": str(mob_state.get("display_name", "")),
                })

                if "hp_after" in result:
                    STATE.character["hp"] = int(result["hp_after"])
                if bool(result.get("changed")):
                    send_current_resource_properties(
                        conn,
                        table,
                        server_context,
                        ("hp",),
                        (
                            "mob-counterattack-damage "
                            f"mob={result['mob_being_name']!r} "
                            f"damage={int(result.get('applied_damage', 0))}"
                        ),
                    )

                print(
                    f"[{now()}] mob counterattack damage: "
                    f"mob={result.get('mob_display_name')!r}/"
                    f"{result.get('mob_being_name')!r}, "
                    f"roll={rolled_attack}, defense={defense}, "
                    f"damage={int(result.get('applied_damage', 0))}, "
                    f"hp={result.get('hp_before', '?')}->"
                    f"{result.get('hp_after', '?')}/"
                    f"{result.get('max_hp', '?')}, "
                    f"lethal_prevented={bool(result.get('lethal_prevented', False))}"
                )
                return result

        def get_runtime_player_attack_profile() -> dict[str, int]:
            if STATE.stat_service is None or STATE.character is None:
                raw_attack = (
                    STATE.character.get("attack_power", 1)
                    if STATE.character is not None
                    else 1
                )
                attack = max(0, int(raw_attack))
                return {
                    "fixed_before_percent": attack,
                    "weapon_min": 0,
                    "weapon_max": 0,
                    "percent_bp": 0,
                    "attack_min": attack,
                    "attack_max": attack,
                    "displayed_attack": attack,
                }
            return STATE.stat_service.normal_attack_profile().as_dict()

        def grant_runtime_mob_reward(
            reward: dict[str, object],
        ) -> dict[str, object]:
            if STATE.character is None:
                return {
                    "accepted": False,
                    "reason": "no-runtime-character",
                }

            base_experience_reward = max(
                0, int(reward.get("experience_reward", 0) or 0)
            )
            base_gold_reward = max(
                0, int(reward.get("gold_reward", 0) or 0)
            )
            player_level = max(1, int(STATE.character.get("level", 1) or 1))
            mob_level = max(1, int(reward.get("mob_level", 1) or 1))
            (
                experience_reward,
                gold_reward,
                reward_rate_percent,
            ) = calculate_monster_reward_awards(
                base_experience=base_experience_reward,
                base_gold=base_gold_reward,
                player_level=player_level,
                mob_level=mob_level,
            )
            # The death line is the first display message for the kill and is
            # independent of numeric reward settlement.  The combat runtime
            # invokes this callback at most once per death cycle.
            mob_display_name = str(
                reward.get("mob_display_name", "")
                or reward.get("mob_being_name", "")
                or "怪物"
            )
            send_configured_side_message(
                conn,
                table,
                server_context,
                "mob_death",
                f"game mob-death message mob={mob_display_name!r}",
                mob_name=mob_display_name,
            )

            ok, reason, result = DATABASE.grant_mob_kill_reward(
                character_id=int(STATE.character["id"]),
                experience_reward=experience_reward,
                gold_reward=gold_reward,
            )
            if not ok or result is None:
                rejected = {
                    "accepted": False,
                    "reason": reason,
                    "mob_being_name": str(
                        reward.get("mob_being_name", "")
                    ),
                }
                print(
                    f"[{now()}] mob reward rejected: reward={reward}, "
                    f"result={rejected}"
                )
                return rejected

            experience_after = int(result["experience_after"])
            gold_after = int(result["gold_after"])

            print(
                f"[{now()}] mob reward scaling: "
                f"player_level={player_level}, mob_level={mob_level}, "
                f"difference={player_level - mob_level}, "
                f"rate={reward_rate_percent}%, "
                f"experience={base_experience_reward}->"
                f"{int(result['experience_awarded'])}, "
                f"gold={base_gold_reward}->"
                f"{int(result['gold_awarded'])}"
            )
            STATE.character["experience"] = experience_after
            STATE.character["player_gold"] = gold_after

            experience_awarded = int(result["experience_awarded"])
            gold_awarded = int(result["gold_awarded"])
            if experience_awarded > 0:
                send_type1(
                    conn,
                    table,
                    server_context,
                    OP_GAME_PROPERTY_UPDATE,
                    build_property_update_payload(
                        PROPERTY_KEY_EXPERIENCE, experience_after
                    ),
                    (
                        "game 0x000E mob-reward "
                        f"experience={experience_after}"
                    ),
                )
            if gold_awarded > 0:
                send_type1(
                    conn,
                    table,
                    server_context,
                    OP_GAME_PROPERTY_UPDATE,
                    build_property_update_payload(
                        PROPERTY_KEY_PLAYER_GOLD, gold_after
                    ),
                    f"game 0x000E mob-reward player_gold={gold_after}",
                )
            if experience_awarded > 0 or gold_awarded > 0:
                send_type1(
                    conn,
                    table,
                    server_context,
                    OP_GAME_PROFILE,
                    build_profile_payload(STATE.character),
                    "game 0x001C mob-reward profile",
                )
            else:
                print(
                    f"[{now()}] mob reward fully decayed; "
                    "no property/profile packets sent"
                )

            # Confirmed reward-message paths from the original client EXE.
            # Numeric state is already committed and synchronized above, so
            # these display-only packets cannot duplicate or roll back rewards.
            if gold_awarded > 0:
                send_configured_side_message(
                    conn,
                    table,
                    server_context,
                    "mob_reward_gold",
                    f"game mob-reward gold-message gold={gold_awarded}",
                    gold=gold_awarded,
                )

            if experience_awarded > 0:
                send_configured_side_message(
                    conn,
                    table,
                    server_context,
                    "mob_reward_experience",
                    (
                        "game mob-reward experience-message "
                        f"experience={experience_awarded}"
                    ),
                    experience=experience_awarded,
                )

            committed: dict[str, object] = dict(result)
            committed.update({
                "accepted": True,
                "reason": reason,
                "mob_being_name": str(
                    reward.get("mob_being_name", "")
                ),
                "mob_display_name": str(
                    reward.get("mob_display_name", "")
                ),
                "gold_min": int(reward.get("gold_min", 0) or 0),
                "gold_max": int(reward.get("gold_max", 0) or 0),
                "player_level": player_level,
                "mob_level": mob_level,
                "level_difference": player_level - mob_level,
                "reward_rate_percent": reward_rate_percent,
                "base_experience_reward": base_experience_reward,
                "base_gold_reward": base_gold_reward,
            })
            print(
                f"[{now()}] mob reward committed: "
                f"mob={committed['mob_display_name']!r}/"
                f"{committed['mob_being_name']!r}, "
                f"experience=+{int(result['experience_awarded'])} "
                f"({int(result['experience_before'])}->"
                f"{experience_after}), "
                f"gold=+{int(result['gold_awarded'])} "
                f"({int(result['gold_before'])}->{gold_after}), "
                f"rate={reward_rate_percent}%, "
                f"levels={player_level}/{mob_level}, "
                f"base_gold_roll={base_gold_reward}, "
                f"gold_range={committed['gold_min']}-"
                f"{committed['gold_max']}"
            )
            return committed

        combat_runtime = MobCombatRuntime(
            send_type1=lambda opcode, data, label: send_type1(
                conn, table, server_context, opcode, data, label
            ),
            send_plain=lambda plain_data, label: send_stateful_plain(
                conn, table, server_context, plain_data, label
            ),
            apply_player_damage=apply_runtime_mob_damage,
            grant_mob_kill_reward=grant_runtime_mob_reward,
            get_player_attack_profile=get_runtime_player_attack_profile,
            player_attack_interval_ms=int(
                STATE.character.get("normal_attack_interval_ms", 2000)
                if STATE.character is not None
                else 2000
            ),
        )
        combat_runtime.load_map(
            initial_mob_records,
            map_id=int(STATE.game_map_id),
            player_x=int(STATE.game_spawn_x),
            player_y=int(STATE.game_spawn_y),
            player_direction=int(STATE.character["direction"] if STATE.character is not None else 0),
            collision_grid=DATABASE.get_map_collision_grid(int(STATE.game_map_id)),
            initially_visible_beings=visible_mob_names,
        )
        combat_runtime.sync_network_visibility(
            player_x=int(STATE.game_spawn_x),
            player_y=int(STATE.game_spawn_y),
            enter_radius=ENTITY_VISIBILITY_ENTER_RADIUS_TILES,
            leave_radius=ENTITY_VISIBILITY_LEAVE_RADIUS_TILES,
        )

        def handle_runtime_teleport(teleport_row) -> None:
            nonlocal npc_records, visible_npc_names
            combat_runtime.close("teleport begin")
            (
                mob_records,
                teleport_visible_mob_names,
                npc_records,
                visible_npc_names,
            ) = send_game_teleport_bundle(
                conn, table, server_context, teleport_row
            )
            combat_runtime.load_map(
                mob_records,
                map_id=int(STATE.game_map_id),
                player_x=int(STATE.game_spawn_x),
                player_y=int(STATE.game_spawn_y),
                player_direction=int(STATE.character["direction"] if STATE.character is not None else 0),
                collision_grid=DATABASE.get_map_collision_grid(int(STATE.game_map_id)),
                initially_visible_beings=teleport_visible_mob_names,
            )
            combat_runtime.sync_network_visibility(
                player_x=int(STATE.game_spawn_x),
                player_y=int(STATE.game_spawn_y),
                enter_radius=ENTITY_VISIBILITY_ENTER_RADIUS_TILES,
                leave_radius=ENTITY_VISIBILITY_LEAVE_RADIUS_TILES,
            )

        game_handlers.set_teleport_callback(handle_runtime_teleport)

        def handle_runtime_skill_upgrade(result: dict[str, object]) -> None:
            send_game_skill_upgrade_sync(
                conn, table, server_context, result, combat_runtime
            )

        game_handlers.set_skill_upgrade_callback(handle_runtime_skill_upgrade)

        def apply_due_temporary_effect_expiration() -> None:
            result = prune_expired_temporary_stat_effects()
            if result is None:
                return
            character, changed = result
            send_changed_stat_properties(
                conn, table, server_context, changed,
                "temporary-effect expiration",
            )
            if STATE.resource_clamps:
                send_current_resource_properties(
                    conn, table, server_context, STATE.resource_clamps,
                    "temporary-effect expiration resource clamp",
                )
            if "normal_attack_interval_ms" in changed:
                combat_runtime.set_player_attack_interval_ms(
                    int(character["normal_attack_interval_ms"])
                )

        (
            regen_in_combat,
            regen_exit_revision,
            regen_exit_delay_seconds,
        ) = combat_runtime.get_player_regeneration_combat_state()
        regen_interval_seconds = (
            RESOURCE_REGEN_COMBAT_INTERVAL_SECONDS
            if regen_in_combat
            else RESOURCE_REGEN_IDLE_INTERVAL_SECONDS
        )
        regen_exit_delay_pending = False
        next_regen_monotonic = time.monotonic() + regen_interval_seconds

        def apply_due_resource_regeneration() -> None:
            nonlocal regen_in_combat, regen_interval_seconds
            nonlocal regen_exit_revision, regen_exit_delay_seconds
            nonlocal regen_exit_delay_pending, next_regen_monotonic
            with player_resource_lock:
                if STATE.character is None:
                    return

                current_monotonic = time.monotonic()
                (
                    current_in_combat,
                    current_exit_revision,
                    current_exit_delay_seconds,
                ) = combat_runtime.get_player_regeneration_combat_state()
                mode_changed = current_in_combat != regen_in_combat
                return_exit_announced = (
                    current_exit_revision != regen_exit_revision
                )
                if mode_changed or return_exit_announced:
                    previous_in_combat = regen_in_combat
                    regen_in_combat = current_in_combat
                    regen_exit_revision = current_exit_revision
                    regen_exit_delay_seconds = current_exit_delay_seconds
                    regen_interval_seconds = (
                        RESOURCE_REGEN_COMBAT_INTERVAL_SECONDS
                        if regen_in_combat
                        else RESOURCE_REGEN_IDLE_INTERVAL_SECONDS
                    )

                    # When the final engaged monster starts RETURNING, the first
                    # IDLE regeneration tick waits five seconds.  This is a
                    # one-shot exit delay; after that tick, normal IDLE recovery
                    # resumes every one second.
                    regen_exit_delay_pending = bool(
                        previous_in_combat
                        and not regen_in_combat
                        and return_exit_announced
                        and regen_exit_delay_seconds
                        > RESOURCE_REGEN_IDLE_INTERVAL_SECONDS
                    )
                    first_delay_seconds = (
                        regen_exit_delay_seconds
                        if regen_exit_delay_pending
                        else regen_interval_seconds
                    )
                    next_regen_monotonic = (
                        current_monotonic + first_delay_seconds
                    )
                    print(
                        f"[{now()}] resource regeneration mode: "
                        f"{'COMBAT' if regen_in_combat else 'IDLE'}, "
                        f"regular_interval={regen_interval_seconds:.1f}s, "
                        f"first_delay={first_delay_seconds:.1f}s, "
                        f"return_exit={regen_exit_delay_pending}"
                    )
                    return

                if current_monotonic < next_regen_monotonic:
                    return

                # One tick always restores exactly the character's configured
                # HP/MP/SP recovery amounts.  A delayed server loop may owe more
                # than one tick, but a 5-second combat tick never becomes five
                # times the normal recovery amount.
                if regen_exit_delay_pending:
                    # The five-second return delay is consumed by exactly one
                    # normal-sized recovery tick, then IDLE returns to its
                    # regular one-second cadence.
                    due_ticks = 1
                    regen_exit_delay_pending = False
                    next_regen_monotonic = (
                        current_monotonic + regen_interval_seconds
                    )
                else:
                    due_ticks = 1 + int(
                        (current_monotonic - next_regen_monotonic)
                        // regen_interval_seconds
                    )
                    next_regen_monotonic += (
                        due_ticks * regen_interval_seconds
                    )
                result = DATABASE.apply_character_regeneration(
                    int(STATE.character["id"]),
                    due_ticks,
                    max_hp=int(STATE.character["max_hp"]),
                    max_mp=int(STATE.character["max_mp"]),
                    max_sp=int(STATE.character["max_sp"]),
                    hp_regen_per_second=int(STATE.character["hp_regen_per_second"]),
                    mp_regen_per_second=int(STATE.character["mp_regen_per_second"]),
                    sp_regen_per_second=int(STATE.character["sp_regen_per_second"]),
                )
                result.update({
                    "regeneration_ticks": due_ticks,
                    "regen_mode": "COMBAT" if regen_in_combat else "IDLE",
                    "regen_interval_seconds": regen_interval_seconds,
                })
                send_game_regeneration_sync(
                    conn, table, server_context, result
                )

        stream = bytearray()
        packet_number = 0
        post_map_ready_bundle_sent = False
        welcome_side_message_sent = False
        map_ready_monotonic: float | None = None
        heartbeat_last_client_seconds: int | None = None
        heartbeat_last_server_monotonic: float | None = None
        heartbeat_report_count = 0
        active_shop: ShopDefinition | None = None
        conn.settimeout(GAME_POST_REPLY_RECV_TIMEOUT_SECONDS)
        while True:
            try:
                ciphertext = conn.recv(4096)
            except socket.timeout:
                apply_due_temporary_effect_expiration()
                apply_due_resource_regeneration()
                continue
            if not ciphertext:
                print(f"[{now()}] Game client disconnected")
                return
            packet_number += 1
            dump(f"game follow-up encrypted #{packet_number}", ciphertext)
            state_before = client_context.state
            plain = decode_stateful_packet(ciphertext=ciphertext, inverse_tables=inverse_tables, context=client_context)
            print(f"[{now()}] game follow-up decode #{packet_number}: state_before=0x{state_before:02X}, state_after=0x{client_context.state:02X}, row=0x{client_context.table_row:02X}")
            dump(f"game follow-up plain #{packet_number}", plain)
            stream.extend(plain)
            for frame in extract_plain_frames(stream):
                frame_type, opcode, payload = describe_plain_frame(frame, packet_number)
                map_id_before_dispatch = int(STATE.game_map_id)
                GAME_DISPATCHER.dispatch(
                    PacketContext("game", frame_type, opcode, payload, addr, control)
                )
                if frame_type == 1 and opcode == 0x8005:
                    # The dispatcher may synchronously trigger a map teleport.
                    # In that case the callback has already reloaded combat state
                    # at the destination, so the old trigger coordinate must not
                    # be applied to the new map.
                    if int(STATE.game_map_id) == map_id_before_dispatch:
                        if len(payload) == 8:
                            packed_position = int.from_bytes(payload[0:4], "big")
                            position_x = (packed_position >> 16) & 0xFFFF
                            position_y = packed_position & 0xFFFF
                            sync_map_npc_visibility(
                                conn,
                                table,
                                server_context,
                                map_id=map_id_before_dispatch,
                                records=npc_records,
                                visible_names=visible_npc_names,
                                player_x=position_x,
                                player_y=position_y,
                            )
                            combat_runtime.sync_network_visibility(
                                player_x=position_x,
                                player_y=position_y,
                                enter_radius=ENTITY_VISIBILITY_ENTER_RADIUS_TILES,
                                leave_radius=ENTITY_VISIBILITY_LEAVE_RADIUS_TILES,
                            )
                        combat_runtime.handle_player_movement(
                            payload, map_id=map_id_before_dispatch
                        )
                elif frame_type == 1 and opcode == 0x8014:
                    combat_runtime.handle_player_attack(
                        payload, player_being_name=str(STATE.being_name or "")
                    )
                if frame_type == 1 and opcode == CLIENT_OP_NPC_INTERACT:
                    active_shop = send_npc_shop_open_response(
                        conn, table, server_context, payload
                    )
                elif frame_type == 1 and opcode == CLIENT_OP_SHOP_CATEGORY:
                    send_shop_category_response(
                        conn, table, server_context, active_shop, payload
                    )
                elif frame_type == 1 and opcode == CLIENT_OP_SHOP_TRANSACTION:
                    handle_shop_transaction_request(
                        conn, table, server_context, active_shop, payload
                    )
                elif frame_type == 1 and opcode == CLIENT_OP_INVENTORY_MOVE:
                    handle_inventory_move_request(
                        conn, table, server_context, payload, combat_runtime
                    )
                if frame_type == 1 and opcode == 0x8015:
                    category = (
                        int.from_bytes(payload[0:2], "big")
                        if len(payload) >= 2
                        else None
                    )
                    value = (
                        int.from_bytes(payload[2:6], "big")
                        if len(payload) >= 6
                        else None
                    )
                    if len(payload) != 6:
                        print(
                            f"[{now()}] client 0x8015 malformed: "
                            f"payload_len={len(payload)}, payload={payload.hex(' ')}; "
                            "no response"
                        )
                    elif category == 10:
                        # Confirmed client call site: map-load state reaches
                        # PlayerLoc-Final, sends 0x8012 value=0, then sends
                        # 0x8015 category=10/value=0 and resets its elapsed
                        # counters.  Treat this as a readiness notification,
                        # not as a request that needs an acknowledgement.
                        map_ready_monotonic = time.monotonic()
                        heartbeat_last_client_seconds = None
                        heartbeat_last_server_monotonic = None
                        heartbeat_report_count = 0
                        print(
                            f"[{now()}] client 0x8015 map-ready: "
                            f"category=10, value={value}, map_id={STATE.game_map_id}; "
                            "no direct protocol reply"
                        )
                        if not welcome_side_message_sent:
                            # Category 10 is already the client's final map-ready
                            # notification, so the welcome line can be sent
                            # immediately instead of waiting for the deliberately
                            # delayed skill/property initialization bundle.
                            send_configured_side_message(
                                conn,
                                table,
                                server_context,
                                "login_welcome",
                                "game login-welcome message",
                            )
                            # Mark the one-shot event complete even when the
                            # message is disabled in config; changing config
                            # should not cause repeated sends in one connection.
                            welcome_side_message_sent = True
                        if not post_map_ready_bundle_sent:
                            send_game_post_map_ready_bundle(
                                conn, table, server_context
                            )
                            post_map_ready_bundle_sent = True
                    elif category == 6:
                        # Confirmed client call site: frame delta is accumulated;
                        # every 10 seconds the cumulative elapsed seconds since
                        # the most recent map-ready transition are reported.
                        # This is telemetry/liveness data, not an inventory
                        # request.  The client sets no pending-reply state, so
                        # there is no evidence for an immediate acknowledgement.
                        current_server_monotonic = time.monotonic()
                        heartbeat_report_count += 1
                        server_elapsed = (
                            current_server_monotonic - map_ready_monotonic
                            if map_ready_monotonic is not None
                            else None
                        )
                        client_delta = (
                            value - heartbeat_last_client_seconds
                            if value is not None
                            and heartbeat_last_client_seconds is not None
                            else None
                        )
                        server_delta = (
                            current_server_monotonic
                            - heartbeat_last_server_monotonic
                            if heartbeat_last_server_monotonic is not None
                            else None
                        )
                        drift = (
                            float(value) - server_elapsed
                            if value is not None and server_elapsed is not None
                            else None
                        )
                        anomaly_parts: list[str] = []
                        if map_ready_monotonic is None:
                            anomaly_parts.append("before-map-ready")
                        if client_delta is not None and client_delta < 0:
                            anomaly_parts.append("client-counter-regressed")
                        if client_delta is not None and client_delta == 0:
                            anomaly_parts.append("duplicate-client-counter")
                        anomaly = ",".join(anomaly_parts) or "none"
                        print(
                            f"[{now()}] client 0x8015 elapsed-report: "
                            f"category=6, client_seconds={value}, "
                            f"client_delta={client_delta}, "
                            f"server_delta={server_delta if server_delta is not None else 'n/a'}, "
                            f"drift={drift if drift is not None else 'n/a'}, "
                            f"report={heartbeat_report_count}, anomaly={anomaly}; "
                            "telemetry accepted, no direct protocol reply"
                        )
                        if value is not None:
                            heartbeat_last_client_seconds = value
                        heartbeat_last_server_monotonic = current_server_monotonic
                    else:
                        known_hint = {
                            1: "client UI/action state (exact domain unresolved)",
                            5: "player/UI flag update (exact flag unresolved)",
                            9: "scene-manager event (exact meaning unresolved)",
                            11: "persisted UI window state slot 0",
                            12: "persisted UI window state slot 1",
                            13: "persisted UI window state slot 2",
                            14: "persisted UI window state slot 3",
                            15: "persisted UI window state slot 4",
                            21: "lazy list-data request (exact domain unresolved)",
                        }.get(category, "unclassified category")
                        print(
                            f"[{now()}] client 0x8015 generic event: "
                            f"category={category}, value={value}, hint={known_hint}; "
                            "no generic response"
                        )
                apply_due_temporary_effect_expiration()
                apply_due_resource_regeneration()
                if control.close_requested:
                    print(f"[{now()}] Game close requested: {control.close_reason}")
                    return
    except ConnectionResetError:
        print(f"[{now()}] Game connection reset by client")
    except BrokenPipeError:
        print(f"[{now()}] Game broken pipe")
    except KeyboardInterrupt:
        raise
    except Exception as error:
        print(f"[{now()}] Game connection error: {type(error).__name__}: {error}")
    finally:
        if combat_runtime is not None:
            combat_runtime.close("game connection closed")
        try:
            conn.close()
        except OSError:
            pass
        print(f"[{now()}] Game connection closed")


def game_listener(table: bytes, inverse_tables: list[list[int]], port: int) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((HOST, port))
        server.listen(5)
        print(f"[{now()}] Game listener active on {HOST}:{port}")
        while True:
            conn, addr = server.accept()
            handle_game_client(conn, addr, table, inverse_tables, port)
    except Exception as error:
        print(f"[{now()}] Game listener {port} failed: {type(error).__name__}: {error}")
    finally:
        server.close()


def start_game_listener(table: bytes, inverse_tables: list[list[int]]) -> None:
    thread = threading.Thread(target=game_listener, args=(table, inverse_tables, GAME_PORT), daemon=True)
    thread.start()


def main() -> None:
    load_runtime_configuration()
    table = load_crypto_table(config.GKK2_PATH)
    print(f"[{now()}] Building inverse crypto tables")
    inverse_tables = build_inverse_tables(table)
    print(f"[{now()}] Inverse crypto tables ready")
    verify_known_stateful_vector(inverse_tables=inverse_tables)
    start_game_listener(table, inverse_tables)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, LOGIN_PORT))
    server.listen(20)
    print(f"[{now()}] Login listener active on {HOST}:{LOGIN_PORT}; server={SERVER_NAME!r}")
    try:
        while True:
            conn, addr = server.accept()
            handle_login_client(conn, addr, table, inverse_tables)
    except KeyboardInterrupt:
        print(f"\n[{now()}] Server stopped")
    finally:
        server.close()


if __name__ == "__main__":
    main()
