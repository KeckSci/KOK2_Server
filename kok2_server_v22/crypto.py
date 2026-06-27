# =============================================================================
# KOK2 CRYPTO MODULE
#
# Extracted from the confirmed working legacy server.
# =============================================================================

import datetime
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


TABLE_FILE_OFFSET = 0x1F6F40
TABLE_SIZE = 0x10000

CLIENT_INITIAL_STATE = 0x48
CLIENT_TABLE_ROW = 0x08

KNOWN_SELECTION_PLAIN = bytes.fromhex(
    "00 01 90 06 00 00 00 02 00 01"
)


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


@dataclass
class StatefulCryptoContext:
    """
    连接建立后的滚动加密状态。

    state:
        当前滚动状态字节。

    table_row:
        查表行编号。
    """

    state: int
    table_row: int

    def validate(self) -> None:
        if not 0 <= self.state <= 0xFF:
            raise ValueError(
                f"state 超出范围：0x{self.state:X}"
            )

        if not 0 <= self.table_row < 10:
            raise ValueError(
                f"table_row 必须在 0 到 9 之间，"
                f"当前为 0x{self.table_row:X}"
            )


def load_crypto_table(gkk2_path: Path) -> bytes:
    if not gkk2_path.exists():
        raise FileNotFoundError(
            f"没有找到 GKK2.exe：{gkk2_path}"
        )

    exe_data = gkk2_path.read_bytes()
    table_end = TABLE_FILE_OFFSET + TABLE_SIZE

    if len(exe_data) < table_end:
        raise RuntimeError(
            "GKK2.exe 文件长度不足，无法读取完整加密表。\n"
            f"当前文件长度：0x{len(exe_data):X}\n"
            f"至少需要：0x{table_end:X}"
        )

    table = exe_data[TABLE_FILE_OFFSET:table_end]

    if len(table) != TABLE_SIZE:
        raise RuntimeError(
            f"加密表长度错误：{len(table)}，"
            f"预期：{TABLE_SIZE}"
        )

    print(
        f"[{now()}] Loaded crypto table "
        f"from file offset 0x{TABLE_FILE_OFFSET:X}"
    )

    return table


def build_inverse_tables(
    table: bytes,
) -> list[list[int]]:
    """
    只为实际使用的前 10 个加密表行建立逆表。

    初始包使用：
        table_row = key1 % 10

    因此有效行范围是：
        0 到 9
    """

    row_count = 10
    required_size = row_count * 256

    if len(table) < required_size:
        raise RuntimeError(
            f"加密表只有 {len(table)} 字节，"
            f"至少需要 {required_size} 字节"
        )

    inverse_tables: list[list[int]] = []

    for row in range(row_count):
        inverse = [-1] * 256
        row_start = row << 8

        for intermediate in range(256):
            cipher_byte = table[
                row_start + intermediate
            ]

            if inverse[cipher_byte] != -1:
                raise RuntimeError(
                    f"加密表第 {row} 行不是一一映射："
                    f"密文字节 0x{cipher_byte:02X} 重复"
                )

            inverse[cipher_byte] = intermediate

        if any(value < 0 for value in inverse):
            raise RuntimeError(
                f"加密表第 {row} 行逆表不完整"
            )

        inverse_tables.append(inverse)

    return inverse_tables


def encrypt_original_byte(
    original_byte: int,
    previous_state: int,
) -> int:
    """
    根据前一状态将原始字节变换为 intermediate。
    """

    original_byte &= 0xFF
    previous_state &= 0xFF

    if previous_state & (
        1 << (previous_state & 7)
    ):
        return original_byte ^ previous_state

    if previous_state & (
        1 << (previous_state & 3)
    ):
        return (
            original_byte + previous_state
        ) & 0xFF

    return (
        original_byte - previous_state
    ) & 0xFF


