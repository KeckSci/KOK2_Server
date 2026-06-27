from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from database import Database
from dispatcher import GAME_DISPATCHER, PacketContext
from shop_protocol import (
    decode_ascii_c_string,
    decode_shop_category_request,
    decode_shop_transaction_request,
)
from inventory_protocol import (
    CLIENT_OP_INVENTORY_MOVE,
    decode_inventory_move_request,
)


@dataclass
class GameHandlerRuntime:
    """
    Runtime dependencies for game-server packet handlers.

    Movement is persisted and runtime state is reloaded on every 0x9007 login,
    marks normal client quit through 0x8003, and accepts state-save packets
    that the client sends immediately before quitting.
    """

    database: Database
    character_id: int
    map_id: int
    character_name: str
    teleport_callback: Callable[[Any], None] | None = None
    skill_upgrade_callback: Callable[[dict[str, object]], None] | None = None


_RUNTIME: GameHandlerRuntime | None = None


def configure_game_handlers(runtime: GameHandlerRuntime) -> None:
    global _RUNTIME
    _RUNTIME = runtime

    print(
        "[game-handler] Runtime configured: "
        f"character_id={runtime.character_id}, "
        f"character_name={runtime.character_name!r}, "
        f"map_id={runtime.map_id}"
    )


def require_runtime() -> GameHandlerRuntime:
    if _RUNTIME is None:
        raise RuntimeError(
            "Game handlers were used before configure_game_handlers()"
        )
    return _RUNTIME


def set_teleport_callback(callback: Callable[[Any], None] | None) -> None:
    runtime = require_runtime()
    runtime.teleport_callback = callback
    print(
        "[game-handler] Teleport callback "
        f"{'installed' if callback is not None else 'cleared'}"
    )


def set_skill_upgrade_callback(
    callback: Callable[[dict[str, object]], None] | None,
) -> None:
    runtime = require_runtime()
    runtime.skill_upgrade_callback = callback
    print(
        "[game-handler] Skill-upgrade callback "
        f"{'installed' if callback is not None else 'cleared'}"
    )


def _decode_skill_upgrade_request(payload: bytes) -> tuple[int, int]:
    """Decode 0x8021 as ``skill_id, action`` big-endian uint16 values.

    The non-ambiguous vitality sample ``00 02 00 01`` confirms that the first
    word is the requested skill ID and the second word is the action.  The
    earlier level sample ``00 01 00 01`` could not reveal the order because
    both values happened to be 1.
    """
    if len(payload) != 4:
        raise ValueError(
            f"0x8021 payload must be 4 bytes, got {len(payload)}"
        )
    skill_id = int.from_bytes(payload[0:2], "big")
    action = int.from_bytes(payload[2:4], "big")
    return skill_id, action


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8021)
def handle_skill_upgrade_request(context: PacketContext) -> None:
    runtime = require_runtime()
    try:
        skill_id, action = _decode_skill_upgrade_request(context.payload)
    except ValueError as error:
        print(
            "[game-handler] malformed 0x8021 skill-upgrade request: "
            f"error={error}, payload={context.payload.hex(' ')}"
        )
        return

    word0 = int.from_bytes(context.payload[0:2], "big")
    word1 = int.from_bytes(context.payload[2:4], "big")
    print(
        "[game-handler] 0x8021 skill-upgrade request: "
        f"skill_word={word0}, action_word={word1}, "
        f"resolved_skill_id={skill_id}, resolved_action={action}"
    )

    result = runtime.database.upgrade_character_skill(
        character_id=runtime.character_id,
        skill_id=skill_id,
        action=action,
    )
    print(
        "[game-handler] 0x8021 skill-upgrade result: "
        + ", ".join(f"{key}={value!r}" for key, value in result.items())
    )

    if runtime.skill_upgrade_callback is None:
        print(
            "[game-handler] Skill upgrade persisted/rejected, but no sender "
            "callback is installed; the client will refresh after reconnect."
        )
        return
    runtime.skill_upgrade_callback(result)


