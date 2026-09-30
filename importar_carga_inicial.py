#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Importador de carga inicial para pokemon_tracker.py.

Uso recomendado en Termux:

    python pokemon_tracker.py --sync

    python importar_carga_inicial.py \
        --actuales pokemon_actuales.csv \
        --historico pokedex_historico.csv \
        --dry-run

    python importar_carga_inicial.py \
        --actuales pokemon_actuales.csv \
        --historico pokedex_historico.csv

Por defecto usa:
    ~/.pokemon_gen12/pokemon.sqlite3

Solo importa filas con importar=SI.
La operación real es transaccional: si la validación detecta errores, no escribe nada.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

GAMES = ("red", "blue", "yellow", "gold", "silver", "crystal")
GEN1 = {"red", "blue", "yellow"}
GEN_MAX = {
    "red": 151, "blue": 151, "yellow": 151,
    "gold": 251, "silver": 251, "crystal": 251,
}
VALID_STATUS = {"party", "box", "retired"}
YES_VALUES = {"si", "sí", "s", "yes", "y", "1", "true", "verdadero"}
DEFAULT_DB = Path.home() / ".pokemon_gen12" / "pokemon.sqlite3"

ACTUALES_FIELDS = [
    "importar", "registro_id", "juego", "dex_no", "especie", "nivel", "estado",
    "ataque_1", "ataque_2", "ataque_3", "ataque_4",
    "preevoluciones_dex", "apodo", "notas",
]
HIST_FIELDS = ["importar", "juego", "dex_no", "especie", "motivo", "notas"]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.strip().lower())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return "".join(c for c in value if c.isalnum())


def wants_import(value: str) -> bool:
    return clean(value).lower() in YES_VALUES


def open_csv(path: Path):
    # utf-8-sig admite CSV con BOM generado por Excel.
    return path.open("r", encoding="utf-8-sig", newline="")