def decrypt_intermediate_byte(
    intermediate: int,
    previous_state: int,
) -> int:
    """
    将 intermediate 逆变换为原始字节。
    """

    intermediate &= 0xFF
    previous_state &= 0xFF

    if previous_state & (
        1 << (previous_state & 7)
    ):
        return intermediate ^ previous_state

    if previous_state & (
        1 << (previous_state & 3)
    ):
        return (
            intermediate - previous_state
        ) & 0xFF

    return (
        intermediate + previous_state
    ) & 0xFF


def encode_initial_packet(
    plain: bytes,
    table: bytes,
    key1: Optional[int] = None,
    key2: Optional[int] = None,
) -> bytes:
    """
    初始独立包格式：

        key1 + encrypted(key2 + plain)

    该格式用于我们向客户端发送 0x7009 响应。
    """

    if key1 is None:
        key1 = random.randint(1, 255)

    if key2 is None:
        key2 = random.randint(1, 255)

    if not 1 <= key1 <= 255:
        raise ValueError("key1 必须在 1 到 255 之间")

    if not 1 <= key2 <= 255:
        raise ValueError("key2 必须在 1 到 255 之间")

    table_row = key1 % 10
    state = key1

    body = bytes([key2]) + plain
    encrypted_body = bytearray()

    for original_byte in body:
        intermediate = encrypt_original_byte(
            original_byte=original_byte,
            previous_state=state,
        )

        state = intermediate

        table_index = (
            table_row << 8
        ) + intermediate

        encrypted_body.append(
            table[table_index]
        )

    packet = (
        bytes([key1])
        + bytes(encrypted_body)
    )

    print(
        f"[{now()}] Server encryption keys: "
        f"key1=0x{key1:02X}, "
        f"key2=0x{key2:02X}, "
        f"table_row={table_row}"
    )

    return packet


def encode_initial_packet_with_context(
    plain: bytes,
    table: bytes,
    key1: Optional[int] = None,
    key2: Optional[int] = None,
) -> tuple[bytes, int, int, StatefulCryptoContext]:
    """
    与 encode_initial_packet 相同，但额外返回服务端发送方向的后续滚动上下文。

    返回：
        packet
        key1
        key2
        StatefulCryptoContext(final_state, table_row)
    """

    if key1 is None:
        key1 = random.randint(1, 255)

    if key2 is None:
        key2 = random.randint(1, 255)

    if not 1 <= key1 <= 255:
        raise ValueError("key1 必须在 1 到 255 之间")

    if not 1 <= key2 <= 255:
        raise ValueError("key2 必须在 1 到 255 之间")

    table_row = key1 % 10
    state = key1
    body = bytes([key2]) + plain
    encrypted_body = bytearray()

    for original_byte in body:
        intermediate = encrypt_original_byte(
            original_byte=original_byte,
            previous_state=state,
        )
        state = intermediate
        encrypted_body.append(
            table[(table_row << 8) + intermediate]
        )

    packet = bytes([key1]) + bytes(encrypted_body)

    context = StatefulCryptoContext(
        state=state,
        table_row=table_row,
    )

    print(
        f"[{now()}] Server encryption keys: "
        f"key1=0x{key1:02X}, "
        f"key2=0x{key2:02X}, "
        f"table_row={table_row}"
    )

    return packet, key1, key2, context


def decode_initial_packet(
    packet: bytes,
    inverse_tables: list[list[int]],
) -> tuple[int, int, bytes, int]:
    """
    解密初始独立包：

        key1 + encrypted(key2 + plain)
    """

    if len(packet) < 2:
        raise ValueError(
            "初始加密包长度不能小于 2 字节"
        )

    key1 = packet[0]
    table_row = key1 % 10
    state = key1

    inverse = inverse_tables[table_row]
    decoded_body = bytearray()

    for cipher_byte in packet[1:]:
        intermediate = inverse[cipher_byte]

        if intermediate < 0:
            raise RuntimeError(
                f"无法逆查密文字节 "
                f"0x{cipher_byte:02X}"
            )

        original_byte = decrypt_intermediate_byte(
            intermediate=intermediate,
            previous_state=state,
        )

        decoded_body.append(original_byte)
        state = intermediate

    if not decoded_body:
        raise RuntimeError(
            "初始包解密后没有 key2"
        )

    key2 = decoded_body[0]
    plain = bytes(decoded_body[1:])

    # state 此时是初始登录包全部解密完成后的最终滚动状态。
    # 客户端在同一连接中发送后续状态式数据时，应从这里继续。
    return key1, key2, plain, state


