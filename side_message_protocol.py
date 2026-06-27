from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from server_packets import (
    CHAT_MESSAGE_TYPE_GREEN,
    OP_GAME_CHAT_MESSAGE,
    OP_GAME_SYSTEM_MESSAGE,
    build_chat_message_record,
    build_system_message_record,
)


STYLE_SYSTEM_LIGHT = "system_light"
STYLE_GREEN = "green"
STYLE_WHITE = "white"

# The client wraps opcode 0x0012/type=2 message text in a literal-text region:
#     <font green>\x07 MESSAGE \x08</font><br>
# 0x07 enters literal mode and 0x08 leaves it.  By beginning the server text
# with 0x08, we leave that region, close the green font, open a white font,
# and then re-enter literal mode for the actual user-facing text.  The final
# 0x08 deliberately leaves literal mode before the client appends </font>.
_LITERAL_BEGIN = "\x07"
_LITERAL_END = "\x08"
_WHITE_FONT_OPEN = "<font color = #FFFFFF>"


@dataclass(frozen=True)
class SideMessagePacket:
    opcode: int
    payload: bytes
    style: str
    color_hex: str


class SideMessageConfigurationError(ValueError):
    pass


def _definition_value(
    definitions: Mapping[str, Mapping[str, Any]],
    message_key: str,
) -> Mapping[str, Any]:
    definition = definitions.get(message_key)
    if not isinstance(definition, Mapping):
        raise SideMessageConfigurationError(
            f"missing side-message definition: {message_key!r}"
        )
    return definition


def render_configured_message(
    definitions: Mapping[str, Mapping[str, Any]],
    message_key: str,
    **values: object,
) -> tuple[bool, str, str]:
    """Return ``(enabled, style, rendered_text)`` for one configured message."""
    definition = _definition_value(definitions, message_key)
    enabled = bool(definition.get("enabled", False))
    style = str(definition.get("style", "")).strip().lower()
    template = str(definition.get("template", ""))

    if style not in {STYLE_SYSTEM_LIGHT, STYLE_GREEN, STYLE_WHITE}:
        raise SideMessageConfigurationError(
            f"unsupported side-message style for {message_key!r}: {style!r}"
        )
    if "<font" in template.lower() or "</font" in template.lower():
        raise SideMessageConfigurationError(
            f"font tags are not supported in side-message template {message_key!r}"
        )

    try:
        rendered = template.format(**values)
    except (KeyError, IndexError, ValueError) as error:
        raise SideMessageConfigurationError(
            f"cannot render side-message {message_key!r}: {error}"
        ) from error

    if "<font" in rendered.lower() or "</font" in rendered.lower():
        raise SideMessageConfigurationError(
            f"font tags are not supported in rendered side-message {message_key!r}"
        )
    if _LITERAL_BEGIN in rendered or _LITERAL_END in rendered:
        raise SideMessageConfigurationError(
            f"reserved client control characters are not supported in side-message {message_key!r}"
        )
    return enabled, style, rendered


def build_side_message_packet(style: str, message: str) -> SideMessagePacket:
    """Build one packet using confirmed client formatting paths."""
    normalized = str(style).strip().lower()
    text = str(message)

    if normalized == STYLE_SYSTEM_LIGHT:
        return SideMessagePacket(
            opcode=OP_GAME_SYSTEM_MESSAGE,
            payload=build_system_message_record(text),
            style=STYLE_SYSTEM_LIGHT,
            color_hex="#FDF597",
        )
    if normalized == STYLE_GREEN:
        return SideMessagePacket(
            opcode=OP_GAME_CHAT_MESSAGE,
            payload=build_chat_message_record(
                CHAT_MESSAGE_TYPE_GREEN,
                "",
                text,
            ),
            style=STYLE_GREEN,
            color_hex="#42FF00",
        )
    if normalized == STYLE_WHITE:
        if _LITERAL_BEGIN in text or _LITERAL_END in text:
            raise SideMessageConfigurationError(
                "white side-message text contains reserved client control characters"
            )
        # Final type=2 side-message markup produced by the client becomes:
        #   <font green>\x07\x08</font><font white>\x07TEXT\x08\x08</font><br>
        # Both 0x08 operations are idempotent, so this is balanced in UIChat2.
        escaped_text = (
            _LITERAL_END
            + "</font>"
            + _WHITE_FONT_OPEN
            + _LITERAL_BEGIN
            + text
            + _LITERAL_END
        )
        return SideMessagePacket(
            opcode=OP_GAME_CHAT_MESSAGE,
            payload=build_chat_message_record(
                CHAT_MESSAGE_TYPE_GREEN,
                "",
                escaped_text,
            ),
            style=STYLE_WHITE,
            color_hex="#FFFFFF",
        )

    raise SideMessageConfigurationError(
        f"unsupported side-message style: {style!r}"
    )
