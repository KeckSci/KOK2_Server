from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class ConnectionControl:
    """Mutable lifecycle request shared by handlers and the socket owner."""

    close_requested: bool = False
    close_reason: str = ""

    def request_close(self, reason: str) -> None:
        self.close_requested = True
        self.close_reason = reason


@dataclass(frozen=True)
class PacketContext:
    channel: str
    frame_type: int
    opcode: int
    payload: bytes
    peer: tuple[str, int] | None = None
    control: ConnectionControl | None = None


PacketHandler = Callable[[PacketContext], None]


class OpcodeDispatcher:
    """Small opcode registry used while protocol recovery is still evolving."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handlers: dict[tuple[int, int], PacketHandler] = {}

    def register(
        self,
        frame_type: int,
        opcode: int,
    ) -> Callable[[PacketHandler], PacketHandler]:
        key = (frame_type, opcode)

        def decorator(handler: PacketHandler) -> PacketHandler:
            if key in self._handlers:
                raise RuntimeError(
                    f"{self.name}: duplicate handler for "
                    f"type={frame_type}, opcode=0x{opcode:04X}"
                )
            self._handlers[key] = handler
            return handler

        return decorator

    def dispatch(self, context: PacketContext) -> bool:
        handler = self._handlers.get(
            (context.frame_type, context.opcode)
        )

        if handler is None:
            print(
                f"[dispatcher:{self.name}] Unhandled packet: "
                f"channel={context.channel}, "
                f"type={context.frame_type}, "
                f"opcode=0x{context.opcode:04X}, "
                f"payload_length={len(context.payload)}"
            )
            return False

        handler(context)
        return True


LOGIN_DISPATCHER = OpcodeDispatcher("login")
GAME_DISPATCHER = OpcodeDispatcher("game")