def decode_stateful_packet(
    ciphertext: bytes,
    inverse_tables: list[list[int]],
    context: StatefulCryptoContext,
) -> bytes:
    """
    解密连接建立后的状态式数据包。

    网络格式：

        encrypted(plain)

    不附加 key1/key2，密文长度等于明文长度。

    每个字节处理完成后：
        context.state = intermediate
    """

    context.validate()

    inverse = inverse_tables[
        context.table_row
    ]

    plaintext = bytearray()

    for cipher_byte in ciphertext:
        intermediate = inverse[cipher_byte]

        if intermediate < 0:
            raise RuntimeError(
                f"无法逆查密文字节 "
                f"0x{cipher_byte:02X}"
            )

        original_byte = decrypt_intermediate_byte(
            intermediate=intermediate,
            previous_state=context.state,
        )

        plaintext.append(original_byte)

        # 更新连接滚动状态
        context.state = intermediate

    return bytes(plaintext)


def encode_stateful_packet(
    plain: bytes,
    table: bytes,
    context: StatefulCryptoContext,
) -> bytes:
    """
    加密连接建立后的状态式数据包。

    返回密文长度与明文长度相同。
    """

    context.validate()

    ciphertext = bytearray()
    row_start = context.table_row << 8

    for original_byte in plain:
        intermediate = encrypt_original_byte(
            original_byte=original_byte,
            previous_state=context.state,
        )

        cipher_byte = table[
            row_start + intermediate
        ]

        ciphertext.append(cipher_byte)

        # 更新连接滚动状态
        context.state = intermediate

    return bytes(ciphertext)


def decode_stateful_candidate(
    ciphertext: bytes,
    inverse_tables: list[list[int]],
    initial_state: int,
    table_row: int,
    update_mode: str = "intermediate",
) -> tuple[bytes, int]:
    """
    使用指定假设解密，但不修改正式连接上下文。

    update_mode:
        intermediate  每字节后以逆表所得 intermediate 更新状态
        cipher        每字节后以密文字节更新状态
        plain         每字节后以明文字节更新状态
    """

    state = initial_state & 0xFF
    inverse = inverse_tables[table_row]
    plaintext = bytearray()

    for cipher_byte in ciphertext:
        intermediate = inverse[cipher_byte]

        original_byte = decrypt_intermediate_byte(
            intermediate=intermediate,
            previous_state=state,
        )
        plaintext.append(original_byte)

        if update_mode == "intermediate":
            state = intermediate
        elif update_mode == "cipher":
            state = cipher_byte
        elif update_mode == "plain":
            state = original_byte
        else:
            raise ValueError(
                f"未知 update_mode：{update_mode}"
            )

    return bytes(plaintext), state


def find_stateful_context_candidates(
    ciphertext: bytes,
    inverse_tables: list[list[int]],
    expected_plaintext: bytes,
) -> list[tuple[int, int, str, int]]:
    """
    穷举前 10 行、256 个初始状态以及三种状态更新假设。

    返回：
        (initial_state, table_row, update_mode, final_state)
    """

    matches: list[tuple[int, int, str, int]] = []

    if len(ciphertext) != len(expected_plaintext):
        return matches

    for table_row in range(10):
        for initial_state in range(256):
            for update_mode in (
                "intermediate",
                "cipher",
                "plain",
            ):
                decoded, final_state = (
                    decode_stateful_candidate(
                        ciphertext=ciphertext,
                        inverse_tables=inverse_tables,
                        initial_state=initial_state,
                        table_row=table_row,
                        update_mode=update_mode,
                    )
                )

                if decoded == expected_plaintext:
                    matches.append(
                        (
                            initial_state,
                            table_row,
                            update_mode,
                            final_state,
                        )
                    )

    return matches


