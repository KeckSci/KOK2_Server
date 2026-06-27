from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from mob_database import decode_u8_grid, validate_binary_mask


class SpawnGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpawnPoint:
    population_id: int
    population_key: str
    template_id: int
    slot: int
    x: int
    y: int
    direction: int
    locked: bool = False


@dataclass(frozen=True)
class RegionGenerationResult:
    region_id: int
    region_key: str
    map_id: int
    requested_count: int
    points: tuple[SpawnPoint, ...]
    disabled_rows: int
    candidate_cells: int
    preview: bool


def _distance(ax: int, ay: int, bx: int, by: int) -> float:
    return math.hypot(int(ax) - int(bx), int(ay) - int(by))


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (str(table_name),),
    ).fetchone() is not None


def _load_npc_points(connection: sqlite3.Connection, map_id: int) -> list[tuple[int, int]]:
    table_name = f"map_npcs_{int(map_id)}"
    if not _table_exists(connection, table_name):
        return []
    rows = connection.execute(
        f"SELECT position_x, position_y FROM {table_name} WHERE enabled != 0"
    ).fetchall()
    return [(int(row[0]), int(row[1])) for row in rows]


def _load_teleport_points(connection: sqlite3.Connection, map_id: int) -> list[tuple[int, int]]:
    if not _table_exists(connection, "map_teleports"):
        return []
    rows = connection.execute(
        """
        SELECT trigger_x AS x, trigger_y AS y
        FROM map_teleports
        WHERE source_map_id = ? AND enabled != 0
        UNION ALL
        SELECT target_x AS x, target_y AS y
        FROM map_teleports
        WHERE target_map_id = ? AND enabled != 0
        """,
        (int(map_id), int(map_id)),
    ).fetchall()
    return [(int(row[0]), int(row[1])) for row in rows]


def _load_region(connection: sqlite3.Connection, region_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM mob_spawn_regions WHERE region_id = ?",
        (int(region_id),),
    ).fetchone()
    if row is None:
        raise SpawnGenerationError(f"region not found: region_id={region_id}")
    return row


def _load_collision(connection: sqlite3.Connection, map_id: int) -> tuple[int, int, int, bytes]:
    row = connection.execute(
        "SELECT * FROM map_collision_grids WHERE map_id = ?",
        (int(map_id),),
    ).fetchone()
    if row is None:
        raise SpawnGenerationError(
            f"map collision grid is missing: map_id={map_id}"
        )
    width = int(row["width"])
    height = int(row["height"])
    raw = decode_u8_grid(
        row["grid_data"],
        str(row["encoding"]),
        expected_size=width * height,
    )
    return width, height, int(row["walkable_value"]), raw


def _load_region_mask(region: sqlite3.Row) -> tuple[int, int, bytes]:
    width = int(region["mask_width"])
    height = int(region["mask_height"])
    raw = decode_u8_grid(
        region["mask_data"],
        str(region["mask_encoding"]),
        expected_size=width * height,
    )
    validate_binary_mask(raw)
    return width, height, raw


