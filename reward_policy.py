from __future__ import annotations

import config


def monster_reward_rate_percent(player_level: int, mob_level: int) -> int:
    """Return the configured experience/gold rate for one monster kill."""
    player = max(1, int(player_level))
    monster = max(1, int(mob_level))
    level_advantage = player - monster
    if level_advantage <= 0:
        rate = config.MOB_REWARD_RATE_PLAYER_NOT_HIGHER_PERCENT
    elif level_advantage == 1:
        rate = config.MOB_REWARD_RATE_PLAYER_PLUS_1_PERCENT
    elif level_advantage == 2:
        rate = config.MOB_REWARD_RATE_PLAYER_PLUS_2_PERCENT
    elif level_advantage == 3:
        rate = config.MOB_REWARD_RATE_PLAYER_PLUS_3_PERCENT
    else:
        rate = config.MOB_REWARD_RATE_PLAYER_PLUS_4_OR_MORE_PERCENT
    return max(0, int(rate))


def scale_monster_reward_up(base_value: int, rate_percent: int) -> int:
    """Scale one non-negative reward and round positive fractions upward."""
    base = max(0, int(base_value))
    rate = max(0, int(rate_percent))
    if base == 0 or rate == 0:
        return 0
    return (base * rate + 99) // 100


def calculate_monster_reward_awards(
    *,
    base_experience: int,
    base_gold: int,
    player_level: int,
    mob_level: int,
) -> tuple[int, int, int]:
    """Return ``(experience, gold, rate_percent)`` for one kill."""
    rate = monster_reward_rate_percent(player_level, mob_level)
    return (
        scale_monster_reward_up(base_experience, rate),
        scale_monster_reward_up(base_gold, rate),
        rate,
    )
