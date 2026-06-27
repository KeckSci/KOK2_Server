# =============================================================================
# KOK2 PROTOCOL MODULE
#
# Owns common frame builders, frame extraction and packet descriptions.
# Extracted from the confirmed working legacy server without changing behavior.
# =============================================================================

import datetime


TEXT_ENCODING = "CP936"


def now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def ascii_view(data: bytes) -> str:
    return "".join(
        chr(value) if 32 <= value <= 126 else "."
        for value in data
    )


def dump(label: str, data: bytes) -> None:
    print(f"[{now()}] {label}: {len(data)} bytes")

    if not data:
        print("<empty>")
        return

    print(data.hex(" "))
    print("ASCII:", ascii_view(data))


def encode_text(value: str | bytes, encoding: str = TEXT_ENCODING) -> bytes:
    """Encode protocol text using the confirmed client C-string code page."""
    if isinstance(value, bytes):
        if b"\x00" in value:
            raise ValueError("protocol text bytes must not contain NUL")
        return value
    return str(value).encode(encoding, errors="strict")


def c_string(value: str | bytes, encoding: str = TEXT_ENCODING) -> bytes:
    """Encode a byte-oriented protocol C string with a trailing NUL byte."""
    return encode_text(value, encoding=encoding) + b"\x00"


def build_type1_plain(opcode: int, payload: bytes) -> bytes:
    if not 0 <= opcode <= 0xFFFF:
        raise ValueError("opcode 必须在 0x0000 到 0xFFFF 之间")
    return (
        b"\x00\x01"
        + opcode.to_bytes(2, "big")
        + len(payload).to_bytes(4, "big")
        + payload
    )




def build_type2_plain(opcode: int, records: list[bytes]) -> bytes:
    """
    Build a type 2 multi-record frame.

    Decompiled client sender FUN_004C0620 builds:
        uint16 frame_type = 2
        uint16 opcode
        uint32 payload_length
        repeated serialized records

    payload_length is the byte length of the repeated record data only,
    excluding the 8-byte frame header.
    """
    if not 0 <= opcode <= 0xFFFF:
        raise ValueError("opcode 必须在 0x0000 到 0xFFFF 之间")

    payload = b"".join(records)
    return (
        b"\x00\x02"
        + opcode.to_bytes(2, "big")
        + len(payload).to_bytes(4, "big")
        + payload
    )


def build_type4_plain(
    opcode: int,
    data: bytes,
) -> bytes:
    """
    type 4 内部 frame：

        00 04
        + payload length，4 字节大端
        + opcode，2 字节大端
        + data
    """

    if not 0 <= opcode <= 0xFFFF:
        raise ValueError(
            "opcode 必须在 0x0000 到 0xFFFF 之间"
        )

    payload = (
        opcode.to_bytes(2, "big")
        + data
    )

    return (
        b"\x00\x04"
        + len(payload).to_bytes(4, "big")
        + payload
    )


def extract_plain_frames(buffer: bytearray) -> list[bytes]:
    """
    从连续明文流中提取完整帧。

    type 1:
        00 01 + opcode(2, big-endian)
        + payload_length(4, big-endian)
        + payload

        总长度 = 8 + payload_length

    type 2:
        00 02 + opcode(2, big-endian)
        + payload_length(4, big-endian)
        + repeated record payload

        总长度 = 8 + payload_length

    type 4:
        00 04 + payload_length(4, big-endian)
        + opcode(2, big-endian) + data

        payload_length 已包含 opcode，
        总长度 = 6 + payload_length
    """

    frames: list[bytes] = []

    while True:
        if len(buffer) < 2:
            break

        frame_type = int.from_bytes(buffer[0:2], "big")

        if frame_type in (1, 2):
            if len(buffer) < 8:
                break

            payload_length = int.from_bytes(
                buffer[4:8],
                "big",
            )
            total_length = 8 + payload_length

        elif frame_type == 4:
            if len(buffer) < 6:
                break

            payload_length = int.from_bytes(
                buffer[2:6],
                "big",
            )
            total_length = 6 + payload_length

        else:
            print(
                f"[{now()}] Unknown plaintext frame type "
                f"0x{frame_type:04X}; clearing stream buffer"
            )
            dump(
                "Unparsed plaintext stream",
                bytes(buffer),
            )
            buffer.clear()
            break

        if payload_length > 0x100000:
            print(
                f"[{now()}] Implausible payload length "
                f"{payload_length}; clearing stream buffer"
            )
            buffer.clear()
            break

        if len(buffer) < total_length:
            break

        frames.append(bytes(buffer[:total_length]))
        del buffer[:total_length]

    return frames


def describe_plain_frame(
    frame: bytes,
    frame_number: int,
) -> tuple[int, int, bytes]:
    frame_type = int.from_bytes(frame[0:2], "big")

    if frame_type in (1, 2):
        opcode = int.from_bytes(frame[2:4], "big")
        payload_length = int.from_bytes(frame[4:8], "big")
        payload = frame[8:]

    elif frame_type == 4:
        payload_length = int.from_bytes(frame[2:6], "big")
        opcode = int.from_bytes(frame[6:8], "big")
        payload = frame[8:]

    else:
        raise ValueError(
            f"Unsupported frame type 0x{frame_type:04X}"
        )

    print(
        f"[{now()}] Parsed frame #{frame_number}: "
        f"type={frame_type}, "
        f"opcode=0x{opcode:04X}, "
        f"payload_length={payload_length}, "
        f"total_length={len(frame)}"
    )

    dump(
        f"Parsed frame #{frame_number} payload",
        payload,
    )

    if opcode == 0x8012 and len(payload) == 2:
        print(
            f"[{now()}] 0x8012 value: "
            f"{int.from_bytes(payload, 'big')}"
        )

    elif opcode == 0x8015:
        if len(payload) >= 2:
            print(
                f"[{now()}] 0x8015 uint16 field: "
                f"{int.from_bytes(payload[0:2], 'big')}"
            )
        if len(payload) >= 6:
            print(
                f"[{now()}] 0x8015 uint32 field: "
                f"{int.from_bytes(payload[2:6], 'big')}"
            )

    return frame_type, opcode, payload
