#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Pokemon Gen I-II Tracker
========================

Gestor local para Pokémon Red/Blue/Yellow y Gold/Silver/Crystal.

- Sin dependencias externas: solo biblioteca estándar de Python.
- Persistencia SQLite en ~/.pokemon_gen12/pokemon.sqlite3
- Registra ejemplares individuales: juego, especie, nivel, ataques, estado y notas.
- Conserva el historial del Pokédex por juego aunque el Pokémon evolucione o se intercambie.
- Gestiona un party de hasta 6 Pokémon por título.
- Detecta cuándo un Pokémon alcanza la última fase disponible de su línea en esa generación.
- Consulta localizaciones salvajes de Gen I/II descargadas una sola vez desde PokéAPI.
- Distingue especies ausentes de un título, evoluciones por intercambio, regalos y estáticos.
- El objetivo por defecto sigue la petición del usuario:
    R/B/Y: #001-#150 (150 especies; Mew excluido)
    G/S/C: #001-#250 (250 especies; Celebi excluido)

Primera ejecución recomendada:
    python pokemon_tracker.py --sync

Luego:
    python pokemon_tracker.py

Los datos de usuario y la base local quedan en:
    ~/.pokemon_gen12/pokemon.sqlite3
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

APP_DIR = Path.home() / ".pokemon_gen12"
DEFAULT_DB = APP_DIR / "pokemon.sqlite3"
POKEAPI = "https://pokeapi.co/api/v2"

GAMES = ("red", "blue", "yellow", "gold", "silver", "crystal")
GAME_LABEL = {
    "red": "Red",
    "blue": "Blue",
    "yellow": "Yellow",
    "gold": "Gold",
    "silver": "Silver",
    "crystal": "Crystal",
}
GEN1 = {"red", "blue", "yellow"}
GEN2 = {"gold", "silver", "crystal"}

# Objetivo personal pedido: 150 en RBY, 250 en GSC.
DEX_TARGET_MAX = {
    "red": 150,
    "blue": 150,
    "yellow": 150,
    "gold": 250,
    "silver": 250,
    "crystal": 250,
}

# Máximo de especie que existe físicamente en cada generación.
GEN_MAX_SPECIES = {
    "red": 151,
    "blue": 151,
    "yellow": 151,
    "gold": 251,
    "silver": 251,
    "crystal": 251,
}

# Especies que no pueden obtenerse de forma nativa en el título indicado
# y deben llegar desde otro juego o evento.
#
# R/B: exclusivas del título opuesto + Mew.
# Yellow: lista de especies ausentes + Mew.
# G/S/C: especies ausentes de toda Gen II + exclusivas del título opuesto.
#
# No incluimos aquí evoluciones por intercambio: el script las trata como
# "obtenibles mediante intercambio/evolución" porque el usuario sí intercambia.
ABSENT_ALL_GSC = {
    1, 2, 3, 4, 5, 6, 7, 8, 9,          # starters Kanto
    138, 139, 140, 141,                   # fósiles Omanyte/Kabuto
    144, 145, 146,                        # aves legendarias
    150, 151, 251,                        # Mewtwo, Mew, Celebi
}

NATIVE_MISSING = {
    "red": {
        27, 28, 37, 38, 52, 53, 69, 70, 71, 126, 127, 151
    },
    "blue": {
        23, 24, 43, 44, 45, 56, 57, 58, 59, 123, 125, 151
    },
    "yellow": {
        13, 14, 15, 23, 24, 26, 52, 53, 109, 110, 124, 125, 126, 151
    },
    "gold": ABSENT_ALL_GSC | {
        37, 38, 52, 53, 165, 166, 225, 227, 231, 232
    },
    "silver": ABSENT_ALL_GSC | {
        56, 57, 58, 59, 167, 168, 207, 216, 217, 226
    },
    "crystal": ABSENT_ALL_GSC | {
        37, 38, 56, 57, 179, 180, 181, 203, 223, 224
    },
}

EVENT_ONLY = {
    ("red", 151): "Mew: distribución/evento, glitch o intercambio desde una copia que ya lo posea.",
    ("blue", 151): "Mew: distribución/evento, glitch o intercambio desde una copia que ya lo posea.",
    ("yellow", 151): "Mew: distribución/evento, glitch o intercambio desde una copia que ya lo posea.",
    ("gold", 151): "Mew no se obtiene normalmente en G/S/C; debe llegar por intercambio/evento.",
    ("silver", 151): "Mew no se obtiene normalmente en G/S/C; debe llegar por intercambio/evento.",
    ("crystal", 151): "Mew no se obtiene normalmente en G/S/C; debe llegar por intercambio/evento.",
    ("gold", 251): "Celebi: evento en cartucho original.",
    ("silver", 251): "Celebi: evento en cartucho original.",
    ("crystal", 251): "Celebi: evento en cartucho original; en Virtual Console de Crystal existe el evento del GS Ball.",
}

# Casos especiales donde una evolución normal de la especie no puede realizarse
# dentro del juego indicado.
BLOCKED_EVOLUTION = {
    ("yellow", 25, 26): "El Pikachu compañero de Yellow rechaza la Thunder Stone; Raichu requiere intercambio.",
}

METHOD_ES = {
    "walk": "hierba/cueva",
    "surf": "Surf",
    "old-rod": "Caña Vieja",
    "good-rod": "Caña Buena",
    "super-rod": "Supercaña",
    "rock-smash": "Golpe Roca",
    "headbutt": "Golpe Cabeza",
    "dark-grass": "hierba oscura",
    "gift": "regalo",
    "static": "encuentro estático",
    "game-corner": "premio del casino",
    "in-game-trade": "intercambio con NPC",
    "fossil": "revivir fósil",
    "breeding": "crianza",
    "event": "evento",
}

