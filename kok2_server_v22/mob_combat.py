from __future__ import annotations

import math
import random
import threading
import time
from typing import Callable

from protocol import build_type1_plain, c_string
from server_packets import build_map_mob_being_payload


# Confirmed v61 protocol constants.  These values describe client packet
# behavior, while per-monster timing/range values come from SQLite records.
DEFAULT_PLAYER_NORMAL_ATTACK_INTERVAL_MS = 2000
MOB_INCREMENTAL_0005_FIELD1C_MASK = 0x00010000
MOB_MOVE_MODE = 1
MOB_MOVE_COMMAND_MIN_INTERVAL_SECONDS = 0.35
MOB_MOVE_BASE_SPEED = 180
MOB_MOVE_BASE_TILES_PER_SECOND = 2.50
MOB_MOVE_RETURN_SETTLE_SECONDS = 0.50
MOB_MOVE_CHASE_RECHECK_SETTLE_SECONDS = 0.12
PLAYER_COMBAT_RETURN_EXIT_DELAY_SECONDS = 5.0


SendType1 = Callable[[int, bytes, str], None]
SendPlain = Callable[[bytes, str], None]
ApplyPlayerDamage = Callable[[dict[str, object], int], dict[str, object]]
GetPlayerAttackProfile = Callable[[], dict[str, int]]
GrantMobKillReward = Callable[[dict[str, object]], dict[str, object]]


def roll_attack_value(attack_min: int, attack_max: int) -> int:
    """Roll only when a genuine attack range exists.

    Equal bounds intentionally avoid the RNG path so an unarmed character, or
    any other fixed-value attack source, always deals a deterministic amount
    before target defense is applied.
    """
    minimum = max(0, int(attack_min))
    maximum = max(minimum, int(attack_max))
    if minimum == maximum:
        return minimum
    return random.randint(minimum, maximum)


def calculate_physical_damage(attack_roll: int, defense_power: int) -> int:
    """Apply the current physical defense rule with a one-damage floor."""
    return max(1, max(0, int(attack_roll)) - max(0, int(defense_power)))


def _int_value(record: dict[str, object], key: str, default: int) -> int:
    try:
        value = record.get(key, default)
        return int(default if value is None else value)
    except (TypeError, ValueError):
        return int(default)


def _float_value(record: dict[str, object], key: str, default: float) -> float:
    try:
        value = record.get(key, default)
        return float(default if value is None else value)
    except (TypeError, ValueError):
        return float(default)


def _text_value(record: dict[str, object], key: str, default: str = "") -> str:
    value = record.get(key, default)
    return str(default if value is None else value)


def _c_string(value: str) -> bytes:
    """Encode combat names with the same CP936 C-string codec as the rest of the protocol.

    Monster internal names are ASCII, while player Being names may be Chinese.
    Using ASCII here raises UnicodeEncodeError during the 0x8014 mode=1 ->
    0x0032 acknowledgement and closes the game connection.
    """
    return c_string(str(value))


def _u16(value: int) -> bytes:
    return max(0, min(0xFFFF, int(value))).to_bytes(2, "big")


def _i16(value: int) -> bytes:
    parsed = max(-0x8000, min(0x7FFF, int(value)))
    return parsed.to_bytes(2, "big", signed=True)


