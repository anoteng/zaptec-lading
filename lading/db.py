"""SQLite-lagring. Alle tider er epoch-sekunder (UTC)."""
import json
import sqlite3
from pathlib import Path

SKJEMA = """
CREATE TABLE IF NOT EXISTS maling (          -- Tibber Pulse, hvert ~10. s
  ts INTEGER PRIMARY KEY, effekt_w REAL, time_kwh REAL, maaler_kwh REAL,
  i1 REAL, i3 REAL, u1 REAL, u2 REAL, u3 REAL);
CREATE TABLE IF NOT EXISTS maalerstand (     -- endringer i HAN-målerens telleverk (kommer ca. hver time)
  ts INTEGER PRIMARY KEY, kwh REAL);
CREATE TABLE IF NOT EXISTS time_hus (        -- husets forbruk per klokketime fra tre kilder
  time INTEGER PRIMARY KEY, estimat REAL, maaler REAL, elvia REAL, elvia_verifisert INTEGER);
CREATE TABLE IF NOT EXISTS lader (           -- Zaptec-status, hvert minutt
  ts INTEGER PRIMARY KEY, online INTEGER, modus INTEGER, effekt_w REAL, okt_kwh REAL,
  strom_satt REAL, i1 REAL, i2 REAL, i3 REAL, final_stop INTEGER);
CREATE TABLE IF NOT EXISTS time_lader (      -- laderens energi per klokketime (integrert effekt)
  time INTEGER PRIMARY KEY, kwh REAL);
CREATE TABLE IF NOT EXISTS lader_maaler (    -- laderens signerte MID-måler
  ts INTEGER PRIMARY KEY, kwh REAL);
CREATE TABLE IF NOT EXISTS pris (            -- spotpris eks. mva
  start INTEGER PRIMARY KEY, slutt INTEGER, nok_kwh REAL);
CREATE TABLE IF NOT EXISTS innstilling (     -- små verdier som skal overleve omstart (JSON)
  nokkel TEXT PRIMARY KEY, verdi TEXT);
CREATE TABLE IF NOT EXISTS styring_logg (    -- styringens beslutninger og kommandoer
  ts REAL, handling TEXT, detaljer TEXT, utfort INTEGER, feil TEXT);
CREATE INDEX IF NOT EXISTS styring_logg_ts ON styring_logg(ts);
"""


class DB:
    def __init__(self, sti: str):
        Path(sti).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(sti, isolation_level=None, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript(SKJEMA)

    def x(self, sql: str, *args):
        return self.c.execute(sql, args)

    def alle(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.c.execute(sql, args).fetchall()

    def en(self, sql: str, *args):
        return self.c.execute(sql, args).fetchone()

    # --- hus ---
    def lagre_maling(self, ts, effekt, time_kwh, maaler, i1, i3, u1, u2, u3):
        self.x("INSERT OR REPLACE INTO maling VALUES (?,?,?,?,?,?,?,?,?)",
               ts, effekt, time_kwh, maaler, i1, i3, u1, u2, u3)

    def sett_time_hus(self, time: int, felt: str, kwh: float, verifisert: int | None = None):
        assert felt in ("estimat", "maaler", "elvia")
        self.x(f"INSERT INTO time_hus(time, {felt}) VALUES (?, ?) "
               f"ON CONFLICT(time) DO UPDATE SET {felt} = excluded.{felt}", time, kwh)
        if verifisert is not None:
            self.x("UPDATE time_hus SET elvia_verifisert = ? WHERE time = ?", verifisert, time)

    # --- innstillinger ---
    def hent(self, nokkel: str, standard=None):
        r = self.en("SELECT verdi FROM innstilling WHERE nokkel = ?", nokkel)
        return json.loads(r["verdi"]) if r else standard

    def lagre(self, nokkel: str, verdi):
        if verdi is None:
            self.x("DELETE FROM innstilling WHERE nokkel = ?", nokkel)
        else:
            self.x("INSERT OR REPLACE INTO innstilling VALUES (?, ?)", nokkel, json.dumps(verdi))

    # --- lader ---
    def legg_til_lader_kwh(self, time: int, kwh: float):
        self.x("INSERT INTO time_lader VALUES (?, ?) ON CONFLICT(time) DO UPDATE SET kwh = kwh + excluded.kwh",
               time, kwh)