@GAME_DISPATCHER.register(frame_type=1, opcode=0x9007)
def observe_game_login(context: PacketContext) -> None:
    print(
        "[game-handler] 0x9007 game-server login: "
        f"payload={context.payload.hex(' ')}"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8003)
def handle_client_quit(context: PacketContext) -> None:
    """
    The client sends opcode 0x8003 with the null-terminated string "quit"
    during its normal shutdown sequence.

    The handler asks the socket owner to close only the current game
    connection.
    """

    if context.payload != b"quit\x00":
        print(
            "[game-handler] 0x8003 quit-like request with unexpected payload: "
            f"{context.payload.hex(' ')}; closing game connection anyway"
        )
    else:
        print(
            "[game-handler] 0x8003 normal quit request accepted"
        )

    runtime = require_runtime()
    runtime.database.update_character_last_logout_area(
        character_id=runtime.character_id,
        logout_area=runtime.map_id,
    )
    print(
        "[game-handler] 0x8003 last_logout_area persisted: "
        f"character={runtime.character_name!r}, area={runtime.map_id}"
    )

    if context.control is None:
        print(
            "[game-handler] 0x8003 cannot request close: "
            "connection control is unavailable"
        )
        return

    context.control.request_close(
        "client sent opcode 0x8003"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8031)
def handle_client_ui_state_save(context: PacketContext) -> None:
    """
    The client sends opcode 0x8031 from FUN_004555e0 during normal quit.

    The payload is an array of uint32 values: uint32 count followed by count
    big-endian uint32 records. It stores interface/task-like client state.
    """

    payload = context.payload
    count = (
        int.from_bytes(payload[0:4], "big")
        if len(payload) >= 4
        else None
    )
    expected_length = (
        4 + count * 4
        if count is not None
        else None
    )

    if count is None or expected_length != len(payload):
        print(
            "[game-handler] 0x8031 UI-state save with unusual payload: "
            f"count={count}, expected_length={expected_length}, "
            f"actual_length={len(payload)}, payload={payload.hex(' ')}"
        )
    else:
        values = [
            int.from_bytes(payload[offset:offset + 4], "big")
            for offset in range(4, len(payload), 4)
        ]
        print(
            "[game-handler] 0x8031 UI-state save accepted: "
            f"count={count}, values_head={values[:6]}"
        )

    # Do not close here. The client sends 0x8003 "quit" after this state-save
    # frame, often in the same encrypted packet.


@GAME_DISPATCHER.register(frame_type=1, opcode=0x802F)
def observe_client_option_state(context: PacketContext) -> None:
    """
    The client may send opcode 0x802F before quit. FUN_004CC560 shows the
    layout as uint8 plus three uint32 fields.
    """

    payload = context.payload
    if len(payload) != 13:
        print(
            "[game-handler] 0x802F option-state payload with unusual length: "
            f"expected=13, actual={len(payload)}, payload={payload.hex(' ')}"
        )
        return

    field00 = payload[0]
    field01 = int.from_bytes(payload[1:5], "big")
    field05 = int.from_bytes(payload[5:9], "big")
    field09 = int.from_bytes(payload[9:13], "big")
    print(
        "[game-handler] 0x802F option-state save accepted: "
        f"field00=0x{field00:02X}, "
        f"field01=0x{field01:08X}, "
        f"field05=0x{field05:08X}, "
        f"field09=0x{field09:08X}"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8005)