# Adquisiciones no salvajes importantes. La tabla se inserta en SQLite y se
# puede ampliar manualmente sin tocar el resto del programa.
#
# (species_id, games, place, method, note)
SPECIAL_ROWS = [
    # ---------------- Gen I: starters y regalos ----------------
    (1, ("red", "blue"), "Pallet Town / laboratorio del Prof. Oak", "gift", "Starter: elegir entre Bulbasaur, Charmander y Squirtle."),
    (4, ("red", "blue"), "Pallet Town / laboratorio del Prof. Oak", "gift", "Starter: elegir entre Bulbasaur, Charmander y Squirtle."),
    (7, ("red", "blue"), "Pallet Town / laboratorio del Prof. Oak", "gift", "Starter: elegir entre Bulbasaur, Charmander y Squirtle."),
    (25, ("yellow",), "Pallet Town / laboratorio del Prof. Oak", "gift", "Pikachu inicial de Yellow."),
    (1, ("yellow",), "Cerulean City", "gift", "Regalo si la amistad de Pikachu es suficiente."),
    (4, ("yellow",), "Route 24", "gift", "Charmander entregado por un entrenador."),
    (7, ("yellow",), "Vermilion City", "gift", "Squirtle entregado tras conseguir la Thunder Badge."),

    (83, ("red", "blue"), "Vermilion City", "in-game-trade", "Intercambia un Spearow por Farfetch'd."),
    (122, ("red", "blue"), "Route 2", "in-game-trade", "Intercambia un Abra por Mr. Mime."),
    (124, ("red", "blue"), "Cerulean City", "in-game-trade", "Intercambia un Poliwhirl por Jynx."),
    (108, ("red", "blue"), "Route 18 Gate", "in-game-trade", "Lickitung se obtiene mediante intercambio con NPC."),
    (122, ("yellow",), "Route 2", "in-game-trade", "Intercambia un Clefairy por Mr. Mime."),

    (133, ("red", "blue", "yellow"), "Celadon Mansion, azotea", "gift", "Eevee único como regalo."),
    (106, ("red", "blue", "yellow"), "Saffron City / Fighting Dojo", "gift", "Elegir entre Hitmonlee o Hitmonchan."),
    (107, ("red", "blue", "yellow"), "Saffron City / Fighting Dojo", "gift", "Elegir entre Hitmonlee o Hitmonchan."),
    (131, ("red", "blue", "yellow"), "Silph Co. 7F", "gift", "Lapras de regalo."),
    (137, ("red", "blue", "yellow"), "Celadon Game Corner / Prize Corner", "game-corner", "Porygon como premio; el coste varía por versión."),

    (138, ("red", "blue", "yellow"), "Mt. Moon -> Cinnabar Lab", "fossil", "Elegir Helix Fossil y revivirlo en Cinnabar."),
    (140, ("red", "blue", "yellow"), "Mt. Moon -> Cinnabar Lab", "fossil", "Elegir Dome Fossil y revivirlo en Cinnabar."),
    (142, ("red", "blue", "yellow"), "Pewter Museum -> Cinnabar Lab", "fossil", "Old Amber; revivir en Cinnabar."),
    (143, ("red", "blue", "yellow"), "Routes 12 y 16", "static", "Snorlax bloqueando el camino; usar Poké Flute."),
    (144, ("red", "blue", "yellow"), "Seafoam Islands", "static", "Articuno."),
    (145, ("red", "blue", "yellow"), "Power Plant", "static", "Zapdos."),
    (146, ("red", "blue", "yellow"), "Victory Road", "static", "Moltres."),
    (150, ("red", "blue", "yellow"), "Cerulean Cave", "static", "Mewtwo; accesible tras ser Campeón."),
    (151, ("red", "blue", "yellow"), "Distribución externa", "event", "Mew no se obtiene mediante juego normal."),

    # ---------------- Gen II: regalos/estáticos ----------------
    (152, ("gold", "silver", "crystal"), "New Bark Town / laboratorio del Prof. Elm", "gift", "Starter: elegir entre Chikorita, Cyndaquil y Totodile."),
    (155, ("gold", "silver", "crystal"), "New Bark Town / laboratorio del Prof. Elm", "gift", "Starter: elegir entre Chikorita, Cyndaquil y Totodile."),
    (158, ("gold", "silver", "crystal"), "New Bark Town / laboratorio del Prof. Elm", "gift", "Starter: elegir entre Chikorita, Cyndaquil y Totodile."),
    (175, ("gold", "silver", "crystal"), "Violet City / Pokémon Center", "gift", "Huevo de Togepi recibido del ayudante del Prof. Elm."),
    (133, ("gold", "silver", "crystal"), "Goldenrod City / casa de Bill", "gift", "Eevee de regalo tras conocer a Bill."),
    (185, ("gold", "silver", "crystal"), "Route 36", "static", "Sudowoodo bloqueando el camino."),
    (213, ("gold", "silver", "crystal"), "Cianwood City", "gift", "Shuckle de regalo."),
    (236, ("gold", "silver", "crystal"), "Mt. Mortar", "gift", "Tyrogue de regalo tras derrotar al Karate King."),
    (131, ("gold", "silver", "crystal"), "Union Cave B2F, viernes", "static", "Lapras semanal."),
    (130, ("gold", "silver", "crystal"), "Lake of Rage", "static", "Gyarados rojo."),
    (142, ("gold", "silver", "crystal"), "Route 14", "in-game-trade", "Aerodactyl mediante intercambio con NPC."),
    (143, ("gold", "silver", "crystal"), "Vermilion City", "static", "Snorlax tras restaurar la Radio Card/Kanto radio."),
    (243, ("gold", "silver", "crystal"), "Johto", "static", "Raikou: roaming tras liberar a las bestias legendarias."),
    (244, ("gold", "silver", "crystal"), "Johto", "static", "Entei: roaming tras liberar a las bestias legendarias."),
    (245, ("gold", "silver"), "Johto", "static", "Suicune: roaming tras liberar a las bestias legendarias."),
    (245, ("crystal",), "Tin Tower", "static", "Suicune: encuentro fijo dentro de la historia."),
    (249, ("gold", "silver", "crystal"), "Whirl Islands", "static", "Lugia; el momento/nivel depende de la versión."),
    (250, ("gold", "silver", "crystal"), "Tin Tower", "static", "Ho-Oh; el momento/nivel depende de la versión."),
    (147, ("crystal",), "Dragon's Den / Dragon Shrine", "gift", "Dratini de regalo; puede conocer ExtremeSpeed si respondes correctamente."),

    # Bebés por crianza en Gen II.
    (172, ("gold", "silver", "crystal"), "Route 34 / Day Care", "breeding", "Criar Pikachu/Raichu para obtener Pichu."),
    (173, ("gold", "silver", "crystal"), "Route 34 / Day Care", "breeding", "Criar Clefairy/Clefable para obtener Cleffa."),
    (174, ("gold", "silver", "crystal"), "Route 34 / Day Care", "breeding", "Criar Jigglypuff/Wigglytuff para obtener Igglybuff."),
    (236, ("gold", "silver", "crystal"), "Route 34 / Day Care", "breeding", "También puede obtenerse por crianza de Hitmonlee/Hitmonchan/Hitmontop."),
    (238, ("gold", "silver", "crystal"), "Route 34 / Day Care", "breeding", "Criar Jynx para obtener Smoochum."),
    (239, ("gold", "silver", "crystal"), "Route 34 / Day Care", "breeding", "Criar Electabuzz para obtener Elekid."),
    (240, ("gold", "silver", "crystal"), "Route 34 / Day Care", "breeding", "Criar Magmar para obtener Magby."),

    (251, ("gold", "silver"), "Distribución externa", "event", "Celebi requiere evento en cartucho original."),
    (251, ("crystal",), "Ilex Forest", "event", "En cartucho original requiere evento del GS Ball; en Virtual Console puede activarse dentro del juego."),
]

