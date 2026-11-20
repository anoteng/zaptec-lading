"""Spotpriser fra hvakosterstrommen.no (eks. mva)."""
import asyncio
import datetime as dt
import logging

import aiohttp

from .db import DB
from .tid import OSLO, naa
from .tilstand import Tilstand

log = logging.getLogger("lading.priser")
URL = "https://www.hvakosterstrommen.no/api/v1/prices/{d:%Y}/{d:%m}-{d:%d}_{omr}.json"


async def hent_dag(http: aiohttp.ClientSession, db: DB, dag: dt.date, omr: str) -> bool:
    async with http.get(URL.format(d=dag, omr=omr)) as r:
        if r.status == 404:
            return False  # ikke publisert ennå
        r.raise_for_status()
        rader = await r.json()
    for p in rader:
        db.x("INSERT OR REPLACE INTO pris VALUES (?,?,?)",
             int(dt.datetime.fromisoformat(p["time_start"]).timestamp()),
             int(dt.datetime.fromisoformat(p["time_end"]).timestamp()), p["NOK_per_kWh"])
    return bool(rader)


def har_dag(db: DB, dag: dt.date) -> bool:
    start = int(dt.datetime(dag.year, dag.month, dag.day, tzinfo=OSLO).timestamp())
    return db.en("SELECT COUNT(*) n FROM pris WHERE start >= ? AND start < ?", start, start + 86400 + 3600)["n"] >= 23


async def historikk(http: aiohttp.ClientSession, db: DB, cfg: dict, kilde, feilet: set[dt.date]) -> int:
    """Henter manglende dager fra historikk_fra til i går, ett kall i sekundet. Returnerer antall hentet."""
    fra = cfg["priser"].get("historikk_fra")
    if not fra:
        return 0
    omr, d, igaar = cfg["priser"]["omraade"], dt.date.fromisoformat(fra), naa().date() - dt.timedelta(days=1)
    mangler = []
    while d <= igaar:
        if d not in feilet and not har_dag(db, d):
            mangler.append(d)
        d += dt.timedelta(days=1)
    for i, d in enumerate(mangler):
        kilde.ok(f"henter historiske priser ({len(mangler) - i} dager igjen)")
        try:
            if not await hent_dag(http, db, d, omr):
                feilet.add(d)
        except aiohttp.ClientResponseError as e:
            log.warning("historisk pris %s: %s", d, e)
            feilet.add(d)
        await asyncio.sleep(1)
    if mangler:
        log.info("hentet historiske spotpriser for %d dager", len(mangler) - len(feilet & set(mangler)))
    return len(mangler)


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
    omr = cfg["priser"]["omraade"]
    kilde = tilstand.kilder["priser"]
    feilet: set[dt.date] = set()  # historiske dager som ikke finnes i API-et; prøves igjen ved omstart
    while True:
        try:
            idag = naa().date()
            dager = [idag] + ([idag + dt.timedelta(days=1)] if naa().hour >= 13 else [])
            for d in dager:
                if not har_dag(db, d):
                    if await hent_dag(http, db, d, omr):
                        log.info("hentet spotpriser for %s", d)
            await historikk(http, db, cfg, kilde, feilet)
            mangler_morgen = naa().hour >= 13 and not har_dag(db, idag + dt.timedelta(days=1))
            kilde.ok("venter på morgendagens priser" if mangler_morgen else "oppdatert")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kilde.feilet(e)
            log.warning("prisfeil: %s", e)
        await asyncio.sleep(900)