def print_stateful_candidates(
    ciphertext: bytes,
    inverse_tables: list[list[int]],
    expected_plaintext: bytes,
) -> list[tuple[int, int, str, int]]:
    matches = find_stateful_context_candidates(
        ciphertext=ciphertext,
        inverse_tables=inverse_tables,
        expected_plaintext=expected_plaintext,
    )

    if not matches:
        print(
            f"[{now()}] No exact stateful context candidate "
            f"matched the expected plaintext."
        )
        print(
            f"[{now()}] This usually means at least one of "
            f"the following assumptions is wrong:"
        )
        print(
            "  1. the captured ciphertext starts at a packet boundary;"
        )
        print(
            "  2. the expected plaintext is correct;"
        )
        print(
            "  3. the table row is constant for the whole packet;"
        )
        print(
            "  4. the byte transform has been reconstructed correctly;"
        )
        return matches

    print(
        f"[{now()}] Found {len(matches)} exact "
        f"stateful context candidate(s):"
    )

    for (
        initial_state,
        table_row,
        update_mode,
        final_state,
    ) in matches[:32]:
        print(
            "  "
            f"state=0x{initial_state:02X}, "
            f"row=0x{table_row:02X}, "
            f"update={update_mode}, "
            f"final_state=0x{final_state:02X}"
        )

    if len(matches) > 32:
        print(
            f"  ... {len(matches) - 32} more candidate(s)"
        )

    return matches


def verify_known_stateful_vector(
    inverse_tables: list[list[int]],
) -> None:
    """
    验证刚刚通过 x32dbg 得到的已知数据：

    state = CLIENT_INITIAL_STATE
    row   = CLIENT_TABLE_ROW

    ciphertext:
        42 97 89 BB BB BB BB E1 E1 E0

    expected plaintext:
        00 01 90 06 00 00 00 02 00 01
    """

    known_ciphertext = bytes.fromhex(
        "42 97 89 BB BB BB BB E1 E1 E0"
    )

    expected_plaintext = bytes.fromhex(
        "00 01 90 06 00 00 00 02 00 01"
    )

    test_context = StatefulCryptoContext(
        state=CLIENT_INITIAL_STATE,
        table_row=CLIENT_TABLE_ROW,
    )

    decoded = decode_stateful_packet(
        ciphertext=known_ciphertext,
        inverse_tables=inverse_tables,
        context=test_context,
    )

    print(
        f"[{now()}] Verifying known stateful vector"
    )

    dump(
        "Known ciphertext",
        known_ciphertext,
    )

    dump(
        "Decoded known plaintext",
        decoded,
    )

    if decoded != expected_plaintext:
        print(
            f"[{now()}] WARNING: configured stateful context "
            f"did not match the known vector."
        )
        print(
            f"[{now()}] Actual:   {decoded.hex(' ')}"
        )
        print(
            f"[{now()}] Expected: "
            f"{expected_plaintext.hex(' ')}"
        )

        matches = print_stateful_candidates(
            ciphertext=known_ciphertext,
            inverse_tables=inverse_tables,
            expected_plaintext=expected_plaintext,
        )

        print(
            f"[{now()}] Verification is now diagnostic only; "
            f"the server will continue to start."
        )
        return

    print(
        f"[{now()}] Stateful crypto verification passed"
    )

    print(
        f"[{now()}] Known vector final state: "
        f"0x{test_context.state:02X}"
    )
