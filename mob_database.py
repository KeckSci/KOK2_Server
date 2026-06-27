from __future__ import annotations

import hashlib
import zlib

MOB_SCHEMA_SQL = r'''
-- V17.40: database-authoritative monster templates, collision grids,
-- irregular spawn regions, region populations, and stable concrete spawns.
CREATE TABLE IF NOT EXISTS mob_templates (
    template_id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    monster_data_id TEXT NOT NULL DEFAULT '',
    client_model_id TEXT NOT NULL DEFAULT '',

    level INTEGER NOT NULL DEFAULT 1 CHECK(level >= 1),
    max_hp INTEGER NOT NULL DEFAULT 1 CHECK(max_hp >= 1),
    max_mp INTEGER NOT NULL DEFAULT 0 CHECK(max_mp >= 0),
    max_sp INTEGER NOT NULL DEFAULT 0 CHECK(max_sp >= 0),
    attack_min INTEGER NOT NULL DEFAULT 0 CHECK(attack_min >= 0),
    attack_max INTEGER NOT NULL DEFAULT 0 CHECK(attack_max >= attack_min),
    defense_power INTEGER NOT NULL DEFAULT 0 CHECK(defense_power >= 0),
    magic_attack_min INTEGER NOT NULL DEFAULT 0 CHECK(magic_attack_min >= 0),
    magic_attack_max INTEGER NOT NULL DEFAULT 0 CHECK(magic_attack_max >= magic_attack_min),
    magic_defense_power INTEGER NOT NULL DEFAULT 0 CHECK(magic_defense_power >= 0),
    earth_resistance INTEGER NOT NULL DEFAULT 0,
    water_resistance INTEGER NOT NULL DEFAULT 0,
    fire_resistance INTEGER NOT NULL DEFAULT 0,
    wind_resistance INTEGER NOT NULL DEFAULT 0,
    light_resistance INTEGER NOT NULL DEFAULT 0,
    dark_resistance INTEGER NOT NULL DEFAULT 0,

    experience_reward INTEGER NOT NULL DEFAULT 0 CHECK(experience_reward >= 0),
    gold_min INTEGER NOT NULL DEFAULT 0 CHECK(gold_min >= 0),
    gold_max INTEGER NOT NULL DEFAULT 0 CHECK(gold_max >= gold_min),
    special_text TEXT NOT NULL DEFAULT '',

    first_attack_delay_ms INTEGER NOT NULL DEFAULT 800 CHECK(first_attack_delay_ms >= 0),
    attack_interval_ms INTEGER NOT NULL DEFAULT 2500 CHECK(attack_interval_ms >= 1),
    attack_range REAL NOT NULL DEFAULT 2.0 CHECK(attack_range >= 0),
    move_speed INTEGER NOT NULL DEFAULT 180,
    default_leash_range REAL NOT NULL DEFAULT 20.0 CHECK(default_leash_range >= 0),

    -- Idle roaming is independent from combat chase.  The original spawn point
    -- remains the roaming center, while combat captures a temporary anchor at
    -- the monster's actual pre-hit position.
    idle_wander_enabled INTEGER NOT NULL DEFAULT 1 CHECK(idle_wander_enabled IN (0, 1)),
    idle_wander_radius REAL NOT NULL DEFAULT 3.0 CHECK(idle_wander_radius >= 0),
    idle_wander_min_pause_ms INTEGER NOT NULL DEFAULT 3000 CHECK(idle_wander_min_pause_ms >= 0),
    idle_wander_max_pause_ms INTEGER NOT NULL DEFAULT 7000 CHECK(idle_wander_max_pause_ms >= idle_wander_min_pause_ms),
    idle_wander_move_speed INTEGER NOT NULL DEFAULT 180 CHECK(idle_wander_move_speed >= 1),

    corpse_ms INTEGER NOT NULL DEFAULT 5000 CHECK(corpse_ms >= 0),
    default_respawn_ms INTEGER NOT NULL DEFAULT 30000 CHECK(default_respawn_ms >= 0),
    action_type INTEGER NOT NULL DEFAULT 1,
    attack_effect_field10 INTEGER NOT NULL DEFAULT 0,
    aggro_mode TEXT NOT NULL DEFAULT 'RETALIATE'
        CHECK(aggro_mode IN ('NONE', 'RETALIATE', 'PROXIMITY')),
    aggro_range REAL NOT NULL DEFAULT 0.0 CHECK(aggro_range >= 0),

    -- Opcode 0x0005 presentation defaults retained for later runtime stages.
    scale_x INTEGER NOT NULL DEFAULT 100,
    scale_y INTEGER NOT NULL DEFAULT 100,
    scale_z INTEGER NOT NULL DEFAULT 100,
    ghost_mode INTEGER NOT NULL DEFAULT 0,
    being_field0c INTEGER NOT NULL DEFAULT 0,
    being_field10 INTEGER NOT NULL DEFAULT 0,
    being_field18 INTEGER NOT NULL DEFAULT 0,
    being_field1c INTEGER NOT NULL DEFAULT 0,
    being_field20 INTEGER NOT NULL DEFAULT 100,
    being_field2c INTEGER NOT NULL DEFAULT 1,
    array1_u32_array TEXT NOT NULL DEFAULT '',
    array2_u32_array TEXT NOT NULL DEFAULT '',

    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mob_templates_enabled
ON mob_templates(enabled, template_id);

-- One authoritative client collision grid per map. Runtime and generators use
-- the original game-coordinate orientation: index = y * width + x.
CREATE TABLE IF NOT EXISTS map_collision_grids (
    map_id INTEGER PRIMARY KEY,
    width INTEGER NOT NULL CHECK(width >= 1),
    height INTEGER NOT NULL CHECK(height >= 1),
    encoding TEXT NOT NULL DEFAULT 'ZLIB_U8'
        CHECK(encoding IN ('RAW_U8', 'ZLIB_U8')),
    walkable_value INTEGER NOT NULL DEFAULT 1 CHECK(walkable_value BETWEEN 0 AND 255),
    grid_data BLOB NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(map_id) REFERENCES maps(id)
);

-- An irregular, image-derived population region. The mask is stored in game
-- coordinates and can contain disconnected islands (the green 10100 region
-- intentionally has two separate areas).
CREATE TABLE IF NOT EXISTS mob_spawn_regions (
    region_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region_key TEXT NOT NULL UNIQUE,
    region_name TEXT NOT NULL DEFAULT '',
    map_id INTEGER NOT NULL,
    mask_width INTEGER NOT NULL CHECK(mask_width >= 1),
    mask_height INTEGER NOT NULL CHECK(mask_height >= 1),
    mask_encoding TEXT NOT NULL DEFAULT 'ZLIB_U8'
        CHECK(mask_encoding IN ('RAW_U8', 'ZLIB_U8')),
    mask_data BLOB NOT NULL,
    source_color TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    random_seed INTEGER NOT NULL DEFAULT 1,
    min_distance REAL NOT NULL DEFAULT 3.0 CHECK(min_distance >= 0),
    avoid_npc_distance REAL NOT NULL DEFAULT 3.0 CHECK(avoid_npc_distance >= 0),
    avoid_teleport_distance REAL NOT NULL DEFAULT 4.0 CHECK(avoid_teleport_distance >= 0),
    direction_mode TEXT NOT NULL DEFAULT 'RANDOM'
        CHECK(direction_mode IN ('RANDOM', 'FIXED')),
    fixed_direction INTEGER NOT NULL DEFAULT 0 CHECK(fixed_direction BETWEEN 0 AND 65535),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(map_id) REFERENCES maps(id)
);

CREATE INDEX IF NOT EXISTS idx_mob_spawn_regions_map
ON mob_spawn_regions(map_id, enabled, region_id);

-- Many monster types may share one colored region. This is the table normally
-- edited when changing how many of each monster should appear on a map.
CREATE TABLE IF NOT EXISTS mob_region_populations (
    population_id INTEGER PRIMARY KEY AUTOINCREMENT,
    population_key TEXT NOT NULL UNIQUE,
    region_id INTEGER NOT NULL,
    template_id INTEGER NOT NULL,
    spawn_count INTEGER NOT NULL DEFAULT 0 CHECK(spawn_count >= 0),
    sort_order INTEGER NOT NULL DEFAULT 0,
    random_seed_offset INTEGER NOT NULL DEFAULT 0,
    leash_range_override REAL,
    respawn_ms_override INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(region_id, template_id),
    FOREIGN KEY(region_id) REFERENCES mob_spawn_regions(region_id) ON DELETE CASCADE,
    FOREIGN KEY(template_id) REFERENCES mob_templates(template_id)
);

CREATE INDEX IF NOT EXISTS idx_mob_region_populations_region
ON mob_region_populations(region_id, enabled, sort_order, population_id);

-- Concrete stable spawn points consumed by the future runtime monster manager.
-- Regeneration keeps spawn_id/network_id through UNIQUE(population_id, slot).
CREATE TABLE IF NOT EXISTS map_mob_spawns (
    spawn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    map_id INTEGER NOT NULL,
    template_id INTEGER NOT NULL,
    region_id INTEGER,
    population_id INTEGER,
    population_slot INTEGER CHECK(population_slot IS NULL OR population_slot >= 1),
    position_x INTEGER NOT NULL CHECK(position_x BETWEEN 0 AND 65535),
    position_y INTEGER NOT NULL CHECK(position_y BETWEEN 0 AND 65535),
    direction INTEGER NOT NULL DEFAULT 0 CHECK(direction BETWEEN 0 AND 65535),
    network_id INTEGER UNIQUE CHECK(network_id IS NULL OR network_id > 0),
    leash_range_override REAL,
    respawn_ms_override INTEGER,
    source_type TEXT NOT NULL DEFAULT 'MANUAL'
        CHECK(source_type IN ('MANUAL', 'GENERATED')),
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(population_id, population_slot),
    FOREIGN KEY(map_id) REFERENCES maps(id),
    FOREIGN KEY(template_id) REFERENCES mob_templates(template_id),
    FOREIGN KEY(region_id) REFERENCES mob_spawn_regions(region_id) ON DELETE SET NULL,
    FOREIGN KEY(population_id) REFERENCES mob_region_populations(population_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_map_mob_spawns_map
ON map_mob_spawns(map_id, enabled, spawn_id);
CREATE INDEX IF NOT EXISTS idx_map_mob_spawns_template
ON map_mob_spawns(template_id, enabled, spawn_id);
CREATE INDEX IF NOT EXISTS idx_map_mob_spawns_population
ON map_mob_spawns(population_id, population_slot);

CREATE TRIGGER IF NOT EXISTS trg_map_mob_spawns_assign_network_id
AFTER INSERT ON map_mob_spawns
FOR EACH ROW
WHEN NEW.network_id IS NULL OR NEW.network_id = 0
BEGIN
    UPDATE map_mob_spawns
    SET network_id = 2000000 + NEW.spawn_id
    WHERE spawn_id = NEW.spawn_id;
END;

CREATE VIEW IF NOT EXISTS v_map_mob_distribution AS
SELECT
    r.map_id,
    r.region_id,
    r.region_key,
    r.region_name,
    p.population_id,
    p.population_key,
    p.spawn_count,
    p.sort_order,
    t.template_id,
    t.template_key,
    t.display_name,
    t.monster_data_id,
    t.client_model_id,
    t.level,
    t.max_hp,
    t.attack_min,
    t.attack_max,
    t.defense_power,
    t.experience_reward,
    r.enabled AS region_enabled,
    p.enabled AS population_enabled,
    t.enabled AS template_enabled
FROM mob_region_populations AS p
JOIN mob_spawn_regions AS r ON r.region_id = p.region_id
JOIN mob_templates AS t ON t.template_id = p.template_id;

CREATE VIEW IF NOT EXISTS v_map_mob_spawns AS
SELECT
    s.*,
    r.region_key,
    r.region_name,
    p.population_key,
    t.template_key,
    t.display_name,
    t.monster_data_id,
    t.client_model_id,
    t.level,
    t.max_hp,
    t.max_mp,
    t.max_sp,
    t.attack_min,
    t.attack_max,
    t.defense_power,
    t.experience_reward
FROM map_mob_spawns AS s
JOIN mob_templates AS t ON t.template_id = s.template_id
LEFT JOIN mob_spawn_regions AS r ON r.region_id = s.region_id
LEFT JOIN mob_region_populations AS p ON p.population_id = s.population_id;
'''


def encode_u8_grid(raw: bytes, *, compress: bool = True) -> tuple[str, bytes, str]:
    payload = bytes(raw)
    encoding = "ZLIB_U8" if compress else "RAW_U8"
    stored = zlib.compress(payload, level=9) if compress else payload
    return encoding, stored, hashlib.sha256(payload).hexdigest()


def decode_u8_grid(data: bytes, encoding: str, *, expected_size: int) -> bytes:
    normalized = str(encoding).upper().strip()
    if normalized == "RAW_U8":
        raw = bytes(data)
    elif normalized == "ZLIB_U8":
        raw = zlib.decompress(bytes(data))
    else:
        raise ValueError(f"unsupported grid encoding: {encoding!r}")
    if len(raw) != int(expected_size):
        raise ValueError(
            f"grid size mismatch: expected={expected_size}, actual={len(raw)}"
        )
    return raw


def validate_binary_mask(raw: bytes) -> None:
    unexpected = sorted(set(raw) - {0, 1})
    if unexpected:
        raise ValueError(f"mask contains values other than 0/1: {unexpected[:16]}")


def count_enabled(raw: bytes) -> int:
    return sum(1 for value in raw if value != 0)