def hp_percent(current_hp: int, max_hp: int) -> int:
    current = max(0, int(current_hp))
    maximum = max(1, int(max_hp))
    if current <= 0:
        return 0
    return max(1, min(100, (current * 100) // maximum))


def distance_tiles(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(float(ax) - float(bx), float(ay) - float(by))


def direction_toward(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    fallback: int = 0,
) -> int:
    """Return the confirmed client direction: north=0, west=90, south=180, east=270."""
    dx = int(to_x) - int(from_x)
    dy = int(to_y) - int(from_y)
    if dx == 0 and dy == 0:
        return int(fallback) & 0xFFFF
    return int(round(math.degrees(math.atan2(-dx, -dy)) % 360.0)) % 360


def choose_adjacent_destination(
    mob_x: int,
    mob_y: int,
    player_x: int,
    player_y: int,
) -> tuple[int, int]:
    """Choose one tile adjacent to the player on the monster-facing side."""
    dx = int(mob_x) - int(player_x)
    dy = int(mob_y) - int(player_y)
    if abs(dx) >= abs(dy):
        step_x = 1 if dx > 0 else -1 if dx < 0 else 0
        return int(player_x) + step_x, int(player_y)
    step_y = 1 if dy > 0 else -1 if dy < 0 else 0
    return int(player_x), int(player_y) + step_y


def build_attack_relation_0032_payload(
    actor_being_name: str,
    target_being_name: str,
    field08: int = 0,
) -> bytes:
    return b"".join((
        _c_string(actor_being_name),
        _c_string(target_being_name),
        max(0, min(0xFFFFFFFF, int(field08))).to_bytes(4, "big"),
    ))


def build_damage_display_0035_payload(
    source_being_name: str,
    target_being_name: str,
    damage: int,
    *,
    effect_id: int = 0,
    target_hp_percent: int = 100,
    source_state: int = 0,
    display_type: int = 0,
) -> bytes:
    """Build one server opcode 0x0035 combat-result display record.

    Confirmed wire order::

        source C string, target C string,
        u32 effect_id,
        u8 target_hp_percent, u8 source_state, u8 display_type,
        u32 final_damage

    The client stores ``target_hp_percent`` in the same Being field that is
    populated by opcode 0x0005 ``field20``.  It is the target's post-hit HP
    percentage, not a Boolean alive flag.  Zero enters the client's death /
    corpse presentation path; values 1..100 display the remaining HP bar.
    The server remains authoritative for HP and death, while this value mirrors
    the already-calculated post-hit state for client presentation.

    ``effect_id=0`` resolves to the configured ``none`` effect.  The client
    independently chooses light-blue or yellow number textures according to
    whether the target Being is the local player.
    """
    parsed_damage = max(0, min(0xFFFFFFFF, int(damage)))
    return b"".join((
        _c_string(source_being_name),
        _c_string(target_being_name),
        max(0, min(0xFFFFFFFF, int(effect_id))).to_bytes(4, "big"),
        max(0, min(100, int(target_hp_percent))).to_bytes(1, "big"),
        max(0, min(0xFF, int(source_state))).to_bytes(1, "big"),
        max(0, min(0xFF, int(display_type))).to_bytes(1, "big"),
        parsed_damage.to_bytes(4, "big"),
    ))


def build_action_0036_payload(
    actor_being_name: str,
    target_being_name: str,
    *,
    field08: int = 0,
    field0a: int = 0,
    field0c: int = 0,
    field0e: int = 0,
    field10: int = 0,
    action_type: int = 1,
    field14: int = 0,
    field16: int = 0,
) -> bytes:
    return b"".join((
        _c_string(actor_being_name),
        _c_string(target_being_name),
        _u16(field08),
        _u16(field0a),
        _u16(field0c),
        _u16(field0e),
        _u16(field10),
        max(0, min(0xFF, int(action_type))).to_bytes(1, "big"),
        _i16(field14),
        _u16(field16),
    ))


def build_mob_move_0004_payload(
    network_id: int,
    destination_x: int,
    destination_y: int,
    *,
    movement_mode: int = MOB_MOVE_MODE,
    movement_speed: int = MOB_MOVE_BASE_SPEED,
) -> bytes:
    parsed_id = max(0, min(0xFFFFFFFF, int(network_id)))
    x = max(0, min(0xFFFF, int(destination_x)))
    y = max(0, min(0xFFFF, int(destination_y)))
    mode = max(0, min(0xFF, int(movement_mode)))
    speed = max(-0x8000, min(0x7FFF, int(movement_speed)))
    packed_position = ((x & 0xFFFF) << 16) | (y & 0xFFFF)
    return b"".join((
        parsed_id.to_bytes(4, "big"),
        packed_position.to_bytes(4, "big"),
        mode.to_bytes(1, "big"),
        speed.to_bytes(2, "big", signed=True),
    ))


class MobCombatRuntime:
    """Per-game-connection monster combat runtime based on the stable v61 flow.

    Persistent monster definitions and spawn positions remain authoritative in
    SQLite.  This class only holds ephemeral HP, movement and timer state for
    the current client connection.
    """

    def __init__(
        self,
        *,
        send_type1: SendType1,
        send_plain: SendPlain,
        get_player_attack_profile: GetPlayerAttackProfile,
        apply_player_damage: ApplyPlayerDamage | None = None,
        grant_mob_kill_reward: GrantMobKillReward | None = None,
        player_attack_interval_ms: int = DEFAULT_PLAYER_NORMAL_ATTACK_INTERVAL_MS,
    ) -> None:
        self._send_type1 = send_type1
        self._send_plain = send_plain
        self._apply_player_damage = apply_player_damage
        self._grant_mob_kill_reward = grant_mob_kill_reward
        self._get_player_attack_profile = get_player_attack_profile
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, object]] = {}
        self._player: dict[str, object] = {
            "map_id": 0,
            "x": 0,
            "y": 0,
            "direction": 0,
            "updated_at": 0.0,
        }
        self._session_generation = 0
        self._closed = False
        self._collision_grid: dict[str, object] | None = None
        self._player_attack_interval_seconds = max(
            0.05, int(player_attack_interval_ms) / 1000.0
        )
        self._player_attack_started_at: float | None = None
        # One-shot regeneration delay announced when the final engaged mob
        # leaves COMBAT and begins RETURNING.  This is connection-local and
        # intentionally not persisted in SQLite.
        self._combat_exit_revision = 0
        self._combat_exit_delay_seconds = 1.0
        self._visibility_enter_radius: float | None = None
        self._visibility_leave_radius: float | None = None
        print(
            "[player-attack] configured normal attack interval: "
            f"{self._player_attack_interval_seconds:.3f}s "
            f"({int(player_attack_interval_ms)} ms)"
        )

    def set_player_attack_interval_ms(self, interval_ms: int) -> None:
        """Apply a newly derived attack interval without rebuilding combat state."""
        parsed = max(50, int(interval_ms))
        with self._lock:
            self._player_attack_interval_seconds = parsed / 1000.0
        print(
            "[player-attack] updated normal attack interval: "
            f"{self._player_attack_interval_seconds:.3f}s ({parsed} ms)"
        )

    def _has_active_player_combat_locked(self) -> bool:
        """Return whether a live, target-bound monster has a running attack loop.

        Requiring ``attack_loop_running`` is an intentional fail-safe: a stale
        ``combat_state='COMBAT'`` left behind by an unexpected timer failure can
        never hold player regeneration in COMBAT forever.  Normal chasing and
        waiting monsters keep their independent attack loop running, so they
        still count as active combat even while outside attack range.
        """
        return any(
            bool(state.get("alive", False))
            and bool(state.get("present", False))
            and str(state.get("combat_state", "IDLE")) == "COMBAT"
            and bool(str(state.get("target_player_being_name", "") or ""))
            and bool(state.get("attack_loop_running", False))
            for state in self._states.values()
        )

    def is_player_in_combat(self) -> bool:
        """Return whether any live monster is actively engaged with the player."""
        with self._lock:
            if self._closed:
                return False
            return self._has_active_player_combat_locked()

    def get_player_regeneration_combat_state(self) -> tuple[bool, int, float]:
        """Return combat mode plus the latest one-shot COMBAT->IDLE delay token.

        The server regeneration scheduler uses the revision to distinguish a
        normal IDLE state from the moment when the final monster starts
        returning.  The latter receives one five-second delay before the first
        IDLE regeneration tick; later IDLE ticks return to the normal one-second
        cadence.
        """
        with self._lock:
            if self._closed:
                return False, int(self._combat_exit_revision), 1.0
            return (
                self._has_active_player_combat_locked(),
                int(self._combat_exit_revision),
                float(self._combat_exit_delay_seconds),
            )

    def _begin_return_locked(
        self,
        state: dict[str, object],
        *,
        reason: str,
    ) -> tuple[int, int]:
        """Atomically stop combat for one mob and arm the final-exit delay.

        The revision is advanced only when this transition leaves no other
        actively engaged monster.  Therefore multiple monsters behave
        naturally: the player remains in COMBAT until the final one disengages.
        """
        anchor_x = int(
            state.get("combat_anchor_x", state.get("spawn_x", 0)) or 0
        )
        anchor_y = int(
            state.get("combat_anchor_y", state.get("spawn_y", 0)) or 0
        )
        state["combat_state"] = "RETURNING"
        state["target_player_being_name"] = ""
        state["attack_loop_running"] = False
        state["attack_loop_generation"] = int(
            state.get("attack_loop_generation", 0)
        ) + 1
        state["movement_phase"] = "RETURN"

        if not self._has_active_player_combat_locked():
            self._combat_exit_revision += 1
            self._combat_exit_delay_seconds = (
                PLAYER_COMBAT_RETURN_EXIT_DELAY_SECONDS
            )
            print(
                "[mob-combat] final mob started RETURNING: "
                f"delay first IDLE regeneration by "
                f"{PLAYER_COMBAT_RETURN_EXIT_DELAY_SECONDS:.1f}s, "
                f"revision={self._combat_exit_revision}, reason={reason}"
            )
        return anchor_x, anchor_y

    def _settle_return_locally_locked(
        self,
        state: dict[str, object],
        *,
        reason: str,
    ) -> None:
        """Clear a failed return without leaving a permanent COMBAT state."""
        anchor_x = int(
            state.get("combat_anchor_x", state.get("spawn_x", 0)) or 0
        )
        anchor_y = int(
            state.get("combat_anchor_y", state.get("spawn_y", 0)) or 0
        )
        state["current_x"] = anchor_x
        state["current_y"] = anchor_y
        state["current_x_float"] = float(anchor_x)
        state["current_y_float"] = float(anchor_y)
        state["movement_active"] = False
        state["movement_generation"] = int(
            state.get("movement_generation", 0)
        ) + 1
        state["movement_phase"] = "IDLE"
        state["combat_state"] = "IDLE"
        state["target_player_being_name"] = ""
        state["attack_loop_running"] = False
        state["current_hp"] = int(state.get("max_hp", 1) or 1)
        state["last_move_destination"] = None
        state["combat_anchor_valid"] = False
        state["wander_generation"] = int(
            state.get("wander_generation", 0)
        ) + 1
        print(
            f"[mob-ai] local safe disengage: mob={state.get('being_name')!r}, "
            f"anchor=({anchor_x},{anchor_y}), reason={reason}"
        )

    def load_map(
        self,
        records: list[dict[str, object]],
        *,
        map_id: int,
        player_x: int,
        player_y: int,
        player_direction: int = 0,
        collision_grid: dict[str, object] | None = None,
        initially_visible_beings: set[str] | None = None,
    ) -> None:
        with self._lock:
            self._invalidate_all_locked("load map")
            self._closed = False
            self._states.clear()
            self._collision_grid = dict(collision_grid) if collision_grid is not None else None
            self._player_attack_started_at = None
            self._combat_exit_revision += 1
            self._combat_exit_delay_seconds = 1.0
            self._player.update({
                "map_id": int(map_id),
                "x": int(player_x),
                "y": int(player_y),
                "direction": int(player_direction),
                "updated_at": time.monotonic(),
            })
            for record in records:
                state = self._make_state(dict(record), int(map_id))
                state["network_visible"] = (
                    True
                    if initially_visible_beings is None
                    else str(state["being_name"]) in initially_visible_beings
                )
                self._states[str(state["being_name"])] = state
            being_names = list(self._states)
            count = len(being_names)
            collision_ready = bool(
                self._collision_grid is not None
                and int(self._collision_grid.get("map_id", map_id)) == int(map_id)
            )
        print(
            f"[mob-combat] registered {count} runtime mob(s): "
            f"map={int(map_id)}, player=({int(player_x)},{int(player_y)}), "
            f"collision_grid={collision_ready}"
        )
        for being_name in being_names:
            self._schedule_idle_wander(being_name, initial=True)

    def sync_network_visibility(
        self,
        *,
        player_x: int,
        player_y: int,
        enter_radius: float,
        leave_radius: float,
    ) -> tuple[int, int]:
        """Create/delete monster Beings as they enter or leave player view.

        ``0x0005`` is the client-side Being create/update packet and ``0x0007``
        removes that Being.  Runtime state remains registered while invisible,
        so wandering, respawn and future visibility transitions keep using the
        same stable network identity.
        """
        parsed_enter = max(0.0, float(enter_radius))
        parsed_leave = max(parsed_enter, float(leave_radius))
        to_show: list[dict[str, object]] = []
        to_hide: list[str] = []
        now_value = time.monotonic()

        with self._lock:
            if self._closed:
                return 0, 0
            self._visibility_enter_radius = parsed_enter
            self._visibility_leave_radius = parsed_leave
            self._player["x"] = int(player_x)
            self._player["y"] = int(player_y)
            self._player["updated_at"] = now_value

            for state in self._states.values():
                current_x, current_y, _ = self._update_estimated_position_locked(
                    state, now_value
                )
                distance = distance_tiles(
                    int(player_x), int(player_y), current_x, current_y
                )
                visible = bool(state.get("network_visible", False))
                present = bool(state.get("present", False))
                threshold = parsed_leave if visible else parsed_enter
                should_be_visible = present and distance <= threshold

                if should_be_visible and not visible:
                    state["network_visible"] = True
                    to_show.append(state)
                elif visible and not should_be_visible:
                    to_hide.append(str(state.get("being_name", "")))

        # Delete first so a large movement jump cannot temporarily leave both
        # old and new view sets active in the client.
        for being_name in to_hide:
            self._send_delete(
                being_name,
                f"game 0x0007 mob-leave-view {being_name!r}",
            )
        for state in to_show:
            self._send_mob_state(
                state,
                f"game 0x0005 mob-enter-view {str(state.get('being_name', ''))!r}",
            )

        if to_show or to_hide:
            print(
                f"[mob-visibility] player=({int(player_x)},{int(player_y)}), "
                f"enter={parsed_enter:.1f}, leave={parsed_leave:.1f}, "
                f"show={len(to_show)}, hide={len(to_hide)}"
            )
        return len(to_show), len(to_hide)

    def close(self, reason: str = "connection closed") -> None:
        with self._lock:
            self._closed = True
            self._invalidate_all_locked(reason)
            self._states.clear()
            self._collision_grid = None
            self._visibility_enter_radius = None
            self._visibility_leave_radius = None
            self._player_attack_started_at = None
            self._combat_exit_revision += 1
            self._combat_exit_delay_seconds = 1.0
        print(f"[mob-combat] runtime closed: {reason}")

    def _invalidate_all_locked(self, reason: str) -> None:
        self._session_generation += 1
        for state in self._states.values():
            state["attack_loop_running"] = False
            state["attack_loop_generation"] = int(
                state.get("attack_loop_generation", 0)
            ) + 1
            state["movement_active"] = False
            state["movement_generation"] = int(
                state.get("movement_generation", 0)
            ) + 1
            state["wander_generation"] = int(
                state.get("wander_generation", 0)
            ) + 1
        if self._states:
            print(
                f"[mob-combat] invalidated {len(self._states)} runtime mob(s): {reason}"
            )

    def _make_state(
        self,
        record: dict[str, object],
        map_id: int,
    ) -> dict[str, object]:
        spawn_id = _int_value(record, "spawn_id", 0)
        network_id = _int_value(record, "network_id", 2_000_000 + spawn_id)
        being_name = _text_value(
            record,
            "internal_name",
            f"NPC{int(map_id)}_{1000 + spawn_id:03d}#{network_id}",
        )
        spawn_x = _int_value(record, "position_x", 0)
        spawn_y = _int_value(record, "position_y", 0)
        direction = _int_value(record, "direction", 0) & 0xFFFF
        max_hp = max(1, _int_value(record, "max_hp", 1))
        move_speed = max(1, _int_value(record, "move_speed", MOB_MOVE_BASE_SPEED))
        return {
            "being_name": being_name,
            "map_id": int(map_id),
            "spawn_id": spawn_id,
            "network_id": network_id,
            "record": dict(record),
            "display_name": _text_value(record, "display_name", being_name),
            "level": max(1, _int_value(record, "level", 1)),
            "max_hp": max_hp,
            "current_hp": max_hp,
            "attack_min": max(0, _int_value(record, "attack_min", 0)),
            "attack_max": max(
                max(0, _int_value(record, "attack_min", 0)),
                _int_value(record, "attack_max", 0),
            ),
            "defense_power": max(0, _int_value(record, "defense_power", 0)),
            "experience_reward": max(
                0, _int_value(record, "experience_reward", 0)
            ),
            "gold_min": max(0, _int_value(record, "gold_min", 0)),
            "gold_max": max(
                max(0, _int_value(record, "gold_min", 0)),
                _int_value(record, "gold_max", 0),
            ),
            "reward_granted": False,
            "alive": True,
            "present": True,
            "network_visible": True,
            "corpse": False,
            "death_sequence_scheduled": False,
            "spawn_x": spawn_x,
            "spawn_y": spawn_y,
            "current_x": spawn_x,
            "current_y": spawn_y,
            "current_x_float": float(spawn_x),
            "current_y_float": float(spawn_y),
            "spawn_direction": direction,
            "current_direction": direction,
            # The spawn point is the idle-roaming center.  A separate combat
            # anchor is captured at the monster's actual position on the first
            # hit of each engagement.
            "combat_anchor_valid": False,
            "combat_anchor_x": spawn_x,
            "combat_anchor_y": spawn_y,
            "combat_anchor_direction": direction,
            "combat_state": "IDLE",
            "target_player_being_name": "",
            "attack_loop_running": False,
            "attack_loop_generation": 0,
            "wander_generation": 0,
            "movement_generation": 0,
            "movement_active": False,
            "movement_phase": "IDLE",
            "movement_start_x": float(spawn_x),
            "movement_start_y": float(spawn_y),
            "movement_target_x": float(spawn_x),
            "movement_target_y": float(spawn_y),
            "movement_started_at": 0.0,
            "movement_duration": 0.0,
            "last_move_command_at": 0.0,
            "last_move_destination": None,
            "first_attack_delay": max(
                0.0, _int_value(record, "first_attack_delay_ms", 800) / 1000.0
            ),
            "attack_interval": max(
                0.05, _int_value(record, "attack_interval_ms", 2500) / 1000.0
            ),
            "attack_range": max(0.0, _float_value(record, "attack_range", 2.0)),
            "move_speed": move_speed,
            "estimated_tiles_per_second": max(
                0.10,
                MOB_MOVE_BASE_TILES_PER_SECOND
                * float(move_speed)
                / float(MOB_MOVE_BASE_SPEED),
            ),
            "leash_range": max(
                0.0, _float_value(record, "effective_leash_range", 20.0)
            ),
            "idle_wander_enabled": bool(
                _int_value(record, "idle_wander_enabled", 1)
            ),
            "idle_wander_radius": max(
                0.0, _float_value(record, "idle_wander_radius", 3.0)
            ),
            "idle_wander_min_pause": max(
                0.0,
                _int_value(record, "idle_wander_min_pause_ms", 3000) / 1000.0,
            ),
            "idle_wander_max_pause": max(
                0.0,
                _int_value(record, "idle_wander_max_pause_ms", 7000) / 1000.0,
            ),
            "idle_wander_move_speed": max(
                1, _int_value(record, "idle_wander_move_speed", 180)
            ),
            "corpse_seconds": max(
                0.0, _int_value(record, "corpse_ms", 5000) / 1000.0
            ),
            "respawn_seconds": max(
                0.0, _int_value(record, "effective_respawn_ms", 30000) / 1000.0
            ),
            "action_type": max(0, min(0xFF, _int_value(record, "action_type", 1))),
            "attack_effect_field10": max(
                0,
                min(0xFFFF, _int_value(record, "attack_effect_field10", 0)),
            ),
            "aggro_mode": _text_value(record, "aggro_mode", "RETALIATE").upper(),
        }

    def _update_player_position(
        self,
        *,
        map_id: int,
        x: int,
        y: int,
        direction: int,
    ) -> None:
        with self._lock:
            self._player.update({
                "map_id": int(map_id),
                "x": int(x),
                "y": int(y),
                "direction": int(direction),
                "updated_at": time.monotonic(),
            })

    def _player_snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._player)

    def _is_walkable_locked(self, map_id: int, x: int, y: int) -> bool:
        grid = self._collision_grid
        if grid is None:
            return False
        try:
            if int(grid.get("map_id", map_id)) != int(map_id):
                return False
            width = int(grid["width"])
            height = int(grid["height"])
            grid_x = int(x)
            grid_y = int(y)
            if not (0 <= grid_x < width and 0 <= grid_y < height):
                return False
            raw = grid["raw_grid"]
            return int(raw[grid_y * width + grid_x]) == int(
                grid.get("walkable_value", 1)
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return False

    def _normalize_combat_anchor_locked(
        self,
        state: dict[str, object],
        x: int,
        y: int,
    ) -> tuple[int, int]:
        map_id = int(state.get("map_id", 0) or 0)
        candidates: list[tuple[int, int]] = [(int(x), int(y))]
        candidates.extend((
            (
                int(round(float(state.get("movement_target_x", x) or x))),
                int(round(float(state.get("movement_target_y", y) or y))),
            ),
            (
                int(round(float(state.get("movement_start_x", x) or x))),
                int(round(float(state.get("movement_start_y", y) or y))),
            ),
            (
                int(state.get("spawn_x", x) or x),
                int(state.get("spawn_y", y) or y),
            ),
        ))
        for radius in (1, 2):
            ring: list[tuple[int, int]] = []
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    ring.append((int(x) + dx, int(y) + dy))
            ring.sort(key=lambda point: distance_tiles(point[0], point[1], x, y))
            candidates.extend(ring)

        seen: set[tuple[int, int]] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if self._is_walkable_locked(map_id, candidate[0], candidate[1]):
                return candidate
        return int(state.get("spawn_x", x) or x), int(
            state.get("spawn_y", y) or y
        )

    @staticmethod
    def _random_wander_pause_locked(state: dict[str, object]) -> float:
        minimum = max(0.0, float(state.get("idle_wander_min_pause", 3.0) or 0.0))
        maximum = max(minimum, float(state.get("idle_wander_max_pause", 7.0) or 0.0))
        return random.uniform(minimum, maximum)

    def _choose_idle_wander_destination_locked(
        self,
        state: dict[str, object],
    ) -> tuple[int, int] | None:
        radius = max(0.0, float(state.get("idle_wander_radius", 3.0) or 0.0))
        if radius < 1.0:
            return None
        map_id = int(state.get("map_id", 0) or 0)
        spawn_x = int(state.get("spawn_x", 0) or 0)
        spawn_y = int(state.get("spawn_y", 0) or 0)
        current_x, current_y, moving = self._update_estimated_position_locked(state)
        if moving:
            return None
        current_ix = int(round(current_x))
        current_iy = int(round(current_y))

        # Sample discrete cells inside the spawn-centered circle.  The collision
        # grid is authoritative; no idle movement is issued without it.
        candidates: list[tuple[int, int]] = []
        integer_radius = max(1, int(math.ceil(radius)))
        for _ in range(32):
            candidate_x = spawn_x + random.randint(-integer_radius, integer_radius)
            candidate_y = spawn_y + random.randint(-integer_radius, integer_radius)
            if (candidate_x, candidate_y) == (current_ix, current_iy):
                continue
            if distance_tiles(candidate_x, candidate_y, spawn_x, spawn_y) > radius:
                continue
            if not self._is_walkable_locked(map_id, candidate_x, candidate_y):
                continue

            # Avoid selecting a cell already occupied by another live monster.
            occupied = False
            for other in self._states.values():
                if other is state:
                    continue
                if not bool(other.get("alive", False)) or not bool(
                    other.get("present", False)
                ):
                    continue
                other_x, other_y, _ = self._update_estimated_position_locked(other)
                if distance_tiles(candidate_x, candidate_y, other_x, other_y) < 1.0:
                    occupied = True
                    break
            if not occupied:
                candidates.append((candidate_x, candidate_y))
        return random.choice(candidates) if candidates else None

    def _schedule_idle_wander(
        self,
        being_name: str,
        *,
        initial: bool = False,
        delay_seconds: float | None = None,
    ) -> None:
        with self._lock:
            state = self._states.get(being_name)
            if state is None:
                return
            if not bool(state.get("idle_wander_enabled", False)):
                return
            if float(state.get("idle_wander_radius", 0.0) or 0.0) < 1.0:
                return
            if self._collision_grid is None:
                if not bool(state.get("wander_missing_collision_logged", False)):
                    state["wander_missing_collision_logged"] = True
                    print(
                        f"[mob-wander] disabled without collision grid: "
                        f"mob={being_name!r}, map={state.get('map_id')}"
                    )
                return
            generation = int(state.get("wander_generation", 0))
            session_generation = self._session_generation
            if delay_seconds is None:
                if initial:
                    maximum = max(
                        1.0,
                        float(state.get("idle_wander_max_pause", 7.0) or 7.0),
                    )
                    delay = random.uniform(1.0, maximum)
                else:
                    delay = self._random_wander_pause_locked(state)
            else:
                delay = max(0.05, float(delay_seconds))

        def wander_tick() -> None:
            try:
                with self._lock:
                    if self._closed or self._session_generation != session_generation:
                        return
                    current = self._states.get(being_name)
                    if current is None:
                        return
                    if int(current.get("wander_generation", 0)) != generation:
                        return
                    if str(current.get("combat_state", "IDLE")) != "IDLE":
                        return
                    if not bool(current.get("alive", False)) or not bool(
                        current.get("present", False)
                    ):
                        return
                    destination = self._choose_idle_wander_destination_locked(current)
                    if destination is None:
                        retry_delay = self._random_wander_pause_locked(current)
                    else:
                        retry_delay = None

                if destination is None:
                    self._schedule_idle_wander(
                        being_name,
                        delay_seconds=max(0.50, retry_delay),
                    )
                    return

                destination_x, destination_y = destination
                speed = int(current.get("idle_wander_move_speed", 180) or 180)
                next_generation, duration = self._send_move(
                    current,
                    destination_x,
                    destination_y,
                    "WANDER",
                    movement_speed=speed,
                )
                print(
                    f"[mob-wander] move: mob={being_name!r}, "
                    f"spawn=({current.get('spawn_x')},{current.get('spawn_y')}), "
                    f"destination=({destination_x},{destination_y}), "
                    f"radius={float(current.get('idle_wander_radius', 0.0)):.1f}, "
                    f"speed={speed}"
                )
                with self._lock:
                    latest = self._states.get(being_name)
                    if latest is None:
                        return
                    if int(latest.get("wander_generation", 0)) != generation:
                        return
                    pause = self._random_wander_pause_locked(latest)
                self._schedule_idle_wander(
                    being_name,
                    delay_seconds=duration + pause,
                )
            except Exception as error:
                print(
                    f"[mob-wander] failed for {being_name!r}: "
                    f"{type(error).__name__}: {error}"
                )

        timer = threading.Timer(max(0.05, delay), wander_tick)
        timer.daemon = True
        timer.start()

    def _update_estimated_position_locked(
        self,
        state: dict[str, object],
        now_monotonic: float | None = None,
    ) -> tuple[float, float, bool]:
        now_value = time.monotonic() if now_monotonic is None else float(now_monotonic)
        current_x = float(state.get("current_x_float", state.get("current_x", 0)) or 0.0)
        current_y = float(state.get("current_y_float", state.get("current_y", 0)) or 0.0)
        if not bool(state.get("movement_active", False)):
            state["current_x_float"] = current_x
            state["current_y_float"] = current_y
            state["current_x"] = int(round(current_x))
            state["current_y"] = int(round(current_y))
            return current_x, current_y, False

        start_time = float(state.get("movement_started_at", now_value) or now_value)
        duration = max(0.01, float(state.get("movement_duration", 0.01) or 0.01))
        start_x = float(state.get("movement_start_x", current_x) or current_x)
        start_y = float(state.get("movement_start_y", current_y) or current_y)
        target_x = float(state.get("movement_target_x", current_x) or current_x)
        target_y = float(state.get("movement_target_y", current_y) or current_y)
        progress = max(0.0, min(1.0, (now_value - start_time) / duration))
        current_x = start_x + (target_x - start_x) * progress
        current_y = start_y + (target_y - start_y) * progress
        active = progress < 1.0
        if not active:
            current_x = target_x
            current_y = target_y
            state["movement_active"] = False
            state["movement_phase"] = "ARRIVED"
        state["current_x_float"] = current_x
        state["current_y_float"] = current_y
        state["current_x"] = int(round(current_x))
        state["current_y"] = int(round(current_y))
        return current_x, current_y, active

    def _start_estimated_move_locked(
        self,
        state: dict[str, object],
        destination_x: int,
        destination_y: int,
        *,
        phase: str,
        tiles_per_second: float | None = None,
    ) -> tuple[int, float]:
        now_value = time.monotonic()
        current_x, current_y, _ = self._update_estimated_position_locked(
            state, now_value
        )
        target_x = float(int(destination_x))
        target_y = float(int(destination_y))
        distance = math.hypot(target_x - current_x, target_y - current_y)
        effective_tiles_per_second = max(
            0.10,
            float(tiles_per_second)
            if tiles_per_second is not None
            else float(state.get("estimated_tiles_per_second", 2.50) or 2.50),
        )
        duration = max(0.20, distance / effective_tiles_per_second)
        generation = int(state.get("movement_generation", 0)) + 1
        state["movement_generation"] = generation
        state["movement_active"] = distance > 0.01
        state["movement_phase"] = str(phase)
        state["movement_start_x"] = current_x
        state["movement_start_y"] = current_y
        state["movement_target_x"] = target_x
        state["movement_target_y"] = target_y
        if distance > 0.01:
            state["current_direction"] = direction_toward(
                int(round(current_x)),
                int(round(current_y)),
                int(destination_x),
                int(destination_y),
                int(state.get("current_direction", 0) or 0),
            )
        state["movement_started_at"] = now_value
        state["movement_duration"] = duration
        state["last_move_command_at"] = now_value
        state["last_move_destination"] = (int(destination_x), int(destination_y))
        return generation, duration

    def _update_facing_locked(
        self,
        state: dict[str, object],
        player_x: int,
        player_y: int,
    ) -> int:
        direction = direction_toward(
            int(state.get("current_x", 0) or 0),
            int(state.get("current_y", 0) or 0),
            int(player_x),
            int(player_y),
            int(state.get("current_direction", state.get("spawn_direction", 0)) or 0),
        )
        state["current_direction"] = direction
        return direction

    def _send_mob_state(
        self,
        state: dict[str, object],
        label: str,
        *,
        incremental_live_update: bool = False,
    ) -> None:
        with self._lock:
            if not bool(state.get("network_visible", True)) or not bool(
                state.get("present", False)
            ):
                return
            self._update_estimated_position_locked(state)
            record = dict(state["record"])
            map_id = int(state["map_id"])
            current_hp = int(state["current_hp"])
            max_hp = int(state["max_hp"])
            alive = bool(state["alive"])
            corpse = bool(state["corpse"])
            record["internal_name"] = str(state["being_name"])
            record["being_field20"] = hp_percent(current_hp, max_hp)
            record["being_field18"] = 4 if corpse or not alive else 0
            if incremental_live_update and alive and not corpse:
                base_field1c = _int_value(record, "being_field1c", 0) & 0xFFFFFFFF
                record["being_field1c"] = (
                    base_field1c | MOB_INCREMENTAL_0005_FIELD1C_MASK
                ) & 0xFFFFFFFF
            record["direction"] = int(
                state.get("current_direction", record.get("direction", 0)) or 0
            ) & 0xFFFF
            record["position_x"] = int(
                state.get("current_x", record.get("position_x", 0)) or 0
            )
            record["position_y"] = int(
                state.get("current_y", record.get("position_y", 0)) or 0
            )
        payload = build_map_mob_being_payload(record, map_id)
        self._send_type1(0x0005, payload, label)

    def _send_delete(self, being_name: str, label: str) -> None:
        with self._lock:
            state = self._states.get(str(being_name))
            if state is not None:
                if not bool(state.get("network_visible", True)):
                    return
                state["network_visible"] = False
        self._send_type1(0x0007, _c_string(being_name), label)

    def _send_attack_relation(
        self,
        actor_being_name: str,
        target_being_name: str,
        label_suffix: str,
    ) -> None:
        self._send_type1(
            0x0032,
            build_attack_relation_0032_payload(
                actor_being_name, target_being_name
            ),
            (
                f"game 0x0032 player-attack-{label_suffix} "
                f"actor={actor_being_name!r} "
                f"target={target_being_name if target_being_name else '<empty>'!r}"
            ),
        )

    def _schedule_attack_clear(
        self,
        actor_being_name: str,
        *,
        attack_started_at: float | None,
    ) -> None:
        """Clear the client's attack request at the configured start-to-start cadence.

        The client swing animation consumes part of the interval.  Therefore
        the delay after mode=2 is not the full database interval; it is the
        remaining time until ``mode=1 timestamp + normal_attack_interval``.
        This makes a 2000 ms database value mean roughly one attack every two
        seconds rather than two seconds plus the animation duration.
        """
        session_generation = self._session_generation
        now_value = time.monotonic()
        start_value = (
            float(attack_started_at)
            if attack_started_at is not None
            else now_value
        )
        clear_deadline = start_value + self._player_attack_interval_seconds
        delay_seconds = max(0.0, clear_deadline - now_value)
        print(
            f"[player-attack] schedule clear: actor={actor_being_name!r}, "
            f"interval={self._player_attack_interval_seconds:.3f}s, "
            f"remaining_recovery={delay_seconds:.3f}s"
        )

        def clear() -> None:
            with self._lock:
                if self._closed or self._session_generation != session_generation:
                    return
            try:
                self._send_attack_relation(
                    actor_being_name, "", "complete-clear"
                )
            except (OSError, ConnectionError) as error:
                print(f"[player-attack] delayed clear skipped: {error}")
            except Exception as error:
                print(
                    f"[player-attack] delayed clear failed: "
                    f"{type(error).__name__}: {error}"
                )

        timer = threading.Timer(delay_seconds, clear)
        timer.daemon = True
        timer.start()

    def handle_player_attack(
        self,
        payload: bytes,
        *,
        player_being_name: str,
    ) -> None:
        nul_index = payload.find(b"\x00")
        if nul_index < 0:
            print(
                "[mob-life] invalid 0x8014 without target C string: "
                f"{payload.hex(' ')}"
            )
            return
        try:
            target_name = payload[:nul_index].decode("ascii")
        except UnicodeDecodeError:
            print(
                "[mob-life] non-ASCII 0x8014 target: "
                f"{payload.hex(' ')}"
            )
            return
        tail = payload[nul_index + 1:]
        attack_mode = int.from_bytes(tail[-2:], "big") if len(tail) >= 2 else None
        print(
            f"[mob-life] 0x8014: target={target_name!r}, "
            f"mode={attack_mode}, tail={tail.hex(' ')}"
        )

        if attack_mode == 1:
            with self._lock:
                state = self._states.get(target_name)
                valid = bool(
                    state is not None
                    and state.get("present", False)
                    and state.get("alive", False)
                )
            if not valid:
                print(
                    f"[player-attack] mode=1 ignored; invalid target "
                    f"{target_name!r}"
                )
                return
            if not player_being_name:
                print("[player-attack] mode=1 ignored; empty player Being name")
                return
            with self._lock:
                self._player_attack_started_at = time.monotonic()
            self._send_attack_relation(
                player_being_name, target_name, "start"
            )
            return

        if attack_mode == 3:
            with self._lock:
                self._player_attack_started_at = None
            if player_being_name:
                self._send_attack_relation(
                    player_being_name, "", "cancel"
                )
            return

        if attack_mode != 2:
            print(
                f"[player-attack] unsupported 0x8014 mode={attack_mode!r}; ignored"
            )
            return

        with self._lock:
            attack_started_at = self._player_attack_started_at
            self._player_attack_started_at = None
            state = self._states.get(target_name)
            if state is None:
                print(f"[mob-life] unknown runtime target: {target_name!r}")
                return
            if not bool(state.get("present", False)):
                print(f"[mob-life] target absent: {target_name!r}")
                return
            if not bool(state.get("alive", False)):
                print(f"[mob-life] target already dead: {target_name!r}")
                return

            current_x, current_y, _ = self._update_estimated_position_locked(state)
            entering_new_combat = str(state.get("combat_state", "IDLE")) != "COMBAT"
            if entering_new_combat:
                anchor_x, anchor_y = self._normalize_combat_anchor_locked(
                    state,
                    int(round(current_x)),
                    int(round(current_y)),
                )
                state["combat_anchor_valid"] = True
                state["combat_anchor_x"] = anchor_x
                state["combat_anchor_y"] = anchor_y
                state["combat_anchor_direction"] = int(
                    state.get("current_direction", state.get("spawn_direction", 0)) or 0
                )
                # Cancel all pending idle-wander callbacks.  The original spawn
                # point remains unchanged and will again become the roaming
                # center after this combat ends.
                state["wander_generation"] = int(
                    state.get("wander_generation", 0)
                ) + 1
                print(
                    f"[mob-ai] combat anchor captured: mob={target_name!r}, "
                    f"anchor=({anchor_x},{anchor_y}), "
                    f"spawn=({state.get('spawn_x')},{state.get('spawn_y')})"
                )
            old_hp = int(state["current_hp"])
            max_hp = int(state["max_hp"])
            attack_profile = dict(self._get_player_attack_profile())
            attack_min = max(0, int(attack_profile.get("attack_min", 0)))
            attack_max = max(
                attack_min, int(attack_profile.get("attack_max", attack_min))
            )
            attack_roll = roll_attack_value(attack_min, attack_max)
            target_defense = max(0, int(state.get("defense_power", 0) or 0))
            damage = calculate_physical_damage(attack_roll, target_defense)
            new_hp = max(0, old_hp - damage)
            state["current_hp"] = new_hp
            died = new_hp <= 0
            if died:
                state["alive"] = False
                state["corpse"] = True
                state["combat_state"] = "DEAD"
                state["target_player_being_name"] = ""
                state["attack_loop_running"] = False
                state["attack_loop_generation"] = int(
                    state.get("attack_loop_generation", 0)
                ) + 1
                state["movement_active"] = False
                state["movement_generation"] = int(
                    state.get("movement_generation", 0)
                ) + 1
                state["movement_phase"] = "DEAD"
                state["wander_generation"] = int(
                    state.get("wander_generation", 0)
                ) + 1
            elif player_being_name and str(
                state.get("aggro_mode", "RETALIATE")
            ).upper() != "NONE":
                state["combat_state"] = "COMBAT"
                state["target_player_being_name"] = player_being_name
                player = dict(self._player)
                self._update_facing_locked(
                    state,
                    int(player.get("x", 0) or 0),
                    int(player.get("y", 0) or 0),
                )
            should_schedule_death = died and not bool(
                state.get("death_sequence_scheduled", False)
            )
            reward_request: dict[str, object] | None = None
            if should_schedule_death:
                state["death_sequence_scheduled"] = True
                if not bool(state.get("reward_granted", False)):
                    state["reward_granted"] = True
                    gold_min = max(0, int(state.get("gold_min", 0) or 0))
                    gold_max = max(
                        gold_min, int(state.get("gold_max", gold_min) or gold_min)
                    )
                    reward_request = {
                        "mob_being_name": str(state.get("being_name", target_name)),
                        "mob_display_name": str(
                            state.get("display_name", target_name)
                        ),
                        "spawn_id": int(state.get("spawn_id", 0) or 0),
                        "mob_level": max(1, int(state.get("level", 1) or 1)),
                        "experience_reward": max(
                            0, int(state.get("experience_reward", 0) or 0)
                        ),
                        "gold_min": gold_min,
                        "gold_max": gold_max,
                        "gold_reward": (
                            gold_min
                            if gold_min == gold_max
                            else random.randint(gold_min, gold_max)
                        ),
                    }

        print(
            f"[mob-life] damage: target={target_name!r}, "
            f"attack_roll={attack_roll} range={attack_min}-{attack_max}, "
            f"weapon={int(attack_profile.get('weapon_min', 0))}-"
            f"{int(attack_profile.get('weapon_max', 0))}, "
            f"defense={target_defense}, damage={damage}, "
            f"hp={old_hp}->{new_hp}/{max_hp}, "
            f"field20={hp_percent(new_hp, max_hp)}, dead={died}"
        )
        self._send_mob_state(
            state,
            (
                f"game 0x0005 mob-hp target={target_name!r} "
                f"damage={damage} hp={old_hp}->{new_hp}/{max_hp}"
            ),
            incremental_live_update=not died,
        )
        if player_being_name and damage > 0:
            self._send_type1(
                0x0035,
                build_damage_display_0035_payload(
                    player_being_name,
                    target_name,
                    damage,
                    target_hp_percent=hp_percent(new_hp, max_hp),
                ),
                (
                    f"game 0x0035 player-damage-display "
                    f"source={player_being_name!r} target={target_name!r} "
                    f"damage={damage}"
                ),
            )
        if player_being_name:
            self._schedule_attack_clear(
                player_being_name, attack_started_at=attack_started_at
            )

        if (
            not died
            and player_being_name
            and str(state.get("aggro_mode", "RETALIATE")).upper() != "NONE"
        ):
            self._ensure_attack_loop(
                target_name, player_being_name
            )
        if reward_request is not None:
            experience_reward = int(reward_request["experience_reward"])
            gold_reward = int(reward_request["gold_reward"])
            if self._grant_mob_kill_reward is None:
                print(
                    f"[mob-reward] no reward callback: mob={target_name!r}, "
                    f"experience={experience_reward}, gold={gold_reward}"
                )
            else:
                try:
                    result = dict(self._grant_mob_kill_reward(reward_request) or {})
                    print(
                        f"[mob-reward] committed: mob={target_name!r}, "
                        f"experience={experience_reward}, gold={gold_reward}, "
                        f"result={result}"
                    )
                except Exception as error:
                    print(
                        f"[mob-reward] failed for {target_name!r}: "
                        f"{type(error).__name__}: {error}"
                    )
        if should_schedule_death:
            self._schedule_death_sequence(target_name)

    def _send_counterattack_burst(
        self,
        state: dict[str, object],
        player_being_name: str,
    ) -> None:
        mob_being_name = str(state["being_name"])
        reset_payload = build_action_0036_payload(
            mob_being_name,
            "",
            action_type=0,
        )
        attack_payload = build_action_0036_payload(
            mob_being_name,
            player_being_name,
            field10=int(state.get("attack_effect_field10", 0) or 0),
            action_type=int(state.get("action_type", 1) or 1),
        )
        plain = (
            build_type1_plain(0x0036, reset_payload)
            + build_type1_plain(0x0036, attack_payload)
        )
        self._send_plain(
            plain,
            (
                f"game 0x0036x2 mob-attack actor={mob_being_name!r} "
                f"target={player_being_name!r}"
            ),
        )

    def _ensure_attack_loop(
        self,
        mob_being_name: str,
        player_being_name: str,
    ) -> None:
        with self._lock:
            state = self._states.get(mob_being_name)
            if state is None:
                return
            if str(state.get("aggro_mode", "RETALIATE")) == "NONE":
                return
            state["combat_state"] = "COMBAT"
            state["target_player_being_name"] = player_being_name
            if bool(state.get("attack_loop_running", False)):
                return
            generation = int(state.get("attack_loop_generation", 0)) + 1
            state["attack_loop_generation"] = generation
            state["attack_loop_running"] = True
            first_delay = float(state.get("first_attack_delay", 0.8) or 0.8)
            session_generation = self._session_generation
        print(
            f"[mob-ai] start attack loop: mob={mob_being_name!r}, "
            f"first_delay={first_delay:.2f}s"
        )

        def schedule_next(delay_seconds: float) -> None:
            timer = threading.Timer(max(0.05, delay_seconds), attack_tick)
            timer.daemon = True
            timer.start()

        def attack_tick() -> None:
            try:
                with self._lock:
                    if self._closed or self._session_generation != session_generation:
                        return
                    current = self._states.get(mob_being_name)
                    if current is None:
                        return
                    if int(current.get("attack_loop_generation", 0)) != generation:
                        return
                    if not bool(current.get("attack_loop_running", False)):
                        return
                    if str(current.get("combat_state", "IDLE")) != "COMBAT":
                        current["attack_loop_running"] = False
                        return
                    if not bool(current.get("present", False)) or not bool(
                        current.get("alive", False)
                    ):
                        current["attack_loop_running"] = False
                        return
                    target = str(
                        current.get("target_player_being_name", "")
                        or player_being_name
                    )
                    current_x, current_y, moving = self._update_estimated_position_locked(
                        current
                    )
                    player = dict(self._player)
                    same_map = int(player.get("map_id", 0) or 0) == int(
                        current.get("map_id", 0) or 0
                    )
                    dist = (
                        distance_tiles(
                            current_x,
                            current_y,
                            int(player.get("x", 0) or 0),
                            int(player.get("y", 0) or 0),
                        )
                        if same_map
                        else float("inf")
                    )
                    attack_range = float(current.get("attack_range", 2.0) or 2.0)
                    interval = float(current.get("attack_interval", 2.5) or 2.5)
                    if dist <= attack_range and not moving:
                        self._update_facing_locked(
                            current,
                            int(player.get("x", 0) or 0),
                            int(player.get("y", 0) or 0),
                        )
                        should_attack = True
                    else:
                        should_attack = False

                if should_attack:
                    self._send_counterattack_burst(current, target)
                    attack_min = max(0, int(current.get("attack_min", 0) or 0))
                    attack_max = max(
                        attack_min, int(current.get("attack_max", attack_min) or attack_min)
                    )
                    attack_roll = random.randint(attack_min, attack_max)
                    damage_result: dict[str, object] = {}
                    if self._apply_player_damage is not None:
                        try:
                            damage_result = dict(
                                self._apply_player_damage(current, attack_roll) or {}
                            )
                        except Exception as error:
                            print(
                                f"[mob-damage] apply failed: mob={mob_being_name!r}, "
                                f"attack_roll={attack_roll}, "
                                f"error={type(error).__name__}: {error}"
                            )
                    applied_damage = max(
                        0, int(damage_result.get("applied_damage", 0) or 0)
                    )
                    if applied_damage > 0:
                        self._send_type1(
                            0x0035,
                            build_damage_display_0035_payload(
                                mob_being_name,
                                target,
                                applied_damage,
                                target_hp_percent=hp_percent(
                                    int(damage_result.get("hp_after", 0) or 0),
                                    int(damage_result.get("max_hp", 1) or 1),
                                ),
                            ),
                            (
                                f"game 0x0035 mob-damage-display "
                                f"source={mob_being_name!r} target={target!r} "
                                f"damage={applied_damage}"
                            ),
                        )
                    print(
                        f"[mob-ai] attack tick: mob={mob_being_name!r}, "
                        f"target={target!r}, distance={dist:.2f}, "
                        f"attack_roll={attack_roll}, "
                        f"applied_damage={damage_result.get('applied_damage', '<not-applied>')}, "
                        f"player_hp={damage_result.get('hp_after', '<unknown>')}"
                    )
                else:
                    print(
                        f"[mob-ai] wait/moving: mob={mob_being_name!r}, "
                        f"distance={dist:.2f}, moving={moving}"
                    )

                with self._lock:
                    latest = self._states.get(mob_being_name)
                    keep_running = bool(
                        latest is not None
                        and self._session_generation == session_generation
                        and int(latest.get("attack_loop_generation", 0)) == generation
                        and latest.get("attack_loop_running", False)
                        and latest.get("combat_state") == "COMBAT"
                        and latest.get("alive", False)
                        and latest.get("present", False)
                    )
                if keep_running:
                    schedule_next(interval)
            except Exception as error:
                return_action: tuple[dict[str, object], int, int] | None = None
                with self._lock:
                    latest = self._states.get(mob_being_name)
                    if (
                        latest is not None
                        and int(latest.get("attack_loop_generation", 0))
                        == generation
                        and str(latest.get("combat_state", "IDLE")) == "COMBAT"
                    ):
                        anchor_x, anchor_y = self._begin_return_locked(
                            latest,
                            reason=(
                                "attack loop exception "
                                f"{type(error).__name__}: {error}"
                            ),
                        )
                        return_action = (latest, anchor_x, anchor_y)
                    elif latest is not None:
                        latest["attack_loop_running"] = False
                        latest["attack_loop_generation"] = int(
                            latest.get("attack_loop_generation", 0)
                        ) + 1
                print(
                    f"[mob-ai] attack loop failed for {mob_being_name!r}: "
                    f"{type(error).__name__}: {error}; "
                    "forcing safe disengage instead of leaving stale COMBAT"
                )

                if return_action is not None:
                    latest, anchor_x, anchor_y = return_action
                    try:
                        return_generation, return_duration = self._send_move(
                            latest, anchor_x, anchor_y, "RETURN"
                        )
                        self._schedule_return_completion(
                            mob_being_name,
                            return_generation,
                            return_duration + MOB_MOVE_RETURN_SETTLE_SECONDS,
                        )
                    except Exception as return_error:
                        # A broken connection can also make the recovery packet
                        # fail.  Clear the server-side state anyway so neither
                        # regeneration nor future map loads can be trapped in a
                        # permanent COMBAT state.
                        with self._lock:
                            newest = self._states.get(mob_being_name)
                            if newest is not None:
                                self._settle_return_locally_locked(
                                    newest,
                                    reason=(
                                        "return after attack-loop failure also "
                                        f"failed: {type(return_error).__name__}: "
                                        f"{return_error}"
                                    ),
                                )

        schedule_next(first_delay)

    def handle_player_movement(self, payload: bytes, *, map_id: int) -> None:
        if len(payload) != 8:
            print(
                f"[mob-ai] invalid 0x8005 length={len(payload)}: "
                f"{payload.hex(' ')}"
            )
            return
        packed_position = int.from_bytes(payload[0:4], "big")
        position_x = (packed_position >> 16) & 0xFFFF
        position_y = packed_position & 0xFFFF
        direction = int.from_bytes(payload[5:7], "big")
        self._update_player_position(
            map_id=int(map_id),
            x=position_x,
            y=position_y,
            direction=direction,
        )
        print(
            f"[mob-ai] player position: map={int(map_id)}, "
            f"pos=({position_x},{position_y}), direction={direction}"
        )

        pending: list[tuple[dict[str, object], int, int, str]] = []
        now_value = time.monotonic()
        with self._lock:
            for state in self._states.values():
                if str(state.get("combat_state", "IDLE")) != "COMBAT":
                    continue
                if not bool(state.get("alive", False)) or not bool(
                    state.get("present", False)
                ):
                    continue
                current_x, current_y, _ = self._update_estimated_position_locked(
                    state, now_value
                )
                mob_x = int(round(current_x))
                mob_y = int(round(current_y))
                anchor_x = int(state.get("combat_anchor_x", mob_x) or mob_x)
                anchor_y = int(state.get("combat_anchor_y", mob_y) or mob_y)
                dist_to_mob = distance_tiles(position_x, position_y, mob_x, mob_y)
                dist_to_anchor = distance_tiles(
                    position_x, position_y, anchor_x, anchor_y
                )
                leash_range = float(state.get("leash_range", 20.0) or 20.0)
                attack_range = float(state.get("attack_range", 2.0) or 2.0)
                self._update_facing_locked(state, position_x, position_y)

                if dist_to_anchor > leash_range:
                    anchor_x, anchor_y = self._begin_return_locked(
                        state,
                        reason="leash exceeded during player movement",
                    )
                    pending.append((state, anchor_x, anchor_y, "RETURN"))
                    print(
                        f"[mob-ai] disengage: mob={state.get('being_name')!r}, "
                        f"player_anchor_distance={dist_to_anchor:.2f}, "
                        f"leash={leash_range:.2f}, "
                        f"return_anchor=({anchor_x},{anchor_y})"
                    )
                    continue
                if dist_to_mob <= attack_range:
                    state["movement_phase"] = "IN_ATTACK_RANGE"
                    continue

                destination_x, destination_y = choose_adjacent_destination(
                    mob_x, mob_y, position_x, position_y
                )
                last_destination = state.get("last_move_destination")
                last_command_at = float(state.get("last_move_command_at", 0.0) or 0.0)
                if (
                    last_destination != (destination_x, destination_y)
                    and now_value - last_command_at >= MOB_MOVE_COMMAND_MIN_INTERVAL_SECONDS
                ):
                    pending.append((state, destination_x, destination_y, "CHASE"))

        for state, destination_x, destination_y, phase in pending:
            generation, duration = self._send_move(
                state, destination_x, destination_y, phase
            )
            if phase == "RETURN":
                self._schedule_return_completion(
                    str(state["being_name"]),
                    generation,
                    duration + MOB_MOVE_RETURN_SETTLE_SECONDS,
                )
            else:
                self._schedule_chase_recheck(
                    str(state["being_name"]),
                    generation,
                    duration + MOB_MOVE_CHASE_RECHECK_SETTLE_SECONDS,
                )

    def _send_move(
        self,
        state: dict[str, object],
        destination_x: int,
        destination_y: int,
        phase: str,
        *,
        movement_speed: int | None = None,
    ) -> tuple[int, float]:
        with self._lock:
            speed = int(
                movement_speed
                if movement_speed is not None
                else state.get("move_speed", MOB_MOVE_BASE_SPEED)
                or MOB_MOVE_BASE_SPEED
            )
            estimated_tiles_per_second = max(
                0.10,
                MOB_MOVE_BASE_TILES_PER_SECOND
                * float(speed)
                / float(MOB_MOVE_BASE_SPEED),
            )
            generation, duration = self._start_estimated_move_locked(
                state,
                destination_x,
                destination_y,
                phase=phase,
                tiles_per_second=estimated_tiles_per_second,
            )
            network_id = int(state.get("network_id", 0) or 0)
            being_name = str(state.get("being_name", "") or "")
            network_visible = bool(state.get("network_visible", True))
        if network_visible:
            self._send_type1(
                0x0004,
                build_mob_move_0004_payload(
                    network_id,
                    destination_x,
                    destination_y,
                    movement_speed=speed,
                ),
                (
                    f"game 0x0004 mob-{phase.lower()} being={being_name!r} "
                    f"network_id={network_id} "
                    f"destination=({destination_x},{destination_y}) "
                    f"speed={speed} duration={duration:.2f}s"
                ),
            )
        return generation, duration

    def _schedule_chase_recheck(
        self,
        being_name: str,
        generation: int,
        delay_seconds: float,
    ) -> None:
        session_generation = self._session_generation

        def recheck() -> None:
            try:
                action: tuple[dict[str, object], int, int, str] | None = None
                with self._lock:
                    if self._closed or self._session_generation != session_generation:
                        return
                    state = self._states.get(being_name)
                    if state is None:
                        return
                    if int(state.get("movement_generation", 0)) != generation:
                        return
                    if str(state.get("combat_state", "IDLE")) != "COMBAT":
                        return
                    current_x, current_y, moving = self._update_estimated_position_locked(
                        state
                    )
                    if moving:
                        retry = threading.Timer(0.10, recheck)
                        retry.daemon = True
                        retry.start()
                        return
                    player = dict(self._player)
                    mob_x = int(round(current_x))
                    mob_y = int(round(current_y))
                    player_x = int(player.get("x", 0) or 0)
                    player_y = int(player.get("y", 0) or 0)
                    anchor_x = int(state.get("combat_anchor_x", mob_x) or mob_x)
                    anchor_y = int(state.get("combat_anchor_y", mob_y) or mob_y)
                    dist_to_mob = distance_tiles(player_x, player_y, mob_x, mob_y)
                    dist_to_anchor = distance_tiles(
                        player_x, player_y, anchor_x, anchor_y
                    )
                    leash_range = float(state.get("leash_range", 20.0) or 20.0)
                    attack_range = float(state.get("attack_range", 2.0) or 2.0)
                    if dist_to_anchor > leash_range:
                        anchor_x, anchor_y = self._begin_return_locked(
                            state,
                            reason="leash exceeded during chase arrival recheck",
                        )
                        action = (state, anchor_x, anchor_y, "RETURN")
                    elif dist_to_mob <= attack_range:
                        state["movement_phase"] = "IN_ATTACK_RANGE"
                        self._update_facing_locked(state, player_x, player_y)
                        return
                    else:
                        destination_x, destination_y = choose_adjacent_destination(
                            mob_x, mob_y, player_x, player_y
                        )
                        action = (state, destination_x, destination_y, "CHASE")

                if action is None:
                    return
                state, destination_x, destination_y, phase = action
                next_generation, duration = self._send_move(
                    state, destination_x, destination_y, phase
                )
                if phase == "RETURN":
                    self._schedule_return_completion(
                        being_name,
                        next_generation,
                        duration + MOB_MOVE_RETURN_SETTLE_SECONDS,
                    )
                else:
                    self._schedule_chase_recheck(
                        being_name,
                        next_generation,
                        duration + MOB_MOVE_CHASE_RECHECK_SETTLE_SECONDS,
                    )
            except Exception as error:
                print(
                    f"[mob-ai] chase recheck failed for {being_name!r}: "
                    f"{type(error).__name__}: {error}"
                )

        timer = threading.Timer(max(0.05, delay_seconds), recheck)
        timer.daemon = True
        timer.start()

    def _schedule_return_completion(
        self,
        being_name: str,
        generation: int,
        delay_seconds: float,
    ) -> None:
        session_generation = self._session_generation

        def finish() -> None:
            try:
                with self._lock:
                    if self._closed or self._session_generation != session_generation:
                        return
                    state = self._states.get(being_name)
                    if state is None:
                        return
                    if int(state.get("movement_generation", 0)) != generation:
                        return
                    if str(state.get("combat_state", "IDLE")) != "RETURNING":
                        return
                    anchor_x = int(
                        state.get("combat_anchor_x", state.get("spawn_x", 0)) or 0
                    )
                    anchor_y = int(
                        state.get("combat_anchor_y", state.get("spawn_y", 0)) or 0
                    )
                    state["current_x"] = anchor_x
                    state["current_y"] = anchor_y
                    state["current_x_float"] = float(anchor_x)
                    state["current_y_float"] = float(anchor_y)
                    state["movement_active"] = False
                    state["movement_phase"] = "IDLE"
                    state["combat_state"] = "IDLE"
                    state["target_player_being_name"] = ""
                    state["current_direction"] = int(
                        state.get(
                            "combat_anchor_direction",
                            state.get("spawn_direction", 0),
                        )
                        or 0
                    )
                    state["current_hp"] = int(state["max_hp"])
                    state["last_move_destination"] = None
                    state["combat_anchor_valid"] = False
                    state["wander_generation"] = int(
                        state.get("wander_generation", 0)
                    ) + 1
                self._send_mob_state(
                    state,
                    f"game 0x0005 mob-return-complete {being_name!r}",
                )
                print(
                    f"[mob-ai] return complete: mob={being_name!r}, "
                    f"combat_anchor=({anchor_x},{anchor_y}), hp=full"
                )
                self._schedule_idle_wander(being_name)
            except Exception as error:
                print(
                    f"[mob-ai] return completion failed for {being_name!r}: "
                    f"{type(error).__name__}: {error}"
                )

        timer = threading.Timer(max(0.05, delay_seconds), finish)
        timer.daemon = True
        timer.start()

    def _schedule_death_sequence(self, being_name: str) -> None:
        """Schedule corpse deletion and respawn from the moment of death.

        ``corpse_seconds`` is the death-to-delete delay.
        ``respawn_seconds`` is the death-to-respawn delay, not an additional
        wait after corpse deletion.  With the normal defaults this means the
        corpse disappears at 5 s and the monster returns at 30 s.
        """
        with self._lock:
            state = self._states.get(being_name)
            if state is None:
                return
            corpse_seconds = float(state.get("corpse_seconds", 5.0) or 0.0)
            respawn_seconds = float(state.get("respawn_seconds", 30.0) or 0.0)
            session_generation = self._session_generation

        # A respawn cannot safely precede deletion of the old Being.  This only
        # affects unusual database values where respawn <= corpse time.
        effective_respawn_delay = max(
            respawn_seconds, corpse_seconds + 0.05
        )
        print(
            f"[mob-life] death timers: mob={being_name!r}, "
            f"corpse_delete={corpse_seconds:.2f}s, "
            f"respawn_from_death={effective_respawn_delay:.2f}s"
        )

        def delete_corpse() -> None:
            try:
                with self._lock:
                    if self._closed or self._session_generation != session_generation:
                        return
                    state = self._states.get(being_name)
                    if state is None or bool(state.get("alive", False)):
                        return
                    if not bool(state.get("present", False)):
                        return
                    state["present"] = False
                    state["corpse"] = False
                self._send_delete(
                    being_name,
                    f"game 0x0007 mob-delete-corpse {being_name!r}",
                )
            except Exception as error:
                print(
                    f"[mob-life] corpse delete failed for {being_name!r}: "
                    f"{type(error).__name__}: {error}"
                )

        def respawn() -> None:
            try:
                with self._lock:
                    if self._closed or self._session_generation != session_generation:
                        return
                    state = self._states.get(being_name)
                    if state is None:
                        return
                    spawn_x = int(state.get("spawn_x", 0) or 0)
                    spawn_y = int(state.get("spawn_y", 0) or 0)
                    state["current_hp"] = int(state["max_hp"])
                    state["alive"] = True
                    state["present"] = True
                    state["corpse"] = False
                    state["death_sequence_scheduled"] = False
                    state["reward_granted"] = False
                    state["combat_state"] = "IDLE"
                    state["target_player_being_name"] = ""
                    state["current_direction"] = int(
                        state.get("spawn_direction", 0) or 0
                    )
                    state["attack_loop_running"] = False
                    state["attack_loop_generation"] = int(
                        state.get("attack_loop_generation", 0)
                    ) + 1
                    state["current_x"] = spawn_x
                    state["current_y"] = spawn_y
                    state["current_x_float"] = float(spawn_x)
                    state["current_y_float"] = float(spawn_y)
                    state["movement_active"] = False
                    state["movement_generation"] = int(
                        state.get("movement_generation", 0)
                    ) + 1
                    state["movement_phase"] = "IDLE"
                    state["last_move_destination"] = None
                    state["combat_anchor_valid"] = False
                    state["combat_anchor_x"] = spawn_x
                    state["combat_anchor_y"] = spawn_y
                    state["combat_anchor_direction"] = int(
                        state.get("spawn_direction", 0) or 0
                    )
                    state["wander_generation"] = int(
                        state.get("wander_generation", 0)
                    ) + 1
                    enter_radius = self._visibility_enter_radius
                    leave_radius = self._visibility_leave_radius
                    player_x = int(self._player.get("x", 0) or 0)
                    player_y = int(self._player.get("y", 0) or 0)
                    if enter_radius is None or leave_radius is None:
                        # Backward-compatible behavior when visibility
                        # management is not enabled by the caller.
                        state["network_visible"] = True
                if enter_radius is None or leave_radius is None:
                    self._send_mob_state(
                        state,
                        f"game 0x0005 mob-respawn {being_name!r}",
                    )
                else:
                    self.sync_network_visibility(
                        player_x=player_x,
                        player_y=player_y,
                        enter_radius=enter_radius,
                        leave_radius=leave_radius,
                    )
                print(f"[mob-life] respawned: {being_name!r}")
                self._schedule_idle_wander(being_name)
            except Exception as error:
                print(
                    f"[mob-life] respawn failed for {being_name!r}: "
                    f"{type(error).__name__}: {error}"
                )

        corpse_timer = threading.Timer(max(0.05, corpse_seconds), delete_corpse)
        corpse_timer.daemon = True
        corpse_timer.start()

        respawn_timer = threading.Timer(
            max(0.05, effective_respawn_delay), respawn
        )
        respawn_timer.daemon = True
        respawn_timer.start()
