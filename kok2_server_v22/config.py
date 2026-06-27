from pathlib import Path


# ============================================================
# Project paths
# ============================================================

PROJECT_DIR = Path(__file__).resolve().parent
GAME_ROOT_DIR = PROJECT_DIR.parent
GKK2_PATH = GAME_ROOT_DIR / "GKK2.exe"


# ============================================================
# Network
# ============================================================

HOST = "0.0.0.0"

LOGIN_PORT = 4701
GAME_PORT = 10100


# ============================================================
# Login-server display and game redirect
# ============================================================

SERVER_NAME = "Local Server"

ENTER_GAME_HOST = "127.0.0.1"
ENTER_GAME_PORT = GAME_PORT


# ============================================================
# New-character defaults
# ============================================================
# These are used only when the client creates a new role.  Existing roles load
# map/position from the database.

NEW_CHARACTER_MAP_ID = 10100
NEW_CHARACTER_SPAWN_X = 128
NEW_CHARACTER_SPAWN_Y = 65


# ============================================================
# Confirmed stable timing
# ============================================================

ROLE_LIST_DELAY_SECONDS = 1.5
ENTER_GAME_REDIRECT_DELAY_SECONDS = 0.20
MAP_TO_PLAYER_DELAY_SECONDS = 1.0


# ============================================================
# Per-connection world visibility
# ============================================================
#
# The original client creates name plates for every NPC/MOB Being that the
# server has created with opcode 0x0005.  It does not apply a useful map-wide
# distance limit of its own, so sending every entity on the map makes all names
# appear at once.  The server therefore keeps only nearby entities in the
# client's Being table:
#
#   distance <= ENTER: send 0x0005 (enter view)
#   distance >  LEAVE: send 0x0007 (leave view)
#
# A wider leave radius prevents repeated create/delete flicker while the player
# moves along the boundary.  Values are map-grid tiles and can be tuned here
# without touching protocol code.

ENTITY_VISIBILITY_ENTER_RADIUS_TILES = 40.0
ENTITY_VISIBILITY_LEAVE_RADIUS_TILES = 45.0



# ============================================================
# Confirmed role-list transport
# ============================================================
# Opcode 0x7002 is sent as a type-2 multi-record frame.
# This is the confirmed format for showing multiple account roles.

# ============================================================
# Database
# ============================================================

DATABASE_PATH = PROJECT_DIR / "kok2.db"

DEFAULT_ACCOUNT_USERNAME = "rrrr"
DEFAULT_ACCOUNT_PASSWORD = "rr"
DEFAULT_CHARACTER_NAME = ""


# ============================================================
# Map table
# ============================================================

MAP_TABLE_XML = PROJECT_DIR / "map_table.xml"

# ============================================================
# Monster reward level-difference scaling
# ============================================================
#
# The monster template remains the sole source of the base experience and
# base gold range.  Gold is rolled from mob_templates.gold_min/gold_max first,
# then both rolled gold and fixed experience are multiplied by the same rate.
# Positive fractional results are rounded upward.

MOB_REWARD_RATE_PLAYER_NOT_HIGHER_PERCENT = 100
MOB_REWARD_RATE_PLAYER_PLUS_1_PERCENT = 75
MOB_REWARD_RATE_PLAYER_PLUS_2_PERCENT = 50
MOB_REWARD_RATE_PLAYER_PLUS_3_PERCENT = 25
MOB_REWARD_RATE_PLAYER_PLUS_4_OR_MORE_PERCENT = 0

# ============================================================
# Server side-message definitions
# ============================================================
#
# All display-only server messages use one common configuration structure:
#
#   enabled  - whether this message is sent
#   template - Python ``str.format`` template
#   style    - one confirmed native client style
#
# Confirmed native styles:
#
#   system_light
#       opcode 0x0001, plain C string, fixed client color #FDF597.
#       This is the cleanest native light/system line and does not add a
#       sender or channel prefix.  It is slightly warm rather than pure white.
#
#   green
#       opcode 0x0012, message type 2, empty sender, fixed client color
#       #42FF00.
#
# The original client does not expose a plain, prefix-free, exact #FFFFFF
# server-message route.  Do not place HTML/font tags in templates: the client
# displays such tags literally rather than interpreting them.
#
# Available placeholders:
#   login_welcome: none
#   mob_death: {mob_name}
#   mob_reward_gold: {gold}
#   mob_reward_experience: {experience}
# Styles: white (pure white), system_light (pale gold), green.

ENABLE_SERVER_SIDE_MESSAGES = True

SERVER_SIDE_MESSAGES = {
    "login_welcome": {
        "enabled": True,
        "template": "欢迎光临万王之王2",
        "style": "white",
    },
    "mob_death": {
        "enabled": True,
        "template": "{mob_name}无法抗拒死神的召唤，失去生命的光彩",
        "style": "white",
    },
    "mob_reward_experience": {
        "enabled": True,
        "template": "你得到了{experience}点经验",
        "style": "green",
    },
    "mob_reward_gold": {
        "enabled": True,
        "template": "你得到了{gold}枚金币",
        "style": "system_light",
    },
}

