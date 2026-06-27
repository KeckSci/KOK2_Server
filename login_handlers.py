from __future__ import annotations

import time
from dataclasses import dataclass
import re
from typing import Callable

from dispatcher import LOGIN_DISPATCHER, PacketContext
from database import is_safe_character_name
from protocol import TEXT_ENCODING, build_type1_plain, build_type2_plain, c_string


@dataclass(frozen=True)
class CreateRoleRequest:
    name: str
    gender_selector: int
    profession_selector: int
    hair_model: int
    feature_model: int
    profession_model: int
    country_selector: int
    gender: int
    profession: int
    body: int
    hair: int
    head: int
    hand_r: int
    hand_l: int
    pants: int
    foot_r: int
    foot_l: int
    raw_tail: bytes


SendType4Callable = Callable[[int, bytes, str], None]
SendRawStatefulCallable = Callable[[bytes, str], None]
SendServerListCallable = Callable[[], None]
RoleSelectCallable = Callable[[str], bool]
AccountLoginCallable = Callable[[str, str], tuple[bool, str, list[str], set[str]]]
RoleCreateCallable = Callable[
    [CreateRoleRequest],
    tuple[bool, str, list[str], set[str]],
]
RoleDeleteCallable = Callable[
    [str],
    tuple[bool, str, list[str], set[str]],
]


# Client 0x7000 uses compact login status codes. Status 2 is the
# password-error branch and displays message/system/index0501.
LOGIN_STATUS_ACCOUNT_PASSWORD_ERROR = 2


@dataclass
class LoginHandlerRuntime:
    """
    Runtime dependencies supplied by server_legacy.py.

    Stable responsibilities:
        client 0x9000 -> bind database account using username/password
        client 0x9005 -> server 0x7002 account role list
        client 0x9002 -> server 0x7003 ChangeConnect

    Confirmed role-list transport:
        0x7002 must be sent as a type-2 multi-record frame:
            00 02 + opcode + payload_length + records

        Each logical 0x7002 record keeps the confirmed serialized layout:
            uint16(1) + role-token C string

        Client-side confirmation at 004C37A0:
            [ESP+08] = record_count
            [ESP+0C] = decoded 8-byte record array
    """

    send_type4: SendType4Callable
    send_raw_stateful: SendRawStatefulCallable
    bind_account_login: AccountLoginCallable
    role_tokens: list[str]
    role_names: set[str]
    role_list_delay_seconds: float
    send_server_list: SendServerListCallable | None = None
    select_role: RoleSelectCallable | None = None
    create_role: RoleCreateCallable | None = None
    delete_role: RoleDeleteCallable | None = None

    authenticated: bool = False
    account_username: str = "<not-authenticated>"
    enter_game_delay_seconds: float = 0.20
    enter_game_status: int = 0
    enter_game_ticket: bytes = b"\x00"
    enter_game_connect_info: str = "127.0.0.1#10100"


_RUNTIME: LoginHandlerRuntime | None = None


def configure_login_handlers(runtime: LoginHandlerRuntime) -> None:
    global _RUNTIME
    _RUNTIME = runtime

    print(
        "[login-handler] Runtime configured: "
        f"authenticated={runtime.authenticated}, "
        f"account={runtime.account_username!r}, "
        f"role_count={len(runtime.role_tokens)}, "
        f"role_names={sorted(runtime.role_names)!r}, "
        f"role_list_delay={runtime.role_list_delay_seconds:.2f}s, "
        f"enter_game_delay={runtime.enter_game_delay_seconds:.2f}s, "
        f"connect_info={runtime.enter_game_connect_info!r}, "
        f"create_role_handler={runtime.create_role is not None}, "
        f"delete_role_handler={runtime.delete_role is not None}"
    )


def require_runtime() -> LoginHandlerRuntime:
    if _RUNTIME is None:
        raise RuntimeError(
            "Login handlers were used before configure_login_handlers()"
        )
    return _RUNTIME


def _format_hex(data: bytes, width: int = 16) -> str:
    if not data:
        return "<empty>"

    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = chunk.hex(" ")
        ascii_part = "".join(
            chr(value) if 32 <= value <= 126 else "."
            for value in chunk
        )
        lines.append(f"  +0x{offset:04X}: {hex_part:<47}  {ascii_part}")
    return "\n".join(lines)


