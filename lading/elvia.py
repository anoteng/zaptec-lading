"""Elvia målerverdier (offisielle timeverdier, kommer med forsinkelse). API-et har en stram kvote."""
import asyncio
import datetime as dt
import logging

import aiohttp

from .db import DB
from .tid import OSLO, naa
from .tilstand import Tilstand

log = logging.getLogger("lading.elvia")
URL = "https://elvia.azure-api.net/customer/metervalues/api/v1/metervalues"


class Kvote(Exception):
    pass


async def hent(http: aiohttp.ClientSession, token: str, db: DB, fra: dt.datetime, til: dt.datetime) -> int:
    params = {"startTime": fra.isoformat(), "endTime": til.isoformat()}
    async with http.get(URL, params=params, headers={"Authorization": f"Bearer {token}"}) as r:
        if r.status in (403, 429):
            raise Kvote(f"HTTP {r.status}: {(await r.text())[:120]}")
        r.raise_for_status()
        d = await r.json()
    n = 0
    for mp in d.get("meteringpoints", []):
        for v in mp["metervalue"]["timeSeries"]:
            if v.get("production"):
                continue
            ts = int(dt.datetime.fromisoformat(v["startTime"]).timestamp())
            db.sett_time_hus(ts, "elvia", v["value"], int(bool(v.get("verified"))))
            n += 1
    return n


def _siste_elvia(db: DB) -> int | None:
    r = db.en("SELECT MAX(time) t FROM time_hus WHERE elvia IS NOT NULL")
    return r["t"] if r else None


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
    token = cfg["hemmelig"].get("ELVIA_METERVALUES_TOKEN")
    ec = cfg["elvia"]
    kilde = tilstand.kilder["elvia"]
    if not token:
        kilde.info = "ikke satt opp (ELVIA_METERVALUES_TOKEN mangler)"
        log.info("Elvia-token mangler: henter ikke timeverdier fra nettselskapet")
        await asyncio.Event().wait()
    start = dt.datetime.fromisoformat(ec["fra"]).replace(tzinfo=OSLO)
    pause = ec.get("backfill_pause_min", 5) * 60
    sist_daglig: dt.date | None = None
    while True:
        try:
            na = naa()
            midnatt = na.replace(hour=0, minute=0, second=0, microsecond=0)
            siste = _siste_elvia(db)
            fra = start if siste is None else dt.datetime.fromtimestamp(siste + 3600, OSLO)
            if fra < midnatt - dt.timedelta(days=2):
                # Historikk: én måned per kall, med pause mellom for å skåne kvoten.
                til = min(fra + dt.timedelta(days=31), midnatt)
                n = await hent(http, token, db, fra, til)
                log.info("Elvia historikk %s–%s: %d timer", fra.date(), til.date(), n)
                kilde.ok(f"henter historikk, har til {til.date()}")
                if n == 0:
                    start = til  # ingen data i perioden, hopp videre
                await asyncio.sleep(pause)
                continue
            if sist_daglig != na.date() and na.hour >= ec.get("hent_kl", 10):
                # Daglig: hent de siste tre døgnene på nytt (verdier kan bli verifisert/korrigert).
                n = await hent(http, token, db, midnatt - dt.timedelta(days=3), midnatt)
                sist_daglig = na.date()
                kilde.ok(f"daglig henting: {n} timer")
                log.info("Elvia daglig: %d timer", n)
            elif kilde.sist_ok is None:
                kilde.ok("à jour")
            await asyncio.sleep(600)
        except asyncio.CancelledError:
            raise
        except Kvote as e:
            kilde.feilet(f"kvote brukt opp, prøver igjen om en time ({e})")
            log.warning("Elvia-kvote: %s", e)
            await asyncio.sleep(3600)
        except Exception as e:
            kilde.feilet(e)
            log.warning("Elvia-feil: %s", e)
            await asyncio.sleep(1800)