def read_rows(path: Path, expected_fields: list[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"No existe: {path}")
    with open_csv(path) as f:
        reader = csv.DictReader(f)
        fields = [clean(x) for x in (reader.fieldnames or [])]
        missing = [x for x in expected_fields if x not in fields]
        if missing:
            raise ValueError(
                f"{path.name}: faltan columnas: {', '.join(missing)}"
            )
        return [
            {k: clean(v) for k, v in row.items()}
            for row in reader
            if wants_import(row.get("importar", ""))
        ]


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"No existe la base SQLite: {db_path}\n"
            "Ejecuta primero pokemon_tracker.py --sync."
        )
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def require_schema(con: sqlite3.Connection) -> None:
    needed = {"species", "evolution_edges", "individuals", "history", "dex_entries"}
    existing = {
        r["name"]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(needed - existing)
    if missing:
        raise RuntimeError(
            "La base no parece pertenecer al tracker. Faltan tablas: "
            + ", ".join(missing)
        )

    count = con.execute(
        "SELECT COUNT(*) AS n FROM species WHERE id BETWEEN 1 AND 251"
    ).fetchone()["n"]
    if count < 251:
        raise RuntimeError(
            "El catálogo de especies no está sincronizado. "
            "Ejecuta: python pokemon_tracker.py --sync"
        )


def species_row(con: sqlite3.Connection, dex_no: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, name_en, name_es, chain_id FROM species WHERE id=?",
        (dex_no,),
    ).fetchone()


def validate_species_name(
    sp: sqlite3.Row, typed_name: str, context: str, errors: list[str]
) -> None:
    if not typed_name:
        return
    candidates = {normalize_name(sp["name_en"])}
    if sp["name_es"]:
        candidates.add(normalize_name(sp["name_es"]))
    if normalize_name(typed_name) not in candidates:
        expected = sp["name_es"] or sp["name_en"]
        errors.append(
            f"{context}: especie='{typed_name}' no coincide con dex_no "
            f"#{sp['id']:03d} ({expected})."
        )


def parse_int(value: str, context: str, errors: list[str]) -> int | None:
    try:
        return int(clean(value))
    except (TypeError, ValueError):
        errors.append(f"{context}: se esperaba un número y se recibió '{value}'.")
        return None


def parse_preevos(value: str, context: str, errors: list[str]) -> list[int]:
    if not clean(value):
        return []
    result = []
    for part in clean(value).replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            sid = int(part)
        except ValueError:
            errors.append(
                f"{context}: preevoluciones_dex contiene '{part}', que no es un número."
            )
            continue
        result.append(sid)
    return list(dict.fromkeys(result))


def ancestor_ids(con: sqlite3.Connection, species_id: int, max_species: int) -> set[int]:
    found: set[int] = set()
    frontier = [species_id]
    while frontier:
        child = frontier.pop()
        rows = con.execute(
            """
            SELECT DISTINCT parent_id
            FROM evolution_edges
            WHERE child_id=? AND parent_id <= ?
            """,
            (child, max_species),
        ).fetchall()
        for r in rows:
            parent = int(r["parent_id"])
            if parent not in found:
                found.add(parent)
                frontier.append(parent)
    return found


def add_dex_entry(
    con: sqlite3.Connection,
    game: str,
    species_id: int,
    individual_id: int | None,
    reason: str,
) -> None:
    con.execute(
        """
        INSERT OR IGNORE INTO dex_entries
            (game, species_id, first_obtained_at, individual_id, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (game, species_id, now_iso(), individual_id, reason),
    )


def validate_actuales(
    con: sqlite3.Connection,
    rows: list[dict[str, str]],
    permit_nonempty: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    prepared: list[dict[str, Any]] = []

    existing_count = con.execute(
        "SELECT COUNT(*) AS n FROM individuals"
    ).fetchone()["n"]
    if existing_count and not permit_nonempty:
        errors.append(
            f"La tabla individuals ya contiene {existing_count} ejemplares. "
            "Por seguridad, el importador de carga inicial se detiene. "
            "Usa --permitir-db-no-vacia solo si sabes que quieres añadir registros."
        )

    ids = [clean(r.get("registro_id")) for r in rows]
    blank_ids = [i + 2 for i, x in enumerate(ids) if not x]
    if blank_ids:
        errors.append(
            "pokemon_actuales.csv: registro_id vacío en fila(s) CSV "
            + ", ".join(map(str, blank_ids))
        )
    duplicated = sorted(k for k, n in Counter(ids).items() if k and n > 1)
    if duplicated:
        errors.append(
            "pokemon_actuales.csv: registro_id duplicado: "
            + ", ".join(duplicated)
        )

    imported_party = Counter()
    existing_party = {
        g: con.execute(
            "SELECT COUNT(*) AS n FROM individuals WHERE game=? AND status='party'",
            (g,),
        ).fetchone()["n"]
        for g in GAMES
    }

    for idx, row in enumerate(rows, start=2):
        ctx = f"pokemon_actuales.csv fila {idx} ({row.get('registro_id') or '?'})"

        game = clean(row.get("juego")).lower()
        if game not in GAMES:
            errors.append(f"{ctx}: juego inválido '{row.get('juego')}'.")
            continue

        dex_no = parse_int(row.get("dex_no", ""), ctx + " dex_no", errors)
        level = parse_int(row.get("nivel", ""), ctx + " nivel", errors)
        status = clean(row.get("estado")).lower()

        if dex_no is None or level is None:
            continue
        if not (1 <= dex_no <= GEN_MAX[game]):
            errors.append(
                f"{ctx}: #{dex_no:03d} no existe en la generación de {game}."
            )
            continue
        if not (1 <= level <= 100):
            errors.append(f"{ctx}: nivel fuera de rango 1-100: {level}.")
        if status not in VALID_STATUS:
            errors.append(
                f"{ctx}: estado inválido '{status}'. "
                "Usa party, box o retired."
            )

        sp = species_row(con, dex_no)
        if not sp:
            errors.append(f"{ctx}: no existe #{dex_no:03d} en species.")
            continue
        validate_species_name(sp, row.get("especie", ""), ctx, errors)

        moves = [
            clean(row.get(f"ataque_{i}", ""))
            for i in range(1, 5)
        ]
        moves = [m for m in moves if m]

        preevos = parse_preevos(
            row.get("preevoluciones_dex", ""), ctx, errors
        )
        ancestors = ancestor_ids(con, dex_no, GEN_MAX[game])
        for pre in preevos:
            if not (1 <= pre <= GEN_MAX[game]):
                errors.append(
                    f"{ctx}: preevolución #{pre:03d} no existe en {game}."
                )
            elif pre not in ancestors:
                errors.append(
                    f"{ctx}: #{pre:03d} no figura como ancestro evolutivo "
                    f"de #{dex_no:03d} en la referencia local."
                )

        if status == "party":
            imported_party[game] += 1

        prepared.append(
            {
                "registro_id": clean(row.get("registro_id")),
                "game": game,
                "species_id": dex_no,
                "level": level,
                "status": status,
                "moves": moves,
                "preevos": preevos,
                "nickname": clean(row.get("apodo")) or None,
                "notes": clean(row.get("notas")) or None,
            }
        )

    for game in GAMES:
        total = existing_party[game] + imported_party[game]
        if total > 6:
            errors.append(
                f"{game}: el party quedaría con {total} Pokémon "
                f"({existing_party[game]} existentes + {imported_party[game]} importados)."
            )

    return prepared, errors


def validate_historico(
    con: sqlite3.Connection,
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    prepared: list[dict[str, Any]] = []

    for idx, row in enumerate(rows, start=2):
        ctx = f"pokedex_historico.csv fila {idx}"
        game = clean(row.get("juego")).lower()
        if game not in GAMES:
            errors.append(f"{ctx}: juego inválido '{row.get('juego')}'.")
            continue

        dex_no = parse_int(row.get("dex_no", ""), ctx + " dex_no", errors)
        if dex_no is None:
            continue
        if not (1 <= dex_no <= GEN_MAX[game]):
            errors.append(
                f"{ctx}: #{dex_no:03d} no existe en la generación de {game}."
            )
            continue

        sp = species_row(con, dex_no)
        if not sp:
            errors.append(f"{ctx}: no existe #{dex_no:03d} en species.")
            continue
        validate_species_name(sp, row.get("especie", ""), ctx, errors)

        reason = clean(row.get("motivo")) or "carga inicial histórica"
        notes = clean(row.get("notas"))
        if notes:
            reason = f"{reason}: {notes}"

        prepared.append(
            {
                "game": game,
                "species_id": dex_no,
                "reason": reason,
            }
        )

    return prepared, errors


def write_mapping(path: Path, mapping: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "registro_id", "sqlite_id", "juego",
                "dex_no", "nivel", "estado"
            ],
        )
        writer.writeheader()
        writer.writerows(mapping)


def perform_import(
    con: sqlite3.Connection,
    actuales: list[dict[str, Any]],
    historico: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mapping: list[dict[str, Any]] = []
    ts = now_iso()

    with con:
        for row in actuales:
            cur = con.execute(
                """
                INSERT INTO individuals
                    (nickname, species_id, level, game, moves_json,
                     status, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["nickname"],
                    row["species_id"],
                    row["level"],
                    row["game"],
                    json.dumps(row["moves"], ensure_ascii=False),
                    row["status"],
                    row["notes"],
                    ts,
                    ts,
                ),
            )
            iid = int(cur.lastrowid)

            add_dex_entry(
                con,
                row["game"],
                row["species_id"],
                iid,
                "carga inicial: ejemplar actual",
            )
            for pre in row["preevos"]:
                add_dex_entry(
                    con,
                    row["game"],
                    pre,
                    iid,
                    "carga inicial: preevolución declarada",
                )

            con.execute(
                """
                INSERT INTO history
                    (individual_id, event_type, game_from, game_to,
                     species_from, species_to, details, created_at)
                VALUES (?, 'initial-import', ?, ?, ?, ?, ?, ?)
                """,
                (
                    iid,
                    row["game"],
                    row["game"],
                    row["species_id"],
                    row["species_id"],
                    f"registro_id={row['registro_id']}; "
                    f"preevoluciones={row['preevos']}",
                    ts,
                ),
            )

            mapping.append(
                {
                    "registro_id": row["registro_id"],
                    "sqlite_id": iid,
                    "juego": row["game"],
                    "dex_no": row["species_id"],
                    "nivel": row["level"],
                    "estado": row["status"],
                }
            )

        for row in historico:
            add_dex_entry(
                con,
                row["game"],
                row["species_id"],
                None,
                row["reason"],
            )

    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importa la situación inicial de RBY/GSC al SQLite del tracker."
    )
    parser.add_argument(
        "--actuales",
        type=Path,
        required=True,
        help="CSV de ejemplares actuales.",
    )
    parser.add_argument(
        "--historico",
        type=Path,
        help="CSV opcional de entradas históricas del Pokédex.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite del tracker (por defecto: {DEFAULT_DB}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida todo sin escribir en SQLite.",
    )
    parser.add_argument(
        "--permitir-db-no-vacia",
        action="store_true",
        help="Permite añadir individuos aunque la DB ya contenga ejemplares.",
    )
    parser.add_argument(
        "--resultado",
        type=Path,
        help="CSV de correspondencia registro_id -> sqlite_id.",
    )
    args = parser.parse_args()

    try:
        actuales_rows = read_rows(args.actuales, ACTUALES_FIELDS)
        historico_rows = (
            read_rows(args.historico, HIST_FIELDS)
            if args.historico
            else []
        )
        con = connect(args.db.expanduser())
        try:
            require_schema(con)
            actuales, errors_a = validate_actuales(
                con, actuales_rows, args.permitir_db_no_vacia
            )
            historico, errors_h = validate_historico(con, historico_rows)
            errors = errors_a + errors_h

            print("VALIDACIÓN DE CARGA INICIAL")
            print("---------------------------")
            print(f"Ejemplares marcados SI : {len(actuales)}")
            print(f"Entradas históricas SI : {len(historico)}")

            party = Counter(r["game"] for r in actuales if r["status"] == "party")
            for game in GAMES:
                if party[game]:
                    print(f"  party {game:<7}: {party[game]}")

            if errors:
                print(f"\nERRORES ({len(errors)}). No se ha escrito nada:\n")
                for err in errors:
                    print(" -", err)
                return 2

            print("\nValidación correcta.")
            if args.dry_run:
                print("Modo --dry-run: SQLite NO ha sido modificado.")
                return 0

            mapping = perform_import(con, actuales, historico)
        finally:
            con.close()

        result_path = (
            args.resultado
            if args.resultado
            else args.actuales.with_name("resultado_importacion.csv")
        )
        write_mapping(result_path, mapping)

        print("\nIMPORTACIÓN COMPLETADA")
        print(f"SQLite actualizado: {args.db.expanduser()}")
        print(f"Mapa de IDs: {result_path}")
        print(
            "Puedes abrir ahora pokemon_tracker.py y revisar "
            "'Ver todos mis Pokémon' y 'Ver progreso del Pokédex'."
        )
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
