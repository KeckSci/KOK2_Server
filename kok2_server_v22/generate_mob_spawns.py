from __future__ import annotations

import argparse
from pathlib import Path

from database import Database
from mob_spawn_generator import SpawnGenerationError, generate_region, selected_region_ids


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "kok2.db"
DEFAULT_SCHEMA_PATH = PROJECT_DIR / "schema.sql"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate stable map_mob_spawns rows from database-owned collision "
            "grids, painted region masks, and mob_region_populations."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--region-id", type=int)
    parser.add_argument("--map-id", type=int)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="calculate points without changing map_mob_spawns",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database = Database(args.database.resolve())
    database.initialize_schema(DEFAULT_SCHEMA_PATH)

    with database.session() as connection:
        region_ids = selected_region_ids(
            connection,
            region_id=args.region_id,
            map_id=args.map_id,
        )
        if not region_ids:
            print("No mob_spawn_regions matched.")
            return 0

        for region_id in region_ids:
            try:
                result = generate_region(
                    connection,
                    region_id,
                    preview=bool(args.preview),
                )
            except SpawnGenerationError as error:
                print(f"[ERROR] region_id={region_id}: {error}")
                raise

            mode = "PREVIEW" if result.preview else "APPLIED"
            print(
                f"[{mode}] region={result.region_key!r} id={result.region_id} "
                f"map={result.map_id} count={len(result.points)}/"
                f"{result.requested_count} candidates={result.candidate_cells} "
                f"disabled_extra_rows={result.disabled_rows}"
            )
            by_population: dict[str, int] = {}
            for point in result.points:
                by_population[point.population_key] = (
                    by_population.get(point.population_key, 0) + 1
                )
            for key, count in sorted(by_population.items()):
                print(f"  {key}: {count}")

        if args.preview:
            connection.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