def _candidate_cells(
    *,
    width: int,
    height: int,
    mask: bytes,
    collision: bytes,
    walkable_value: int,
) -> list[tuple[int, int]]:
    return [
        (index % width, index // width)
        for index, mask_value in enumerate(mask)
        if mask_value != 0 and int(collision[index]) == int(walkable_value)
    ]


def _is_far_enough(
    x: int,
    y: int,
    points: Sequence[tuple[int, int]],
    minimum: float,
) -> bool:
    if minimum <= 0:
        return True
    return all(_distance(x, y, px, py) >= minimum for px, py in points)


def _direction_for(region: sqlite3.Row, rng: random.Random) -> int:
    if str(region["direction_mode"]).upper() == "FIXED":
        return int(region["fixed_direction"]) & 0xFFFF
    return rng.randrange(0, 360)


def generate_region(
    connection: sqlite3.Connection,
    region_id: int,
    *,
    preview: bool = False,
) -> RegionGenerationResult:
    """Synchronize every monster population inside one irregular region."""
    connection.row_factory = sqlite3.Row
    region = _load_region(connection, int(region_id))
    map_id = int(region["map_id"])
    region_key = str(region["region_key"])

    populations = connection.execute(
        """
        SELECT p.*, t.enabled AS template_enabled, t.display_name
        FROM mob_region_populations AS p
        JOIN mob_templates AS t ON t.template_id = p.template_id
        WHERE p.region_id = ?
        ORDER BY p.sort_order, p.population_id
        """,
        (int(region_id),),
    ).fetchall()

    existing_rows = connection.execute(
        """
        SELECT *
        FROM map_mob_spawns
        WHERE region_id = ? AND source_type = 'GENERATED'
        ORDER BY population_id, population_slot, spawn_id
        """,
        (int(region_id),),
    ).fetchall()
    existing_by_key = {
        (int(row["population_id"]), int(row["population_slot"])): row
        for row in existing_rows
        if row["population_id"] is not None and row["population_slot"] is not None
    }

    active_populations = [
        row for row in populations
        if int(region["enabled"] or 0) != 0
        and int(row["enabled"] or 0) != 0
        and int(row["template_enabled"] or 0) != 0
        and int(row["spawn_count"] or 0) > 0
    ]
    requested_count = sum(int(row["spawn_count"]) for row in active_populations)

    if not active_populations:
        if not preview:
            cursor = connection.execute(
                """
                UPDATE map_mob_spawns
                SET enabled=0, updated_at=CURRENT_TIMESTAMP
                WHERE region_id=? AND source_type='GENERATED'
                """,
                (int(region_id),),
            )
            disabled_rows = max(0, int(cursor.rowcount))
        else:
            disabled_rows = len(existing_rows)
        return RegionGenerationResult(
            region_id=int(region_id),
            region_key=region_key,
            map_id=map_id,
            requested_count=0,
            points=tuple(),
            disabled_rows=disabled_rows,
            candidate_cells=0,
            preview=bool(preview),
        )

    collision_width, collision_height, walkable_value, collision = _load_collision(
        connection, map_id
    )
    mask_width, mask_height, mask = _load_region_mask(region)
    if (mask_width, mask_height) != (collision_width, collision_height):
        raise SpawnGenerationError(
            "region mask and collision dimensions differ: "
            f"region={mask_width}x{mask_height}, "
            f"collision={collision_width}x{collision_height}"
        )

    candidates = _candidate_cells(
        width=collision_width,
        height=collision_height,
        mask=mask,
        collision=collision,
        walkable_value=walkable_value,
    )
    rng = random.Random((int(region["random_seed"]) << 32) ^ int(region_id))
    rng.shuffle(candidates)

    npc_points = _load_npc_points(connection, map_id)
    teleport_points = _load_teleport_points(connection, map_id)
    min_distance = float(region["min_distance"] or 0.0)
    avoid_npc_distance = float(region["avoid_npc_distance"] or 0.0)
    avoid_teleport_distance = float(region["avoid_teleport_distance"] or 0.0)

    # Reserve active manual spawns and generated monsters from other regions.
    reserved_rows = connection.execute(
        """
        SELECT position_x, position_y
        FROM map_mob_spawns
        WHERE map_id = ? AND enabled != 0
          AND (region_id IS NULL OR region_id != ?)
        """,
        (map_id, int(region_id)),
    ).fetchall()
    reserved_points = [(int(row[0]), int(row[1])) for row in reserved_rows]

    points: list[SpawnPoint] = []
    accepted_points: list[tuple[int, int]] = []
    active_keys: set[tuple[int, int]] = set()

    # Locked rows remain administrator-authoritative and are placed first.
    for population in active_populations:
        population_id = int(population["population_id"])
        population_key = str(population["population_key"])
        template_id = int(population["template_id"])
        for slot in range(1, int(population["spawn_count"]) + 1):
            key = (population_id, slot)
            active_keys.add(key)
            existing = existing_by_key.get(key)
            if existing is None or int(existing["locked"] or 0) == 0:
                continue
            point = SpawnPoint(
                population_id=population_id,
                population_key=population_key,
                template_id=template_id,
                slot=slot,
                x=int(existing["position_x"]),
                y=int(existing["position_y"]),
                direction=int(existing["direction"] or 0),
                locked=True,
            )
            points.append(point)
            accepted_points.append((point.x, point.y))

    occupied_slots = {(point.population_id, point.slot) for point in points}
    candidate_index = 0
    for population in active_populations:
        population_id = int(population["population_id"])
        population_key = str(population["population_key"])
        template_id = int(population["template_id"])
        direction_rng = random.Random(
            int(region["random_seed"])
            + int(population["random_seed_offset"] or 0)
            + population_id * 1000003
        )
        for slot in range(1, int(population["spawn_count"]) + 1):
            if (population_id, slot) in occupied_slots:
                continue
            placed: SpawnPoint | None = None
            while candidate_index < len(candidates):
                x, y = candidates[candidate_index]
                candidate_index += 1
                if not _is_far_enough(x, y, npc_points, avoid_npc_distance):
                    continue
                if not _is_far_enough(x, y, teleport_points, avoid_teleport_distance):
                    continue
                if not _is_far_enough(x, y, reserved_points, min_distance):
                    continue
                if not _is_far_enough(x, y, accepted_points, min_distance):
                    continue
                placed = SpawnPoint(
                    population_id=population_id,
                    population_key=population_key,
                    template_id=template_id,
                    slot=slot,
                    x=x,
                    y=y,
                    direction=_direction_for(region, direction_rng),
                    locked=False,
                )
                accepted_points.append((x, y))
                points.append(placed)
                break
            if placed is None:
                raise SpawnGenerationError(
                    "unable to place requested monsters: "
                    f"region={region_key!r}, placed={len(points)}/{requested_count}, "
                    f"candidates={len(candidates)}, min_distance={min_distance}. "
                    "Reduce spawn_count/min_distance or enlarge the painted region."
                )

    points.sort(key=lambda item: (item.population_id, item.slot))

    disabled_rows = sum(
        1
        for row in existing_rows
        if (int(row["population_id"]), int(row["population_slot"])) not in active_keys
    )

    if not preview:
        for point in points:
            existing = existing_by_key.get((point.population_id, point.slot))
            if existing is not None and int(existing["locked"] or 0) != 0:
                connection.execute(
                    "UPDATE map_mob_spawns SET enabled=1, updated_at=CURRENT_TIMESTAMP WHERE spawn_id=?",
                    (int(existing["spawn_id"]),),
                )
                continue

            population = next(
                row for row in active_populations
                if int(row["population_id"]) == point.population_id
            )
            connection.execute(
                """
                INSERT INTO map_mob_spawns (
                    map_id, template_id, region_id, population_id, population_slot,
                    position_x, position_y, direction,
                    leash_range_override, respawn_ms_override,
                    source_type, locked, enabled, note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'GENERATED', 0, 1, ?)
                ON CONFLICT(population_id, population_slot) DO UPDATE SET
                    map_id=excluded.map_id,
                    template_id=excluded.template_id,
                    region_id=excluded.region_id,
                    position_x=excluded.position_x,
                    position_y=excluded.position_y,
                    direction=excluded.direction,
                    leash_range_override=excluded.leash_range_override,
                    respawn_ms_override=excluded.respawn_ms_override,
                    source_type='GENERATED',
                    enabled=1,
                    note=excluded.note,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    map_id,
                    point.template_id,
                    int(region_id),
                    point.population_id,
                    point.slot,
                    point.x,
                    point.y,
                    point.direction,
                    population["leash_range_override"],
                    population["respawn_ms_override"],
                    f"generated from {region_key}/{point.population_key} slot {point.slot}",
                ),
            )

        # Rows outside the active population counts remain available for future
        # reuse but are disabled instead of deleted, preserving stable IDs.
        for row in existing_rows:
            key = (int(row["population_id"]), int(row["population_slot"]))
            if key not in active_keys:
                connection.execute(
                    "UPDATE map_mob_spawns SET enabled=0, updated_at=CURRENT_TIMESTAMP WHERE spawn_id=?",
                    (int(row["spawn_id"]),),
                )

    return RegionGenerationResult(
        region_id=int(region_id),
        region_key=region_key,
        map_id=map_id,
        requested_count=requested_count,
        points=tuple(points),
        disabled_rows=disabled_rows,
        candidate_cells=len(candidates),
        preview=bool(preview),
    )


def selected_region_ids(
    connection: sqlite3.Connection,
    *,
    region_id: int | None = None,
    map_id: int | None = None,
) -> list[int]:
    clauses: list[str] = []
    parameters: list[object] = []
    if region_id is not None:
        clauses.append("region_id = ?")
        parameters.append(int(region_id))
    if map_id is not None:
        clauses.append("map_id = ?")
        parameters.append(int(map_id))
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT region_id FROM mob_spawn_regions{where_sql} ORDER BY map_id, region_id",
        tuple(parameters),
    ).fetchall()
    return [int(row[0]) for row in rows]