# Evoluciones por intercambio relevantes en Gen I/II.
# Algunas tienen condiciones/objetos en Gen II; el detalle exacto también se
# descarga desde las cadenas de evolución de PokéAPI.
TRADE_EVOLUTIONS_FALLBACK = {
    64: 65,   # Kadabra -> Alakazam
    67: 68,   # Machoke -> Machamp
    75: 76,   # Graveler -> Golem
    93: 94,   # Haunter -> Gengar
    61: 186,  # Poliwhirl -> Politoed
    79: 199,  # Slowpoke -> Slowking
    95: 208,  # Onix -> Steelix
    117: 230, # Seadra -> Kingdra
    123: 212, # Scyther -> Scizor
    137: 233, # Porygon -> Porygon2
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def pretty_slug(value: str) -> str:
    s = value
    for prefix in ("kanto-", "johto-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    replacements = {
        "mt-": "Mt. ",
        "ss-anne": "S.S. Anne",
        "pokemon-tower": "Pokémon Tower",
        "pokemon-mansion": "Pokémon Mansion",
        "digletts-cave": "Diglett's Cave",
        "victory-road": "Victory Road",
        "seafoam-islands": "Seafoam Islands",
        "power-plant": "Power Plant",
        "rock-tunnel": "Rock Tunnel",
        "union-cave": "Union Cave",
        "whirl-islands": "Whirl Islands",
        "dragons-den": "Dragon's Den",
        "lake-of-rage": "Lake of Rage",
        "ilex-forest": "Ilex Forest",
    }
    if s in replacements:
        return replacements[s]
    s = s.replace("-", " ")
    return " ".join(w.capitalize() if not w.isdigit() else w for w in s.split())


def method_es(method: str) -> str:
    return METHOD_ES.get(method, pretty_slug(method))


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS species (
            id INTEGER PRIMARY KEY,
            name_en TEXT NOT NULL,
            name_es TEXT,
            chain_id INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_species_name_en ON species(name_en);
        CREATE INDEX IF NOT EXISTS idx_species_name_es ON species(name_es);
        CREATE INDEX IF NOT EXISTS idx_species_chain ON species(chain_id);

        CREATE TABLE IF NOT EXISTS evolution_edges (
            parent_id INTEGER NOT NULL,
            child_id INTEGER NOT NULL,
            trigger TEXT,
            min_level INTEGER,
            item TEXT,
            held_item TEXT,
            time_of_day TEXT,
            trade_species TEXT,
            min_happiness INTEGER,
            version_group TEXT,
            details TEXT,
            UNIQUE(
                parent_id, child_id, trigger, min_level, item,
                held_item, time_of_day, trade_species,
                min_happiness, version_group, details
            )
        );

        CREATE INDEX IF NOT EXISTS idx_evo_parent ON evolution_edges(parent_id);
        CREATE INDEX IF NOT EXISTS idx_evo_child ON evolution_edges(child_id);

        CREATE TABLE IF NOT EXISTS encounters (
            species_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            location TEXT NOT NULL,
            method TEXT NOT NULL,
            min_level INTEGER,
            max_level INTEGER,
            chance INTEGER,
            conditions TEXT NOT NULL DEFAULT '',
            UNIQUE(species_id, game, location, method, min_level, max_level, chance, conditions)
        );

        CREATE INDEX IF NOT EXISTS idx_enc_species_game ON encounters(species_id, game);

        CREATE TABLE IF NOT EXISTS special_acquisition (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            species_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            place TEXT NOT NULL,
            method TEXT NOT NULL,
            note TEXT,
            UNIQUE(species_id, game, place, method, note)
        );

        CREATE INDEX IF NOT EXISTS idx_special_species_game
            ON special_acquisition(species_id, game);

        CREATE TABLE IF NOT EXISTS individuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT,
            species_id INTEGER NOT NULL,
            level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 100),
            game TEXT NOT NULL,
            moves_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'box'
                CHECK(status IN ('party', 'box', 'retired')),
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ind_game_status ON individuals(game, status);
        CREATE INDEX IF NOT EXISTS idx_ind_species ON individuals(species_id);

        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            individual_id INTEGER,
            event_type TEXT NOT NULL,
            game_from TEXT,
            game_to TEXT,
            species_from INTEGER,
            species_to INTEGER,
            details TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_history_ind ON history(individual_id);

        CREATE TABLE IF NOT EXISTS dex_entries (
            game TEXT NOT NULL,
            species_id INTEGER NOT NULL,
            first_obtained_at TEXT NOT NULL,
            individual_id INTEGER,
            reason TEXT,
            PRIMARY KEY(game, species_id)
        );
        """
    )
    seed_special(con)
    con.commit()


def seed_special(con: sqlite3.Connection) -> None:
    rows = []
    for species_id, games, place, method, note in SPECIAL_ROWS:
        for game in games:
            rows.append((species_id, game, place, method, note))
    con.executemany(
        """
        INSERT OR IGNORE INTO special_acquisition
            (species_id, game, place, method, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )


def meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def api_json(path_or_url: str, retries: int = 3) -> Any:
    url = path_or_url if path_or_url.startswith("http") else f"{POKEAPI}{path_or_url}"
    last_exc: Exception | None = None
    headers = {"User-Agent": "PokemonGen12Tracker/1.0"}
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(attempt * 1.5)
    raise RuntimeError(f"No se pudo descargar {url}: {last_exc}")


def fetch_species_bundle(species_id: int) -> tuple[int, dict, list]:
    sp = api_json(f"/pokemon-species/{species_id}/")
    encounters = api_json(f"/pokemon/{species_id}/encounters")
    return species_id, sp, encounters


def spanish_name(species_payload: dict) -> str | None:
    for item in species_payload.get("names", []):
        if item.get("language", {}).get("name") == "es":
            return item.get("name")
    return None


def extract_id_from_url(url: str) -> int:
    return int(url.rstrip("/").split("/")[-1])


def flatten_chain(node: dict, out: list[tuple]) -> None:
    parent = extract_id_from_url(node["species"]["url"])
    for child_node in node.get("evolves_to", []):
        child = extract_id_from_url(child_node["species"]["url"])
        details_list = child_node.get("evolution_details") or [{}]
        for d in details_list:
            trigger = (d.get("trigger") or {}).get("name")
            item = (d.get("item") or {}).get("name")
            held_item = (d.get("held_item") or {}).get("name")
            trade_species = (d.get("trade_species") or {}).get("name")
            version_group = (d.get("version_group") or {}).get("name")
            min_level = d.get("min_level")
            time_of_day = d.get("time_of_day") or ""
            min_happiness = d.get("min_happiness")
            compact = {
                k: v
                for k, v in {
                    "gender": (d.get("gender") or {}).get("name") if isinstance(d.get("gender"), dict) else d.get("gender"),
                    "known_move": (d.get("known_move") or {}).get("name"),
                    "known_move_type": (d.get("known_move_type") or {}).get("name"),
                    "location": (d.get("location") or {}).get("name"),
                    "min_affection": d.get("min_affection"),
                    "min_beauty": d.get("min_beauty"),
                    "needs_overworld_rain": d.get("needs_overworld_rain"),
                    "relative_physical_stats": d.get("relative_physical_stats"),
                    "turn_upside_down": d.get("turn_upside_down"),
                }.items()
                if v not in (None, False, "")
            }
            out.append(
                (
                    parent,
                    child,
                    trigger,
                    min_level,
                    item,
                    held_item,
                    time_of_day,
                    trade_species,
                    min_happiness,
                    version_group,
                    json.dumps(compact, ensure_ascii=False, sort_keys=True),
                )
            )
        flatten_chain(child_node, out)


def sync_reference_data(con: sqlite3.Connection, workers: int = 8) -> None:
    print("\nSincronizando especies y encuentros de Gen I/II...")
    print("Se descargan #001-#251 una sola vez; luego las consultas son locales.\n")

    bundles: dict[int, tuple[dict, list]] = {}
    errors: list[tuple[int, str]] = []

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as pool:
        future_map = {
            pool.submit(fetch_species_bundle, i): i
            for i in range(1, 252)
        }
        done = 0
        for fut in as_completed(future_map):
            sid = future_map[fut]
            try:
                _, sp, enc = fut.result()
                bundles[sid] = (sp, enc)
            except Exception as exc:
                errors.append((sid, str(exc)))
            done += 1
            if done % 20 == 0 or done == 251:
                print(f"  especies/encuentros: {done}/251")

    if errors:
        print("\nHubo errores de descarga. No se modificará la referencia local.")
        for sid, err in errors[:10]:
            print(f"  #{sid:03d}: {err}")
        if len(errors) > 10:
            print(f"  ... y {len(errors) - 10} errores más.")
        return

    species_rows = []
    chain_ids: set[int] = set()
    encounter_rows = []

    for sid in range(1, 252):
        sp, enc = bundles[sid]
        chain_id = extract_id_from_url(sp["evolution_chain"]["url"])
        chain_ids.add(chain_id)
        species_rows.append(
            (sid, sp["name"], spanish_name(sp), chain_id)
        )

        for area in enc:
            location = area["location_area"]["name"]
            for vd in area.get("version_details", []):
                game = vd.get("version", {}).get("name")
                if game not in GAMES:
                    continue
                details = vd.get("encounter_details", [])
                if not details:
                    # Conservamos al menos una fila.
                    encounter_rows.append(
                        (sid, game, location, "unknown", None, None, vd.get("max_chance"), "")
                    )
                    continue
                for d in details:
                    method = (d.get("method") or {}).get("name") or "unknown"
                    conditions = ",".join(
                        sorted(
                            c.get("name", "")
                            for c in d.get("condition_values", [])
                            if c.get("name")
                        )
                    )
                    encounter_rows.append(
                        (
                            sid,
                            game,
                            location,
                            method,
                            d.get("min_level"),
                            d.get("max_level"),
                            d.get("chance"),
                            conditions,
                        )
                    )

    print(f"\nDescargando {len(chain_ids)} cadenas evolutivas únicas...")
    chain_payloads: dict[int, dict] = {}
    chain_errors = []

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as pool:
        future_map = {
            pool.submit(api_json, f"/evolution-chain/{cid}/"): cid
            for cid in sorted(chain_ids)
        }
        done = 0
        total = len(future_map)
        for fut in as_completed(future_map):
            cid = future_map[fut]
            try:
                chain_payloads[cid] = fut.result()
            except Exception as exc:
                chain_errors.append((cid, str(exc)))
            done += 1
            if done % 20 == 0 or done == total:
                print(f"  cadenas: {done}/{total}")

    if chain_errors:
        print("\nHubo errores con las cadenas evolutivas. No se modificará la referencia local.")
        for cid, err in chain_errors[:10]:
            print(f"  cadena {cid}: {err}")
        return

    evo_rows: list[tuple] = []
    for payload in chain_payloads.values():
        flatten_chain(payload["chain"], evo_rows)

    with con:
        con.execute("DELETE FROM encounters")
        con.execute("DELETE FROM evolution_edges")
        con.execute("DELETE FROM species")

        con.executemany(
            """
            INSERT INTO species(id, name_en, name_es, chain_id)
            VALUES (?, ?, ?, ?)
            """,
            species_rows,
        )
        con.executemany(
            """
            INSERT OR IGNORE INTO encounters
                (species_id, game, location, method, min_level, max_level, chance, conditions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            encounter_rows,
        )
        con.executemany(
            """
            INSERT OR IGNORE INTO evolution_edges
                (parent_id, child_id, trigger, min_level, item, held_item,
                 time_of_day, trade_species, min_happiness, version_group, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            evo_rows,
        )
        meta_set(con, "reference_synced_at", now_iso())
        meta_set(con, "reference_source", "PokeAPI v2 + reglas locales Gen I/II")

    print("\nSincronización terminada.")
    print(f"  Especies: {len(species_rows)}")
    print(f"  Registros de encuentros: {len(encounter_rows)}")
    print(f"  Reglas de evolución: {len(evo_rows)}")
    print(f"  Base local: {con.execute('PRAGMA database_list').fetchone()[2]}\n")


def require_reference(con: sqlite3.Connection) -> bool:
    row = con.execute("SELECT COUNT(*) AS n FROM species").fetchone()
    if row["n"] >= 251:
        return True
    print("\nTodavía no has sincronizado los datos de referencia.")
    print("Ejecuta:  python pokemon_tracker.py --sync")
    print("o usa la opción de sincronización del menú.\n")
    return False


def normalize_text(s: str) -> str:
    return (
        s.strip()
        .lower()
        .replace("é", "e")
        .replace("á", "a")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ü", "u")
        .replace("ñ", "n")
        .replace("♀", "f")
        .replace("♂", "m")
        .replace(".", "")
        .replace("'", "")
        .replace("-", "")
        .replace(" ", "")
    )


def species_display(row: sqlite3.Row | dict) -> str:
    name = row["name_es"] or row["name_en"]
    return f"#{row['id']:03d} {name}"


def find_species(con: sqlite3.Connection, query: str) -> sqlite3.Row | None:
    q = query.strip()
    if not q:
        return None

    if q.isdigit():
        return con.execute(
            "SELECT * FROM species WHERE id=?",
            (int(q),),
        ).fetchone()

    rows = con.execute(
        "SELECT * FROM species WHERE id BETWEEN 1 AND 251 ORDER BY id"
    ).fetchall()
    nq = normalize_text(q)

    exact = []
    partial = []
    for row in rows:
        candidates = [row["name_en"], row["name_es"] or ""]
        norms = [normalize_text(c) for c in candidates if c]
        if nq in norms:
            exact.append(row)
        elif any(nq in n for n in norms):
            partial.append(row)

    if exact:
        return exact[0]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        print("\nCoincidencias:")
        for i, row in enumerate(partial[:20], 1):
            print(f"  {i}. {species_display(row)}")
        sel = ask_int("Elige número (0 cancela): ", 0, min(20, len(partial)))
        if sel:
            return partial[sel - 1]
    return None


def ask_species(con: sqlite3.Connection, prompt: str = "Pokémon (# o nombre): ") -> sqlite3.Row | None:
    while True:
        q = input(prompt).strip()
        if not q:
            return None
        row = find_species(con, q)
        if row:
            return row
        print("No encontrado. Prueba con número Pokédex o nombre.")


def ask_int(prompt: str, minimum: int | None = None, maximum: int | None = None) -> int | None:
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return None
        try:
            value = int(raw)
        except ValueError:
            print("Introduce un número.")
            continue
        if minimum is not None and value < minimum:
            print(f"Debe ser >= {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"Debe ser <= {maximum}.")
            continue
        return value


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [S/n]: " if default else " [s/N]: "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return default
    return raw in {"s", "si", "sí", "y", "yes"}


def choose_game(prompt: str = "Juego: ") -> str | None:
    print("\n  1. Red      2. Blue      3. Yellow")
    print("  4. Gold     5. Silver    6. Crystal")
    print("  0. Cancelar")
    n = ask_int(prompt, 0, 6)
    if not n:
        return None
    return GAMES[n - 1]


def parse_moves(raw: str) -> list[str]:
    if not raw.strip():
        return []
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    if len(parts) > 4:
        print("Solo se guardarán los primeros 4 ataques.")
        parts = parts[:4]
    return parts


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


def get_species(con: sqlite3.Connection, species_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM species WHERE id=?", (species_id,)).fetchone()


def individuals_in_chain(
    con: sqlite3.Connection,
    chain_id: int,
    exclude_id: int | None = None,
) -> list[sqlite3.Row]:
    params: list[Any] = [chain_id]
    extra = ""
    if exclude_id is not None:
        extra = " AND i.id <> ?"
        params.append(exclude_id)
    return con.execute(
        f"""
        SELECT i.*, s.name_en, s.name_es, s.chain_id
        FROM individuals i
        JOIN species s ON s.id=i.species_id
        WHERE s.chain_id=? {extra}
        ORDER BY i.game, i.id
        """,
        params,
    ).fetchall()


def is_terminal(con: sqlite3.Connection, species_id: int, game: str) -> bool:
    max_id = GEN_MAX_SPECIES[game]
    row = con.execute(
        """
        SELECT COUNT(DISTINCT child_id) AS n
        FROM evolution_edges
        WHERE parent_id=? AND child_id <= ?
        """,
        (species_id, max_id),
    ).fetchone()
    if row["n"] == 0:
        return True

    # Yellow Pikachu: el único hijo de Pikachu es Raichu, pero esa evolución
    # está bloqueada dentro de Yellow.
    children = con.execute(
        """
        SELECT DISTINCT child_id
        FROM evolution_edges
        WHERE parent_id=? AND child_id <= ?
        """,
        (species_id, max_id),
    ).fetchall()

    valid_children = [
        r["child_id"]
        for r in children
        if (game, species_id, r["child_id"]) not in BLOCKED_EVOLUTION
    ]
    return len(valid_children) == 0


def add_individual(con: sqlite3.Connection) -> None:
    if not require_reference(con):
        return
    print("\n=== Registrar Pokémon ===")
    species = ask_species(con)
    if not species:
        return
    game = choose_game()
    if not game:
        return

    if species["id"] > GEN_MAX_SPECIES[game]:
        print(f"{species_display(species)} no existe en {GAME_LABEL[game]}.")
        return

    level = ask_int("Nivel (1-100): ", 1, 100)
    if level is None:
        return
    nickname = input("Apodo (Enter si no tiene): ").strip() or None
    moves = parse_moves(
        input("Ataques separados por coma (máx. 4; puede quedar vacío): ")
    )
    notes = input("Notas (opcional): ").strip() or None

    duplicates = con.execute(
        """
        SELECT i.id, i.game, i.level, i.nickname, s.name_en, s.name_es
        FROM individuals i
        JOIN species s ON s.id=i.species_id
        WHERE i.species_id=?
        ORDER BY i.id
        """,
        (species["id"],),
    ).fetchall()

    if duplicates:
        print("\nAviso: ya tienes registrado este Pokémon:")
        for d in duplicates:
            label = d["name_es"] or d["name_en"]
            print(f"  ID {d['id']} | {label} Nv.{d['level']} | {GAME_LABEL[d['game']]}")
        if not ask_yes_no("¿Registrar otro ejemplar de todos modos?"):
            return

    if species["chain_id"]:
        family = individuals_in_chain(con, species["chain_id"])
        if family and not duplicates:
            print("\nAviso de línea evolutiva: ya tienes ejemplares de esta familia:")
            for d in family:
                label = d["name_es"] or d["name_en"]
                print(f"  ID {d['id']} | {label} Nv.{d['level']} | {GAME_LABEL[d['game']]}")
            print("Esto puede ser correcto en líneas bifurcadas (por ejemplo Eevee).")
            if not ask_yes_no("¿Continuar?"):
                return

    party_count = con.execute(
        "SELECT COUNT(*) AS n FROM individuals WHERE game=? AND status='party'",
        (game,),
    ).fetchone()["n"]
    status = "box"
    if party_count < 6 and ask_yes_no("¿Añadirlo al party de este juego?"):
        status = "party"

    ts = now_iso()
    with con:
        cur = con.execute(
            """
            INSERT INTO individuals
                (nickname, species_id, level, game, moves_json, status, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                nickname,
                species["id"],
                level,
                game,
                json.dumps(moves, ensure_ascii=False),
                status,
                notes,
                ts,
                ts,
            ),
        )
        iid = cur.lastrowid
        add_dex_entry(con, game, species["id"], iid, "registrado")
        con.execute(
            """
            INSERT INTO history
                (individual_id, event_type, game_to, species_to, details, created_at)
            VALUES (?, 'add', ?, ?, ?, ?)
            """,
            (iid, game, species["id"], f"Nivel {level}", ts),
        )

    print(f"\nRegistrado como ID {iid}: {species_display(species)} Nv.{level} en {GAME_LABEL[game]}.")


def individual_label(row: sqlite3.Row) -> str:
    name = row["name_es"] or row["name_en"]
    nick = f'"{row["nickname"]}" ' if row["nickname"] else ""
    status = {"party": "PARTY", "box": "BOX", "retired": "RETIRADO"}[row["status"]]
    return (
        f"ID {row['id']:>3} | {nick}{name:<14} | Nv.{row['level']:<3} | "
        f"{GAME_LABEL[row['game']]:<7} | {status}"
    )


def list_individuals(
    con: sqlite3.Connection,
    game: str | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    clauses = []
    params: list[Any] = []
    if game:
        clauses.append("i.game=?")
        params.append(game)
    if status:
        clauses.append("i.status=?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return con.execute(
        f"""
        SELECT i.*, s.name_en, s.name_es, s.chain_id
        FROM individuals i
        JOIN species s ON s.id=i.species_id
        {where}
        ORDER BY
            CASE i.game
                WHEN 'red' THEN 1 WHEN 'blue' THEN 2 WHEN 'yellow' THEN 3
                WHEN 'gold' THEN 4 WHEN 'silver' THEN 5 WHEN 'crystal' THEN 6
            END,
            CASE i.status WHEN 'party' THEN 1 WHEN 'box' THEN 2 ELSE 3 END,
            i.id
        """,
        params,
    ).fetchall()


def show_roster(con: sqlite3.Connection) -> None:
    if not require_reference(con):
        return
    rows = list_individuals(con)
    print("\n=== Todos tus Pokémon ===")
    if not rows:
        print("Todavía no hay ejemplares registrados.")
        return
    current_game = None
    for row in rows:
        if row["game"] != current_game:
            current_game = row["game"]
            print(f"\n[{GAME_LABEL[current_game]}]")
        print(" ", individual_label(row))
        moves = json.loads(row["moves_json"] or "[]")
        if moves:
            print("       Movimientos:", ", ".join(moves))


def choose_individual(con: sqlite3.Connection, prompt: str = "ID del ejemplar (Enter cancela): ") -> sqlite3.Row | None:
    rows = list_individuals(con)
    if not rows:
        print("No hay Pokémon registrados.")
        return None
    print()
    for row in rows:
        print(" ", individual_label(row))
    iid = ask_int(prompt, 1)
    if iid is None:
        return None
    return con.execute(
        """
        SELECT i.*, s.name_en, s.name_es, s.chain_id
        FROM individuals i
        JOIN species s ON s.id=i.species_id
        WHERE i.id=?
        """,
        (iid,),
    ).fetchone()


def update_individual(con: sqlite3.Connection) -> None:
    if not require_reference(con):
        return
    print("\n=== Actualizar Pokémon ===")
    row = choose_individual(con)
    if not row:
        return

    print("\nEnter conserva el valor actual.")
    level = ask_int(f"Nivel actual [{row['level']}]: ", 1, 100)
    if level is None:
        level = row["level"]

    current_moves = json.loads(row["moves_json"] or "[]")
    raw_moves = input(
        f"Ataques [{', '.join(current_moves) if current_moves else 'ninguno'}]: "
    ).strip()
    moves = parse_moves(raw_moves) if raw_moves else current_moves

    nickname_raw = input(
        f"Apodo [{row['nickname'] or 'sin apodo'}] (escribe - para quitarlo): "
    ).strip()
    if nickname_raw == "":
        nickname = row["nickname"]
    elif nickname_raw == "-":
        nickname = None
    else:
        nickname = nickname_raw

    notes_raw = input(
        f"Notas [{row['notes'] or 'vacío'}] (Enter conserva): "
    ).strip()
    notes = row["notes"] if notes_raw == "" else notes_raw

    with con:
        con.execute(
            """
            UPDATE individuals
            SET level=?, moves_json=?, nickname=?, notes=?, updated_at=?
            WHERE id=?
            """,
            (
                level,
                json.dumps(moves, ensure_ascii=False),
                nickname,
                notes,
                now_iso(),
                row["id"],
            ),
        )
        con.execute(
            """
            INSERT INTO history
                (individual_id, event_type, game_from, game_to,
                 species_from, species_to, details, created_at)
            VALUES (?, 'update', ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["game"],
                row["game"],
                row["species_id"],
                row["species_id"],
                f"Nivel {row['level']} -> {level}; movimientos={moves}",
                now_iso(),
            ),
        )
    print("Actualizado.")


def evolution_method_text(edge: sqlite3.Row) -> str:
    bits = []
    trigger = edge["trigger"] or "evolución"
    trigger_map = {
        "level-up": "subir nivel",
        "use-item": "usar objeto",
        "trade": "intercambio",
        "shed": "especial",
        "spin": "especial",
        "tower-of-darkness": "especial",
        "tower-of-waters": "especial",
        "three-critical-hits": "especial",
        "take-damage": "especial",
        "agile-style-move": "especial",
        "strong-style-move": "especial",
        "recoil-damage": "especial",
        "other": "especial",
    }
    bits.append(trigger_map.get(trigger, trigger))
    if edge["min_level"]:
        bits.append(f"Nv.{edge['min_level']}")
    if edge["item"]:
        bits.append(pretty_slug(edge["item"]))
    if edge["held_item"]:
        bits.append("llevar " + pretty_slug(edge["held_item"]))
    if edge["time_of_day"]:
        bits.append(edge["time_of_day"])
    if edge["min_happiness"]:
        bits.append(f"amistad >= {edge['min_happiness']}")
    return " / ".join(bits)


def valid_evolution_edges(
    con: sqlite3.Connection,
    species_id: int,
    game: str,
) -> list[sqlite3.Row]:
    max_id = GEN_MAX_SPECIES[game]
    rows = con.execute(
        """
        SELECT *
        FROM evolution_edges
        WHERE parent_id=? AND child_id <= ?
        ORDER BY child_id
        """,
        (species_id, max_id),
    ).fetchall()

    dedup = {}
    for row in rows:
        if (game, species_id, row["child_id"]) in BLOCKED_EVOLUTION:
            continue
        key = (row["child_id"], row["trigger"], row["item"], row["held_item"], row["min_level"])
        dedup[key] = row
    return list(dedup.values())


def evolve_individual(
    con: sqlite3.Connection,
    row: sqlite3.Row | None = None,
    forced_child: int | None = None,
    reason: str = "evolución",
) -> sqlite3.Row | None:
    if not require_reference(con):
        return None
    if row is None:
        print("\n=== Evolucionar Pokémon ===")
        row = choose_individual(con)
        if not row:
            return None

    edges = valid_evolution_edges(con, row["species_id"], row["game"])
    if forced_child is not None:
        edges = [e for e in edges if e["child_id"] == forced_child]

    if not edges:
        print("No hay una evolución válida registrada para este Pokémon en este juego.")
        return None

    # Agrupar por hijo para evitar mostrar varias condiciones equivalentes.
    grouped: dict[int, list[sqlite3.Row]] = {}
    for edge in edges:
        grouped.setdefault(edge["child_id"], []).append(edge)

    choices = []
    print("\nEvoluciones disponibles:")
    for i, (child_id, child_edges) in enumerate(grouped.items(), 1):
        child = get_species(con, child_id)
        methods = "; ".join(sorted(set(evolution_method_text(e) for e in child_edges)))
        print(f"  {i}. {species_display(child)}  [{methods}]")
        choices.append((child_id, child_edges[0]))

    if forced_child is not None:
        selected = choices[0]
    else:
        n = ask_int("Elige evolución (0 cancela): ", 0, len(choices))
        if not n:
            return None
        selected = choices[n - 1]

    child_id, edge = selected
    child = get_species(con, child_id)
    old_species = row["species_id"]
    ts = now_iso()

    with con:
        con.execute(
            """
            UPDATE individuals
            SET species_id=?, updated_at=?
            WHERE id=?
            """,
            (child_id, ts, row["id"]),
        )
        # La forma anterior queda conservada en dex_entries. Solo añadimos la nueva.
        add_dex_entry(con, row["game"], child_id, row["id"], reason)
        con.execute(
            """
            INSERT INTO history
                (individual_id, event_type, game_from, game_to,
                 species_from, species_to, details, created_at)
            VALUES (?, 'evolve', ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["game"],
                row["game"],
                old_species,
                child_id,
                evolution_method_text(edge),
                ts,
            ),
        )

    print(f"\nEvolucionado: {species_display(get_species(con, old_species))} -> {species_display(child)}.")

    new_row = con.execute(
        """
        SELECT i.*, s.name_en, s.name_es, s.chain_id
        FROM individuals i
        JOIN species s ON s.id=i.species_id
        WHERE i.id=?
        """,
        (row["id"],),
    ).fetchone()

    if is_terminal(con, child_id, row["game"]):
        print("Esta forma es terminal en la generación de este juego.")
        if new_row["status"] == "party" and ask_yes_no(
            "¿Retirarlo del party para renovar tu equipo?"
        ):
            with con:
                con.execute(
                    "UPDATE individuals SET status='retired', updated_at=? WHERE id=?",
                    (now_iso(), row["id"]),
                )
                con.execute(
                    """
                    INSERT INTO history
                        (individual_id, event_type, game_from, game_to,
                         species_from, species_to, details, created_at)
                    VALUES (?, 'retire', ?, ?, ?, ?, 'Forma final alcanzada', ?)
                    """,
                    (
                        row["id"],
                        row["game"],
                        row["game"],
                        child_id,
                        child_id,
                        now_iso(),
                    ),
                )
            print("Marcado como RETIRADO.")
            new_row = con.execute(
                """
                SELECT i.*, s.name_en, s.name_es, s.chain_id
                FROM individuals i JOIN species s ON s.id=i.species_id
                WHERE i.id=?
                """,
                (row["id"],),
            ).fetchone()
    return new_row


def compatible_trade(source: str, target: str, species_id: int) -> tuple[bool, str]:
    if source == target:
        return False, "Ya está en ese juego."
    if source in GEN1 and target in GEN1:
        return True, "Intercambio Gen I."
    if source in GEN2 and target in GEN2:
        return True, "Intercambio Gen II."
    # Time Capsule: solo especies de Gen I hacia RBY.
    if species_id > 151:
        return False, "Una especie de Gen II no puede pasar por la Time Capsule a R/B/Y."
    return True, (
        "Time Capsule: la especie es compatible, pero si vuelve a Gen I "
        "debe conocer únicamente movimientos existentes en Gen I y no llevar objetos incompatibles."
    )


def trade_evolution_children(
    con: sqlite3.Connection,
    species_id: int,
    game: str,
) -> list[int]:
    max_id = GEN_MAX_SPECIES[game]
    rows = con.execute(
        """
        SELECT DISTINCT child_id
        FROM evolution_edges
        WHERE parent_id=? AND child_id <= ? AND trigger='trade'
        ORDER BY child_id
        """,
        (species_id, max_id),
    ).fetchall()
    ids = [r["child_id"] for r in rows]
    if not ids and species_id in TRADE_EVOLUTIONS_FALLBACK:
        child = TRADE_EVOLUTIONS_FALLBACK[species_id]
        if child <= max_id:
            ids.append(child)
    return ids


def trade_individual(con: sqlite3.Connection) -> None:
    if not require_reference(con):
        return
    print("\n=== Intercambiar Pokémon ===")
    row = choose_individual(con)
    if not row:
        return

    print(f"\nOrigen: {GAME_LABEL[row['game']]}")
    target = choose_game("Juego de destino: ")
    if not target:
        return

    ok, message = compatible_trade(row["game"], target, row["species_id"])
    print(message)
    if not ok:
        return
    if not ask_yes_no("¿Confirmar intercambio?"):
        return

    old_game = row["game"]
    ts = now_iso()
    with con:
        con.execute(
            """
            UPDATE individuals
            SET game=?, status='box', updated_at=?
            WHERE id=?
            """,
            (target, ts, row["id"]),
        )
        # El destino registra la especie en su Pokédex; el origen conserva su entrada.
        add_dex_entry(con, target, row["species_id"], row["id"], "intercambio recibido")
        con.execute(
            """
            INSERT INTO history
                (individual_id, event_type, game_from, game_to,
                 species_from, species_to, details, created_at)
            VALUES (?, 'trade', ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                old_game,
                target,
                row["species_id"],
                row["species_id"],
                "Intercambio entre títulos",
                ts,
            ),
        )

    print(f"Ahora está en {GAME_LABEL[target]} y ha pasado al BOX.")

    updated = con.execute(
        """
        SELECT i.*, s.name_en, s.name_es, s.chain_id
        FROM individuals i JOIN species s ON s.id=i.species_id
        WHERE i.id=?
        """,
        (row["id"],),
    ).fetchone()

    trade_children = trade_evolution_children(con, updated["species_id"], target)
    if trade_children:
        names = [species_display(get_species(con, c)) for c in trade_children]
        print("Este intercambio puede provocar evolución:", ", ".join(names))
        if ask_yes_no("¿Evolucionó durante el intercambio?"):
            if len(trade_children) == 1:
                evolve_individual(con, updated, forced_child=trade_children[0], reason="evolución por intercambio")
            else:
                evolve_individual(con, updated, reason="evolución por intercambio")


def manage_party(con: sqlite3.Connection) -> None:
    if not require_reference(con):
        return
    print("\n=== Gestionar party ===")
    game = choose_game()
    if not game:
        return

    party = list_individuals(con, game=game, status="party")
    print(f"\nParty de {GAME_LABEL[game]} ({len(party)}/6)")
    if party:
        for row in party:
            terminal = " [FINAL]" if is_terminal(con, row["species_id"], game) else ""
            print(" ", individual_label(row) + terminal)
    else:
        print("  (vacío)")

    print("\n1. Añadir desde BOX/RETIRADO")
    print("2. Sacar del party al BOX")
    print("3. Marcar miembro como RETIRADO")
    print("0. Volver")
    op = ask_int("Opción: ", 0, 3)
    if not op:
        return

    if op == 1:
        if len(party) >= 6:
            print("El party ya tiene 6 Pokémon.")
            return
        candidates = [
            r for r in list_individuals(con, game=game)
            if r["status"] != "party"
        ]
        if not candidates:
            print("No hay candidatos en este juego.")
            return
        for row in candidates:
            print(" ", individual_label(row))
        iid = ask_int("ID a añadir: ", 1)
        if iid is None:
            return
        chosen = next((r for r in candidates if r["id"] == iid), None)
        if not chosen:
            print("ID no válido para este juego.")
            return
        with con:
            con.execute(
                "UPDATE individuals SET status='party', updated_at=? WHERE id=?",
                (now_iso(), iid),
            )
            con.execute(
                """
                INSERT INTO history(individual_id, event_type, game_from, game_to,
                                    species_from, species_to, details, created_at)
                VALUES (?, 'party-in', ?, ?, ?, ?, 'Añadido al party', ?)
                """,
                (iid, game, game, chosen["species_id"], chosen["species_id"], now_iso()),
            )
        print("Añadido al party.")

    elif op in (2, 3):
        if not party:
            print("No hay miembros en el party.")
            return
        iid = ask_int("ID del miembro: ", 1)
        if iid is None:
            return
        chosen = next((r for r in party if r["id"] == iid), None)
        if not chosen:
            print("Ese ID no pertenece al party seleccionado.")
            return
        new_status = "box" if op == 2 else "retired"
        event = "party-out" if op == 2 else "retire"
        with con:
            con.execute(
                "UPDATE individuals SET status=?, updated_at=? WHERE id=?",
                (new_status, now_iso(), iid),
            )
            con.execute(
                """
                INSERT INTO history(individual_id, event_type, game_from, game_to,
                                    species_from, species_to, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iid,
                    event,
                    game,
                    game,
                    chosen["species_id"],
                    chosen["species_id"],
                    "Salida del party",
                    now_iso(),
                ),
            )
        print(f"Estado cambiado a {new_status.upper()}.")


def native_status(game: str, species_id: int) -> str:
    if species_id > GEN_MAX_SPECIES[game]:
        return "not-in-generation"
    if (game, species_id) in EVENT_ONLY:
        return "event"
    if species_id in NATIVE_MISSING[game]:
        return "missing"
    return "available"


def evolution_parents(con: sqlite3.Connection, species_id: int, game: str) -> list[sqlite3.Row]:
    max_id = GEN_MAX_SPECIES[game]
    return con.execute(
        """
        SELECT DISTINCT e.parent_id, e.trigger, e.min_level, e.item, e.held_item,
                        e.time_of_day, e.min_happiness,
                        s.name_en, s.name_es, s.chain_id
        FROM evolution_edges e
        JOIN species s ON s.id=e.parent_id
        WHERE e.child_id=? AND e.parent_id <= ?
        ORDER BY e.parent_id
        """,
        (species_id, max_id),
    ).fetchall()


def show_species_info(con: sqlite3.Connection) -> None:
    if not require_reference(con):
        return
    print("\n=== Consultar especie y localizaciones ===")
    sp = ask_species(con)
    if not sp:
        return

    print(f"\n{species_display(sp)}")
    print(f"Línea evolutiva (chain_id): {sp['chain_id']}")

    games = GAMES if sp["id"] <= 151 else ("gold", "silver", "crystal")

    for game in games:
        print(f"\n--- {GAME_LABEL[game]} ---")
        status = native_status(game, sp["id"])

        if status == "not-in-generation":
            print("No existe en esta generación.")
            continue

        if status == "event":
            print("EVENTO / no disponible mediante juego normal.")
            print(" ", EVENT_ONLY[(game, sp["id"])])
        elif status == "missing":
            print("NO DISPONIBLE de forma nativa en este título.")
            print("Debe llegar mediante intercambio desde otro título compatible.")
        else:
            print("Disponible en este título.")

        specials = con.execute(
            """
            SELECT place, method, note
            FROM special_acquisition
            WHERE species_id=? AND game=?
            ORDER BY id
            """,
            (sp["id"], game),
        ).fetchall()
        if specials:
            print("Adquisición especial:")
            for r in specials:
                print(
                    f"  - {r['place']} | {method_es(r['method'])}"
                    + (f" | {r['note']}" if r["note"] else "")
                )

        enc = con.execute(
            """
            SELECT location, method,
                   MIN(min_level) AS min_level,
                   MAX(max_level) AS max_level,
                   MAX(chance) AS chance,
                   GROUP_CONCAT(DISTINCT conditions) AS conditions
            FROM encounters
            WHERE species_id=? AND game=?
            GROUP BY location, method
            ORDER BY location, method
            """,
            (sp["id"], game),
        ).fetchall()

        if enc:
            print("Encuentros salvajes:")
            for r in enc:
                lvl = ""
                if r["min_level"] is not None:
                    if r["min_level"] == r["max_level"]:
                        lvl = f" Nv.{r['min_level']}"
                    else:
                        lvl = f" Nv.{r['min_level']}-{r['max_level']}"
                cond = ""
                if r["conditions"]:
                    clean = ", ".join(
                        pretty_slug(x)
                        for x in sorted(
                            {c for c in r["conditions"].split(",") if c}
                        )
                    )
                    if clean:
                        cond = f" [{clean}]"
                chance = f" ~{r['chance']}%" if r["chance"] is not None else ""
                print(
                    f"  - {pretty_slug(r['location'])} | {method_es(r['method'])}"
                    f"{lvl}{chance}{cond}"
                )

        parents = evolution_parents(con, sp["id"], game)
        if parents:
            print("También puede obtenerse por evolución desde:")
            seen = set()
            for p in parents:
                if p["parent_id"] in seen:
                    continue
                seen.add(p["parent_id"])
                name = p["name_es"] or p["name_en"]
                extra = []
                if p["trigger"] == "trade":
                    extra.append("intercambio")
                elif p["trigger"] == "use-item":
                    extra.append("objeto")
                elif p["trigger"] == "level-up":
                    extra.append("nivel")
                if p["min_level"]:
                    extra.append(f"Nv.{p['min_level']}")
                if p["item"]:
                    extra.append(pretty_slug(p["item"]))
                print(f"  - #{p['parent_id']:03d} {name}" + (f" ({', '.join(extra)})" if extra else ""))

        if not enc and not specials and not parents and status == "available":
            print("No hay ubicación directa registrada; puede depender de crianza, premio u otro método especial.")


def progress_rows(con: sqlite3.Connection, game: str) -> tuple[int, int, list[int]]:
    max_target = DEX_TARGET_MAX[game]
    owned = {
        r["species_id"]
        for r in con.execute(
            """
            SELECT species_id
            FROM dex_entries
            WHERE game=? AND species_id BETWEEN 1 AND ?
            """,
            (game, max_target),
        ).fetchall()
    }
    missing = [i for i in range(1, max_target + 1) if i not in owned]
    return len(owned), max_target, missing


def show_progress(con: sqlite3.Connection) -> None:
    if not require_reference(con):
        return
    print("\n=== Progreso del Pokédex por título ===\n")
    for game in GAMES:
        owned, target, missing = progress_rows(con, game)
        pct = (owned / target * 100) if target else 0
        print(
            f"{GAME_LABEL[game]:<7}: {owned:>3}/{target:<3}  "
            f"{pct:6.2f}%  | faltan {len(missing)}"
        )

    print("\nObjetivos configurados:")
    print("  R/B/Y = #001-#150 (Mew #151 no cuenta).")
    print("  G/S/C = #001-#250 (Celebi #251 no cuenta).")
    print("  Nota: Mew #151 sí está dentro del rango #001-#250 de tu objetivo de Gen II.")

    if ask_yes_no("\n¿Ver las especies que faltan en un juego?"):
        game = choose_game()
        if not game:
            return
        _, _, missing = progress_rows(con, game)
        print(f"\nFaltantes en {GAME_LABEL[game]}:")
        for sid in missing:
            sp = get_species(con, sid)
            status = native_status(game, sid)
            marker = {
                "available": "LOCAL",
                "missing": "TRADE",
                "event": "EVENTO",
                "not-in-generation": "N/A",
            }[status]
            print(f"  {species_display(sp):<22} [{marker}]")


def next_captures(con: sqlite3.Connection) -> None:
    if not require_reference(con):
        return
    print("\n=== Próximas capturas útiles ===")
    game = choose_game()
    if not game:
        return
    _, _, missing = progress_rows(con, game)

    direct = []
    special_only = []
    trade_needed = []
    other = []

    for sid in missing:
        status = native_status(game, sid)
        sp = get_species(con, sid)
        if status in {"missing", "event"}:
            trade_needed.append((sp, status))
            continue

        enc = con.execute(
            """
            SELECT location, method, MIN(min_level) AS min_level
            FROM encounters
            WHERE species_id=? AND game=?
            GROUP BY location, method
            ORDER BY CASE WHEN min_level IS NULL THEN 999 ELSE min_level END, location
            LIMIT 1
            """,
            (sid, game),
        ).fetchone()
        if enc:
            direct.append((sp, enc))
            continue

        special = con.execute(
            """
            SELECT place, method, note
            FROM special_acquisition
            WHERE species_id=? AND game=?
            ORDER BY id LIMIT 1
            """,
            (sid, game),
        ).fetchone()
        if special:
            special_only.append((sp, special))
            continue

        parents = evolution_parents(con, sid, game)
        if parents:
            other.append((sp, "evolución"))
        else:
            other.append((sp, "sin ubicación directa en la base"))

    print(f"\nFaltantes con encuentro salvaje en {GAME_LABEL[game]}: {len(direct)}")
    for sp, enc in direct[:30]:
        lvl = f" Nv.{enc['min_level']}" if enc["min_level"] else ""
        print(
            f"  {species_display(sp):<22} -> "
            f"{pretty_slug(enc['location'])} / {method_es(enc['method'])}{lvl}"
        )
    if len(direct) > 30:
        print(f"  ... y {len(direct) - 30} más.")

    print(f"\nFaltantes con adquisición especial: {len(special_only)}")
    for sp, r in special_only[:30]:
        print(f"  {species_display(sp):<22} -> {r['place']} / {method_es(r['method'])}")

    print(f"\nFaltantes por evolución u otro método: {len(other)}")
    for sp, why in other[:30]:
        print(f"  {species_display(sp):<22} -> {why}")

    print(f"\nFaltantes que requieren otro título/evento: {len(trade_needed)}")
    for sp, status in trade_needed[:30]:
        print(f"  {species_display(sp):<22} -> {'EVENTO' if status == 'event' else 'INTERCAMBIO'}")


def show_history(con: sqlite3.Connection) -> None:
    print("\n=== Historial reciente ===")
    rows = con.execute(
        """
        SELECT h.*, sf.name_es AS from_es, sf.name_en AS from_en,
               st.name_es AS to_es, st.name_en AS to_en
        FROM history h
        LEFT JOIN species sf ON sf.id=h.species_from
        LEFT JOIN species st ON st.id=h.species_to
        ORDER BY h.id DESC
        LIMIT 50
        """
    ).fetchall()
    if not rows:
        print("Sin historial.")
        return
    for r in rows:
        s_from = r["from_es"] or r["from_en"] or "-"
        s_to = r["to_es"] or r["to_en"] or "-"
        g_from = GAME_LABEL.get(r["game_from"], "-")
        g_to = GAME_LABEL.get(r["game_to"], "-")
        print(
            f"  {r['created_at']} | ID {r['individual_id'] or '-'} | "
            f"{r['event_type']:<9} | {s_from} -> {s_to} | {g_from} -> {g_to}"
        )
        if r["details"]:
            print("      ", r["details"])


def backup_db(con: sqlite3.Connection, db_path: Path) -> None:
    print("\n=== Copia de seguridad ===")
    print(f"Base actual: {db_path}")
    default_shared = Path.home() / "storage" / "shared" / "PokemonTracker"
    raw = input(
        f"Directorio destino [Enter = {default_shared}]: "
    ).strip()
    dest_dir = Path(raw).expanduser() if raw else default_shared

    if not dest_dir.exists():
        if dest_dir.parts[: len((Path.home() / "storage").parts)] == (Path.home() / "storage").parts:
            print("\nNo existe el acceso al almacenamiento compartido de Termux.")
            print("Ejecuta una vez: termux-setup-storage")
            return
        dest_dir.mkdir(parents=True, exist_ok=True)

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"pokemon_tracker_{stamp}.sqlite3"

    # VACUUM/backup API para una copia consistente.
    out = sqlite3.connect(dest)
    try:
        con.backup(out)
    finally:
        out.close()
    print(f"Copia creada: {dest}")


def show_menu(con: sqlite3.Connection, db_path: Path) -> None:
    while True:
        synced = meta_get(con, "reference_synced_at")
        print("\n" + "=" * 62)
        print(" POKÉMON GEN I-II TRACKER")
        print("=" * 62)
        print(" 1. Registrar Pokémon")
        print(" 2. Ver todos mis Pokémon")
        print(" 3. Actualizar nivel / ataques / notas")
        print(" 4. Evolucionar Pokémon")
        print(" 5. Intercambiar entre títulos")
        print(" 6. Gestionar party")
        print(" 7. Consultar especie y localizaciones")
        print(" 8. Ver progreso del Pokédex")
        print(" 9. Ver próximas capturas útiles")
        print("10. Ver historial")
        print("11. Sincronizar/actualizar datos de referencia")
        print("12. Crear copia de seguridad SQLite")
        print(" 0. Salir")
        print("-" * 62)
        print(f"DB: {db_path}")
        print(f"Referencia: {synced or 'NO sincronizada'}")
        op = ask_int("Opción: ", 0, 12)

        try:
            if op == 0:
                print("Hasta luego.")
                return
            elif op == 1:
                add_individual(con)
            elif op == 2:
                show_roster(con)
            elif op == 3:
                update_individual(con)
            elif op == 4:
                evolve_individual(con)
            elif op == 5:
                trade_individual(con)
            elif op == 6:
                manage_party(con)
            elif op == 7:
                show_species_info(con)
            elif op == 8:
                show_progress(con)
            elif op == 9:
                next_captures(con)
            elif op == 10:
                show_history(con)
            elif op == 11:
                sync_reference_data(con)
            elif op == 12:
                backup_db(con, db_path)
        except KeyboardInterrupt:
            print("\nOperación cancelada.")
        except EOFError:
            print("\nFin de entrada.")
            return
        except Exception as exc:
            print(f"\nERROR: {exc}")
            print("La base no se ha borrado. Puedes volver al menú y reintentar.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gestor local de Pokémon Gen I/II para R/B/Y y G/S/C."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Ruta del SQLite (por defecto: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Sincroniza PokéAPI y termina.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Conexiones simultáneas durante --sync (por defecto 8).",
    )
    args = parser.parse_args()

    db_path = args.db.expanduser()
    con = connect(db_path)
    try:
        init_db(con)
        if args.sync:
            sync_reference_data(con, workers=args.workers)
            return
        show_menu(con, db_path)
    finally:
        con.close()


if __name__ == "__main__":
    main()