def split_c_strings(data: bytes) -> list[str]:
    strings: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            strings.append(text)

    # First keep the confirmed C-string interpretation.
    for part in data.split(b"\x00"):
        if not part:
            continue
        try:
            text = part.decode("ascii")
        except UnicodeDecodeError:
            continue
        if text and all(32 <= ord(ch) <= 126 for ch in text):
            add(text)

    # Some serializers prefix strings with binary length/metadata bytes. In
    # that case a strict NUL split can drop an otherwise valid account string.
    for match in re.finditer(rb"[ -~]+", data):
        try:
            text = match.group(0).decode("ascii")
        except UnicodeDecodeError:
            continue
        add(text)

    return strings


def split_c_strings_strict(data: bytes) -> list[str]:
    strings: list[str] = []
    for part in data.split(b"\x00"):
        if not part:
            continue
        try:
            text = part.decode("ascii")
        except UnicodeDecodeError:
            continue
        if text and all(32 <= ord(ch) <= 126 for ch in text):
            strings.append(text)
    return strings


def decode_client_text(data: bytes) -> str:
    raw = data.rstrip(b"\x00")
    for encoding in ("ascii", "cp936", "gbk", "cp950", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")




CREATE_ROLE_NAME_BUFFER_SIZE = 11
CREATE_ROLE_TAIL_SIZE = 17
CREATE_ROLE_PAYLOAD_SIZE = CREATE_ROLE_NAME_BUFFER_SIZE + CREATE_ROLE_TAIL_SIZE


# 0x9003 confirmed layout from packet captures:
#   00-0A: fixed 11-byte CP936 role-name buffer. It may not be NUL-terminated;
#          bytes after the typed name can contain stale client-side padding.
#   0B-0C: gender selector, uint16 BE: 0=male, 1=female
#   0D-10: unknown/reserved, currently zero
#   11:    hair style selector: 1..3
#   12:    hair color selector: 0..5, corresponding to UI colors 1..6
#   13-14: unknown/reserved, currently zero
#   15:    feature/head selector: 1..5
#   16:    unknown/reserved, currently zero
#   17-1A: profession id, uint32 BE: 1000 warrior, 2000 mage, 3000 priest
#   1B:    country selector, currently zero/none
#
# CP936 Chinese characters use two bytes, so the 11-byte limit is a byte limit,
# not a Python-character limit.  A name can therefore contain up to five
# ordinary Chinese characters, or five Chinese characters plus one ASCII byte.

def is_safe_role_name(name: str) -> bool:
    return is_safe_character_name(name)


def require_safe_role_name(
    name: str,
    source: str,
    max_encoded_bytes: int = 16,
) -> str:
    clean = name.strip()
    if not is_safe_role_name(clean):
        raise ValueError(
            f"unsafe CP936 role name from {source}: {clean!r}; "
            "expected CP936 letters/numbers/underscore without separators"
        )
    encoded = clean.encode(TEXT_ENCODING, errors="strict")
    if len(encoded) > max_encoded_bytes:
        raise ValueError(
            f"role name from {source} exceeds the allowed CP936 byte length: "
            f"limit={max_encoded_bytes}, encoded_length={len(encoded)}, "
            f"name={clean!r}"
        )
    return clean


def _consume_safe_cp936_name_prefix(raw: bytes) -> bytes:
    """Consume a safe CP936 letter/number prefix from a fixed binary buffer.

    Old client builds do not reliably clear bytes after the typed name.  We
    therefore cannot decode all 11 bytes blindly.  Consume ASCII name bytes or
    complete two-byte CP936 characters and stop at NUL, malformed bytes,
    punctuation, control data, or stale padding.
    """
    result = bytearray()
    offset = 0
    while offset < len(raw):
        first = raw[offset]
        if first == 0:
            break

        if first < 0x80:
            candidate = bytes((first,))
            width = 1
        else:
            if offset + 1 >= len(raw):
                break
            candidate = raw[offset:offset + 2]
            width = 2

        try:
            text = candidate.decode(TEXT_ENCODING, errors="strict")
        except UnicodeDecodeError:
            break

        if len(text) != 1 or not is_safe_character_name(text):
            break

        result.extend(candidate)
        offset += width

    return bytes(result)


def read_fixed_name_buffer_9003(name_buffer: bytes) -> str:
    """Extract one ASCII/Chinese name from the fixed 11-byte 0x9003 buffer."""
    if len(name_buffer) != CREATE_ROLE_NAME_BUFFER_SIZE:
        raise ValueError(
            f"0x9003 name buffer must be {CREATE_ROLE_NAME_BUFFER_SIZE} bytes, "
            f"got {len(name_buffer)}"
        )

    prefix = _consume_safe_cp936_name_prefix(name_buffer)
    if not prefix:
        raise ValueError(
            "0x9003 fixed name buffer does not start with a safe CP936 name"
        )

    try:
        decoded = prefix.decode(TEXT_ENCODING, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"0x9003 role name is not valid CP936: {exc}") from exc

    return require_safe_role_name(
        decoded,
        "fixed 11-byte name buffer",
        max_encoded_bytes=CREATE_ROLE_NAME_BUFFER_SIZE,
    )


def decode_role_name_c_string(payload: bytes, source: str) -> str:
    """Decode a role-name C string from client packets using CP936."""
    raw_name = payload.split(b"\x00", 1)[0]
    try:
        role_name = raw_name.decode(TEXT_ENCODING, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source} role name is not CP936: {exc}") from exc
    return require_safe_role_name(role_name, source)


def clamp_selector(value: int, valid_values: set[int], fallback: int, label: str) -> int:
    if value in valid_values:
        return value
    print(
        f"[login-handler] 0x9003 {label} selector is implausible; "
        f"using fallback {fallback}: raw={value}"
    )
    return fallback


def derive_level1_role_token_parts(
    gender_selector: int,
    hair_style: int,
    hair_color: int,
    feature_selector: int,
) -> tuple[int, int, int, int, int, int, int, int]:
    """
    Build the real created level-1 role appearance.

    Confirmed user testing:
        hair = hair_style * 256 + hair_color
        head = feature_selector * 256

    Confirmed correction:
        Profession does not affect the final level-1 beginner appearance.  The
        profession-specific values in the client preview function are only the
        create-screen concept/costume models.
    """
    token_gender = 2 if gender_selector == 1 else 1

    body = 256
    hair = hair_style * 256 + hair_color
    head = feature_selector * 256
    hand_r = 256
    hand_l = 256
    pants = 256
    foot_r = 256
    foot_l = 256

    return (
        token_gender,
        body,
        hair,
        head,
        hand_r,
        hand_l,
        pants,
        foot_r,
    )


def parse_create_role_fixed_payload_9003(payload: bytes) -> CreateRoleRequest:
    if len(payload) < CREATE_ROLE_PAYLOAD_SIZE:
        raise ValueError(
            "0x9003 fixed payload is too short: "
            f"expected at least {CREATE_ROLE_PAYLOAD_SIZE}, got {len(payload)}"
        )

    name_buffer = payload[:CREATE_ROLE_NAME_BUFFER_SIZE]
    tail = payload[
        CREATE_ROLE_NAME_BUFFER_SIZE:CREATE_ROLE_PAYLOAD_SIZE
    ]

    name = read_fixed_name_buffer_9003(name_buffer)
    print(
        "[login-handler] 0x9003 decoded CP936 role-name buffer: "
        f"raw={name_buffer.hex(' ')}, "
        f"encoded_length={len(name.encode(TEXT_ENCODING))}, "
        f"name={name!r}"
    )

    raw_gender_selector = int.from_bytes(tail[0:2], "big")
    unknown_00 = int.from_bytes(tail[2:6], "big")
    raw_hair_style = tail[6]
    raw_hair_color = tail[7]
    unknown_01 = int.from_bytes(tail[8:10], "big")
    raw_feature_selector = tail[10]
    unknown_02 = tail[11]
    raw_profession_model = int.from_bytes(tail[12:16], "big")
    country_selector = tail[16]

    gender_selector = clamp_selector(
        raw_gender_selector,
        {0, 1},
        0,
        "gender",
    )
    hair_style = clamp_selector(
        raw_hair_style,
        {1, 2, 3},
        1,
        "hair_style",
    )
    hair_color = clamp_selector(
        raw_hair_color,
        {0, 1, 2, 3, 4, 5},
        0,
        "hair_color",
    )
    feature_selector = clamp_selector(
        raw_feature_selector,
        {1, 2, 3, 4, 5},
        1,
        "feature",
    )
    profession_model = clamp_selector(
        raw_profession_model,
        {1000, 2000, 3000},
        1000,
        "profession",
    )

    (
        token_gender,
        body,
        hair,
        head,
        hand_r,
        hand_l,
        pants,
        foot_r,
    ) = derive_level1_role_token_parts(
        gender_selector=gender_selector,
        hair_style=hair_style,
        hair_color=hair_color,
        feature_selector=feature_selector,
    )

    if unknown_00 or unknown_01 or unknown_02:
        print(
            "[login-handler] 0x9003 reserved fields are non-zero: "
            f"unknown_00={unknown_00}, unknown_01={unknown_01}, "
            f"unknown_02={unknown_02}"
        )

    return CreateRoleRequest(
        name=name,
        gender_selector=gender_selector,
        profession_selector=profession_model,
        hair_model=hair,
        feature_model=head,
        profession_model=profession_model,
        country_selector=country_selector,
        gender=token_gender,
        profession=profession_model,
        body=body,
        hair=hair,
        head=head,
        hand_r=hand_r,
        hand_l=hand_l,
        pants=pants,
        foot_r=foot_r,
        foot_l=foot_r,
        raw_tail=tail,
    )


def find_safe_role_name_candidates_9003(payload: bytes) -> list[str]:
    """Conservative CP936 fallback for variant 0x9003 serializers."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw: bytes, source: str) -> None:
        prefix = _consume_safe_cp936_name_prefix(raw[:CREATE_ROLE_NAME_BUFFER_SIZE])
        if not prefix:
            return
        try:
            text = prefix.decode(TEXT_ENCODING, errors="strict")
            clean = require_safe_role_name(
                text,
                source,
                max_encoded_bytes=CREATE_ROLE_NAME_BUFFER_SIZE,
            )
        except (UnicodeDecodeError, ValueError):
            return
        if clean in seen:
            return
        seen.add(clean)
        candidates.append(clean)
        print(
            "[login-handler] 0x9003 safe CP936 name candidate: "
            f"{clean!r} from {source}"
        )

    add(payload, "payload-prefix")
    for index, part in enumerate(payload.split(b"\x00")):
        if part:
            add(part, f"c-string[{index}]")

    candidates.sort(
        key=lambda value: len(value.encode(TEXT_ENCODING, errors="strict")),
        reverse=True,
    )
    return candidates


def parse_create_role_payload_9003(payload: bytes) -> CreateRoleRequest:
    # Prefer the confirmed fixed 28-byte 0x9003 layout.
    if len(payload) >= CREATE_ROLE_PAYLOAD_SIZE:
        try:
            return parse_create_role_fixed_payload_9003(payload)
        except ValueError as exc:
            print(
                "[login-handler] 0x9003 fixed-layout parse failed; "
                f"trying fallback scan: {exc}"
            )

    candidates = find_safe_role_name_candidates_9003(payload)
    if not candidates:
        raise ValueError(
            "0x9003 could not find any safe CP936 role-name candidate in payload"
        )

    # If the fixed layout failed, keep creation conservative: use the name but
    # fall back to the stable male warrior beginner appearance.
    name = candidates[0]
    print(
        "[login-handler] 0x9003 using fallback scanned role name candidate: "
        f"name={name!r}, all_candidates={candidates!r}"
    )

    (
        token_gender,
        body,
        hair,
        head,
        hand_r,
        hand_l,
        pants,
        foot_r,
    ) = derive_level1_role_token_parts(
        gender_selector=0,
        hair_style=1,
        hair_color=0,
        feature_selector=1,
    )

    return CreateRoleRequest(
        name=name,
        gender_selector=0,
        profession_selector=1000,
        hair_model=hair,
        feature_model=head,
        profession_model=1000,
        country_selector=0,
        gender=token_gender,
        profession=1000,
        body=body,
        hair=hair,
        head=head,
        hand_r=hand_r,
        hand_l=hand_l,
        pants=pants,
        foot_r=foot_r,
        foot_l=foot_r,
        raw_tail=payload[-CREATE_ROLE_TAIL_SIZE:],
    )

def parse_login_payload_9000(payload: bytes) -> tuple[str | None, str | None]:
    """
    Parse client opcode 0x9000.

    Decompiled client function FUN_004CA7F0 copies two LPCSTR values into
    DAT_0067D490 and DAT_0067D494, then calls FUN_004C1710(..., 0x9000, 1,
    &DAT_0067D490). The outgoing opcode table entry for 0x9000 has a record
    size of 8, matching two string pointers. The serializer emits type-4
    strings as plain NUL-terminated C strings, so the normal wire payload is:

        username\0 password\0

    This parser keeps a conservative printable-string fallback because some
    clients may include launcher-side decoration before the two strings.
    """
    strings = split_c_strings(payload)
    candidates = parse_login_payload_9000_candidates(payload)
    if candidates:
        return candidates[0]
    return None, None


def parse_login_payload_9000_candidates(payload: bytes) -> list[tuple[str, str]]:
    """
    Return plausible username/password pairs from a 0x9000 payload.

    The confirmed base layout is two C strings, but some launcher/client paths
    can include extra printable fields around them. Try adjacent pairs first,
    then broader ordered pairs; the database check decides which pair is real.
    """
    strings = split_c_strings(payload)
    if len(strings) < 2:
        return []

    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(username: str, password: str) -> None:
        pairs = (
            (username, password),
            (username.strip(), password.strip()),
        )
        for pair in pairs:
            if pair[0] and pair[1] and pair not in seen:
                seen.add(pair)
                candidates.append(pair)

    for index in range(len(strings) - 1):
        add(strings[index], strings[index + 1])

    for username_index, username in enumerate(strings):
        for password_index, password in enumerate(strings):
            if username_index != password_index:
                add(username, password)

    return candidates


def is_blank_login_payload(payload: bytes) -> bool:
    """Return True when opcode 0x9000 carries no visible username/password.

    split_c_strings intentionally drops empty C strings, so a real blank login
    normally has no non-empty printable strings.  This function is used only to
    accept an explicitly blank login payload; non-empty wrong credentials must
    not fall back to the blank account.
    """
    return not any(text.strip() for text in split_c_strings(payload))


def bind_account_login_from_payload(
    payload: bytes,
    bind_account_login: AccountLoginCallable,
) -> tuple[bool, str, list[str], set[str], tuple[str, str] | None, list[str]]:
    strings = split_c_strings(payload)
    candidates = parse_login_payload_9000_candidates(payload)

    if not candidates:
        if is_blank_login_payload(payload):
            ok, account_username, role_tokens, role_names = bind_account_login("", "")
            if ok:
                return ok, account_username, role_tokens, role_names, ("", ""), strings
        return False, "<parse-failed>", [], set(), None, strings

    last_result: tuple[bool, str, list[str], set[str]] = (
        False,
        "<no-candidate-matched>",
        [],
        set(),
    )

    for username, password in candidates:
        result = bind_account_login(username, password)
        ok, account_username, role_tokens, role_names = result
        if ok:
            return (
                ok,
                account_username,
                role_tokens,
                role_names,
                (username, password),
                strings,
            )
        last_result = result

    # Important: do not fall back to the blank account when the client supplied
    # non-empty credentials.  This prevents old credentials such as
    # Non-empty credentials must never fall back to a blank account.
    ok, account_username, role_tokens, role_names = last_result
    return ok, account_username, role_tokens, role_names, None, strings


def build_role_list_record(token: str) -> bytes:
    """
    Build one confirmed 0x7002 role-list record.

    Network serialized record:
        uint16 field00 = 1
        C string role_token

    Client decoded record at 004C37A0:
        DWORD 00000001
        char* role_token
    """
    return b"".join((
        (1).to_bytes(2, "big"),
        c_string(token),
    ))


def send_login_status(runtime: LoginHandlerRuntime, status_code: int, label: str) -> None:
    payload = status_code.to_bytes(2, "big")
    plain = build_type1_plain(0x7000, payload)

    print(
        "[login-handler] Preparing 0x7000 login status response: "
        f"status_code={status_code}, plain_length={len(plain)}\n"
        f"[login-handler] 0x7000 type-1 plaintext:\n{_format_hex(plain)}"
    )

    runtime.send_raw_stateful(
        plain,
        label,
    )


def send_role_list_response(
    runtime: LoginHandlerRuntime,
    label: str,
    delay_seconds: float | None = None,
) -> None:
    logical_records = [
        build_role_list_record(token)
        for token in runtime.role_tokens
    ]
    role_names = [
        token.split("#", 1)[0]
        for token in runtime.role_tokens
    ]

    # The client does not enter the role-selection/create-role state when it
    # receives a completely empty type-2 0x7002 frame (payload_length == 0).
    # The confirmed zero-character representation is one descriptor-valid
    # placeholder record:
    #
    #     uint16 field00 = 1
    #     empty C string = 00
    #
    # Network payload: 00 01 00
    #
    # This is a wire-level empty slot only.  It must never be added to
    # runtime.role_tokens or runtime.role_names and therefore cannot be
    # selected as a real character.
    empty_slot = not logical_records
    wire_records = (
        logical_records
        if logical_records
        else [build_role_list_record("")]
    )
    plain = build_type2_plain(0x7002, wire_records)

    print(
        "[login-handler] Preparing stable type-2 0x7002 role-list response: "
        f"account={runtime.account_username!r}, "
        f"role_count={len(logical_records)}, "
        f"wire_record_count={len(wire_records)}, "
        f"empty_slot={empty_slot}, "
        f"role_names={role_names!r}, "
        f"record_lengths={[len(record) for record in wire_records]!r}, "
        f"plain_length={len(plain)}\n"
        f"[login-handler] 0x7002 type-2 plaintext:\n{_format_hex(plain)}"
    )

    effective_delay = (
        runtime.role_list_delay_seconds
        if delay_seconds is None
        else delay_seconds
    )

    if effective_delay > 0:
        print(
            "[login-handler] Waiting "
            f"{effective_delay:.2f}s "
            "before 0x7002 role-list response"
        )
        time.sleep(effective_delay)

    runtime.send_raw_stateful(
        plain,
        label,
    )

    print("[login-handler] 0x7002 role-list response sent")


@LOGIN_DISPATCHER.register(frame_type=1, opcode=0x9000)
def handle_account_login(context: PacketContext) -> None:
    runtime = require_runtime()

    candidates = parse_login_payload_9000_candidates(context.payload)
    print(
        "[login-handler] 0x9000 account-login request: "
        f"payload_length={len(context.payload)}, "
        f"printable_strings={split_c_strings(context.payload)!r}, "
        f"candidate_count={len(candidates)}"
    )

    if not candidates and not is_blank_login_payload(context.payload):
        runtime.authenticated = False
        runtime.account_username = "<parse-failed>"
        runtime.role_tokens = []
        runtime.role_names = set()
        print(
            "[login-handler] 0x9000 parse failed; this connection will not "
            "receive an account role list. Payload:\n"
            f"{_format_hex(context.payload)}"
        )
        send_login_status(
            runtime,
            LOGIN_STATUS_ACCOUNT_PASSWORD_ERROR,
            "0x7000 login rejected: username/password parse failed",
        )
        return

    (
        ok,
        account_username,
        role_tokens,
        role_names,
        selected_candidate,
        _strings,
    ) = bind_account_login_from_payload(
        context.payload,
        runtime.bind_account_login,
    )
    runtime.authenticated = ok
    runtime.account_username = account_username
    runtime.role_tokens = role_tokens
    runtime.role_names = role_names
    selected_username = selected_candidate[0] if selected_candidate else None

    print(
        "[login-handler] 0x9000 account bind result: "
        f"ok={ok}, account={account_username!r}, "
        f"selected_username={selected_username!r}, "
        f"role_count={len(role_tokens)}, role_names={sorted(role_names)!r}"
    )

    if not ok:
        send_login_status(
            runtime,
            LOGIN_STATUS_ACCOUNT_PASSWORD_ERROR,
            "0x7000 login rejected: account/password mismatch",
        )
        return

    if runtime.send_server_list is not None:
        runtime.send_server_list()


@LOGIN_DISPATCHER.register(frame_type=1, opcode=0x9006)
def observe_server_selection(context: PacketContext) -> None:
    print(
        "[login-handler] 0x9006 server-selection request: "
        f"payload={context.payload.hex(' ')}"
    )


@LOGIN_DISPATCHER.register(frame_type=1, opcode=0x9005)
def handle_role_list_request(context: PacketContext) -> None:
    runtime = require_runtime()

    print(
        "[login-handler] 0x9005 role/resource request: "
        f"payload_length={len(context.payload)}, "
        f"authenticated={runtime.authenticated}, "
        f"account={runtime.account_username!r}"
    )

    if not runtime.authenticated:
        print(
            "[login-handler] Refusing 0x9005 because no valid account is "
            "bound to this connection."
        )
        send_login_status(
            runtime,
            LOGIN_STATUS_ACCOUNT_PASSWORD_ERROR,
            "0x7000 login rejected: role-list before account login",
        )
        return

    send_role_list_response(
        runtime,
        "Stable type-2 0x7002 account role-list response",
    )


@LOGIN_DISPATCHER.register(frame_type=1, opcode=0x9002)
def handle_enter_game(context: PacketContext) -> None:
    runtime = require_runtime()

    try:
        role_name = decode_role_name_c_string(
            context.payload,
            "0x9002 enter-game",
        )
    except ValueError as exc:
        print(f"[login-handler] 0x9002 role-name parse failed: {exc}")
        if context.control is not None:
            context.control.request_close("invalid CP936 role name in 0x9002")
        return

    print(
        "[login-handler] 0x9002 enter-game request: "
        f"role_name={role_name!r}, "
        f"authenticated={runtime.authenticated}, "
        f"account={runtime.account_username!r}"
    )

    if not runtime.authenticated:
        print(
            "[login-handler] Refusing 0x9002 because no valid account is "
            "bound to this connection."
        )
        send_login_status(
            runtime,
            LOGIN_STATUS_ACCOUNT_PASSWORD_ERROR,
            "0x7000 login rejected: enter-game before account login",
        )
        return

    if role_name not in runtime.role_names:
        print(
            "[login-handler] Refusing 0x9002 because selected role is not "
            f"in current account role list: {role_name!r}. Closing login connection."
        )
        if context.control is not None:
            context.control.request_close(
                f"selected role {role_name!r} is not owned by account"
            )
        return

    if runtime.select_role is not None:
        selected_ok = runtime.select_role(role_name)
        print(
            "[login-handler] Selected role bind result: "
            f"role_name={role_name!r}, ok={selected_ok}"
        )
        if not selected_ok:
            if context.control is not None:
                context.control.request_close(
                    f"selected role {role_name!r} failed ownership validation"
                )
            return

    if runtime.enter_game_delay_seconds > 0:
        print(
            "[login-handler] Waiting "
            f"{runtime.enter_game_delay_seconds:.2f}s "
            "before 0x7003 ChangeConnect"
        )
        time.sleep(runtime.enter_game_delay_seconds)

    connect_info_bytes = c_string(runtime.enter_game_connect_info)

    response_data = b"".join(
        (
            runtime.enter_game_status.to_bytes(2, "big"),
            len(runtime.enter_game_ticket).to_bytes(4, "big"),
            runtime.enter_game_ticket,
            connect_info_bytes,
        )
    )

    print(
        "[login-handler] Preparing 0x7003 ChangeConnect: "
        f"status={runtime.enter_game_status}, "
        f"ticket_len={len(runtime.enter_game_ticket)}, "
        f"connect_info={runtime.enter_game_connect_info!r}, "
        f"data_length={len(response_data)}"
    )

    runtime.send_type4(
        0x7003,
        response_data,
        "Login handler 0x7003 ChangeConnect",
    )

    print("[login-handler] 0x7003 ChangeConnect sent")


@LOGIN_DISPATCHER.register(frame_type=1, opcode=0x9003)
def handle_create_role_request(context: PacketContext) -> None:
    runtime = require_runtime()

    print(
        "[login-handler] 0x9003 create-role request: "
        f"payload_length={len(context.payload)}, "
        f"authenticated={runtime.authenticated}, "
        f"account={runtime.account_username!r}, "
        f"payload={context.payload.hex(' ')}"
    )

    if not runtime.authenticated:
        print(
            "[login-handler] Refusing 0x9003 because no valid account is "
            "bound to this connection."
        )
        send_login_status(
            runtime,
            LOGIN_STATUS_ACCOUNT_PASSWORD_ERROR,
            "0x7000 login rejected: create-role before account login",
        )
        return

    try:
        request = parse_create_role_payload_9003(context.payload)
    except ValueError as exc:
        print(f"[login-handler] 0x9003 parse failed: {exc}")
        send_role_list_response(
            runtime,
            "Stable type-2 0x7002 role-list response after create-role parse failure",
            delay_seconds=0,
        )
        return

    print(
        "[login-handler] Parsed 0x9003 create-role request: "
        f"name={request.name!r}, "
        f"gender_selector={request.gender_selector}, "
        f"profession_selector={request.profession_selector}, "
        f"hair_model={request.hair_model}, "
        f"feature_model={request.feature_model}, "
        f"profession_model={request.profession_model}, "
        f"country_selector={request.country_selector}, "
        f"token_parts=(gender={request.gender}, body={request.body}, "
        f"hair={request.hair}, head={request.head}, "
        f"hand_r={request.hand_r}, hand_l={request.hand_l}, "
        f"pants={request.pants}, foot_r={request.foot_r}, "
        f"foot_l={request.foot_l})"
    )

    if runtime.create_role is None:
        print("[login-handler] No create-role handler configured")
        return

    ok, reason, role_tokens, role_names = runtime.create_role(request)
    print(
        "[login-handler] Create-role database result: "
        f"ok={ok}, reason={reason!r}, "
        f"role_count={len(role_tokens)}, role_names={sorted(role_names)!r}"
    )

    if not ok:
        if reason == "duplicate-name":
            print(
                "[login-handler] Create-role rejected: role name already "
                f"exists: {request.name!r}"
            )
        else:
            print(
                "[login-handler] Create-role rejected; returning current "
                f"role list instead of leaving client in creating state: {reason!r}"
            )
        runtime.role_tokens = role_tokens
        runtime.role_names = role_names
        send_role_list_response(
            runtime,
            "Stable type-2 0x7002 role-list response after create-role rejection",
            delay_seconds=0,
        )
        return

    runtime.role_tokens = role_tokens
    runtime.role_names = role_names

    send_role_list_response(
        runtime,
        "Stable type-2 0x7002 role-list response after create-role",
        delay_seconds=0,
    )


def parse_delete_role_name_payload(payload: bytes) -> str:
    """Parse delete-role style payloads that carry one role-name C string.

    The decompiled client has role-name send helpers for 0x9008 and 0x9009,
    while some client builds also use 0x9004 in this area. Accept the same compact
    payload shape for all candidate delete opcodes.
    """
    return decode_role_name_c_string(payload, "delete-role")


def handle_delete_role_request(context: PacketContext, opcode: int) -> None:
    runtime = require_runtime()

    print(
        f"[login-handler] 0x{opcode:04X} delete-role candidate request: "
        f"payload_length={len(context.payload)}, "
        f"authenticated={runtime.authenticated}, "
        f"account={runtime.account_username!r}, "
        f"payload={context.payload.hex(' ')}"
    )

    if not runtime.authenticated:
        print(
            f"[login-handler] Refusing 0x{opcode:04X} delete-role because "
            "no valid account is bound to this connection."
        )
        send_login_status(
            runtime,
            LOGIN_STATUS_ACCOUNT_PASSWORD_ERROR,
            f"0x7000 login rejected: delete-role before account login opcode=0x{opcode:04X}",
        )
        return

    try:
        role_name = parse_delete_role_name_payload(context.payload)
    except ValueError as exc:
        print(f"[login-handler] 0x{opcode:04X} delete-role parse failed: {exc}")
        send_role_list_response(
            runtime,
            f"Stable type-2 0x7002 role-list response after delete-role parse failure opcode=0x{opcode:04X}",
            delay_seconds=0,
        )
        return

    print(
        f"[login-handler] Parsed 0x{opcode:04X} delete-role request: "
        f"role_name={role_name!r}, current_role_names={sorted(runtime.role_names)!r}"
    )

    if role_name not in runtime.role_names:
        print(
            f"[login-handler] Refusing 0x{opcode:04X} delete-role because "
            f"role is not in current account role list: {role_name!r}"
        )
        send_role_list_response(
            runtime,
            f"Stable type-2 0x7002 role-list response after delete-role ownership failure opcode=0x{opcode:04X}",
            delay_seconds=0,
        )
        return

    if runtime.delete_role is None:
        print("[login-handler] No delete-role handler configured")
        return

    ok, reason, role_tokens, role_names = runtime.delete_role(role_name)
    print(
        "[login-handler] Delete-role database result: "
        f"ok={ok}, reason={reason!r}, "
        f"role_count={len(role_tokens)}, role_names={sorted(role_names)!r}"
    )

    runtime.role_tokens = role_tokens
    runtime.role_names = role_names

    send_role_list_response(
        runtime,
        f"Stable type-2 0x7002 role-list response after delete-role opcode=0x{opcode:04X}",
        delay_seconds=0,
    )


@LOGIN_DISPATCHER.register(frame_type=1, opcode=0x9004)
def handle_delete_role_request_9004(context: PacketContext) -> None:
    handle_delete_role_request(context, 0x9004)


@LOGIN_DISPATCHER.register(frame_type=1, opcode=0x9008)
def handle_delete_role_request_9008(context: PacketContext) -> None:
    handle_delete_role_request(context, 0x9008)


@LOGIN_DISPATCHER.register(frame_type=1, opcode=0x9009)
def handle_delete_role_request_9009(context: PacketContext) -> None:
    handle_delete_role_request(context, 0x9009)