def handle_player_movement(context: PacketContext) -> None:
    """
    Confirmed payload layout from the client wrapper FUN_004cba80:

        uint32 packed_position
        uint8  field04
        uint16 field05
        uint8  field07

    packed_position = (x << 16) | y
    """

    runtime = require_runtime()
    payload = context.payload

    if len(payload) != 8:
        print(
            "[game-handler] Invalid 0x8005 movement payload length: "
            f"expected=8, actual={len(payload)}, "
            f"payload={payload.hex(' ')}"
        )
        return

    packed_position = int.from_bytes(payload[0:4], "big")
    position_x = (packed_position >> 16) & 0xFFFF
    position_y = packed_position & 0xFFFF

    field04 = payload[4]
    field05 = int.from_bytes(payload[5:7], "big")
    field07 = payload[7]

    teleport = runtime.database.find_enabled_teleport(
        source_map_id=runtime.map_id,
        trigger_x=position_x,
        trigger_y=position_y,
    )

    if teleport is not None:
        target_map_id = int(teleport["target_map_id"])
        target_x = int(teleport["target_x"])
        target_y = int(teleport["target_y"])
        target_direction = int(teleport["target_direction"])

        runtime.database.update_character_position(
            character_id=runtime.character_id,
            map_id=target_map_id,
            position_x=target_x,
            position_y=target_y,
            direction=target_direction,
        )
        old_map_id = runtime.map_id
        runtime.map_id = target_map_id

        print(
            "[game-handler] 0x8005 teleport triggered: "
            f"character={runtime.character_name!r}, "
            f"from=({old_map_id}, {position_x}, {position_y}), "
            f"to=({target_map_id}, {target_x}, {target_y}), "
            f"direction=0x{target_direction:04X}, "
            f"note={str(teleport['note'])!r}"
        )

        if runtime.teleport_callback is None:
            print(
                "[game-handler] Teleport persisted but no sender callback is installed; "
                "client will see the new map after reconnect."
            )
            return

        runtime.teleport_callback(teleport)
        return

    runtime.database.update_character_position(
        character_id=runtime.character_id,
        map_id=runtime.map_id,
        position_x=position_x,
        position_y=position_y,
        direction=field05,
    )

    print(
        "[game-handler] 0x8005 movement persisted: "
        f"character={runtime.character_name!r}, "
        f"map_id={runtime.map_id}, "
        f"position=({position_x}, {position_y}), "
        f"field04=0x{field04:02X}, "
        f"field05=0x{field05:04X}, "
        f"field07=0x{field07:02X}"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8012)
def observe_client_event_8012(context: PacketContext) -> None:
    value = (
        int.from_bytes(context.payload, "big")
        if len(context.payload) == 2
        else None
    )
    print(
        "[game-handler] 0x8012 client event: "
        f"value={value}, payload={context.payload.hex(' ')}"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8015)
def observe_game_state(context: PacketContext) -> None:
    category = (
        int.from_bytes(context.payload[0:2], "big")
        if len(context.payload) >= 2
        else None
    )
    value = (
        int.from_bytes(context.payload[2:6], "big")
        if len(context.payload) >= 6
        else None
    )
    print(
        "[game-handler] 0x8015 category/value event: "
        f"category={category}, value={value}"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8009)
def observe_npc_interaction(context: PacketContext) -> None:
    target = decode_ascii_c_string(context.payload)
    print(
        "[game-handler] 0x8009 NPC interaction: "
        f"target={target!r}"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8018)
def observe_shop_category_request(context: PacketContext) -> None:
    action, category = decode_shop_category_request(context.payload)
    print(
        "[game-handler] 0x8018 shop category request: "
        f"action={action}, category={category!r}"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=0x8019)
def observe_shop_transaction_request(context: PacketContext) -> None:
    try:
        request = decode_shop_transaction_request(context.payload)
    except (ValueError, UnicodeDecodeError) as error:
        print(
            "[game-handler] malformed 0x8019 shop transaction request: "
            f"error={error}, payload={context.payload.hex(' ')}"
        )
        return
    print(
        "[game-handler] 0x8019 shop transaction request: "
        f"action={request.action}, item={request.resource_key!r}, "
        f"client_u16=({request.client_value1},{request.client_value2})"
    )


@GAME_DISPATCHER.register(frame_type=1, opcode=CLIENT_OP_INVENTORY_MOVE)
def observe_inventory_move_request(context: PacketContext) -> None:
    try:
        request = decode_inventory_move_request(context.payload)
    except ValueError as error:
        print(
            "[game-handler] malformed 0x800B inventory move request: "
            f"error={error}, payload={context.payload.hex(' ')}"
        )
        return
    print(
        "[game-handler] 0x800B inventory move request: "
        f"source_slot={request.source_slot}, "
        f"target_slot={request.target_slot}, client_quantity={request.client_quantity}"
    )
