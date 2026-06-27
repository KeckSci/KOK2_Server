from __future__ import annotations

from dataclasses import dataclass

CLIENT_OP_INVENTORY_MOVE = 0x800B


@dataclass(frozen=True)
class InventoryMoveRequest:
    source_slot: int
    target_slot: int
    client_quantity: int


def decode_inventory_move_request(payload: bytes) -> InventoryMoveRequest:
    """Decode client opcode 0x800B: source index, target index, move quantity.

    The payload is three big-endian uint16 values.  Static client analysis
    confirms the third value is copied directly from item+0x174 before the
    request is built; it is not an action code.

    Stackable objects normally send a positive quantity.  Original
    non-stackable equipment uses quantity 0, meaning "move this independent
    item object" rather than "move zero items".
    """
    if len(payload) != 6:
        raise ValueError(
            f"0x800B inventory move payload must be 6 bytes, got {len(payload)}"
        )
    return InventoryMoveRequest(
        source_slot=int.from_bytes(payload[0:2], "big"),
        target_slot=int.from_bytes(payload[2:4], "big"),
        client_quantity=int.from_bytes(payload[4:6], "big"),
    )
