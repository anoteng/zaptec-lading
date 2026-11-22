"""Temperatur og forbruk: utetemperatur per time, vedfyringsdager, graddager, temperaturmodell og værvarsel.

- Ute-, inne- og ønsket temperatur kommer fra varmepumpa (MELCloud, se varmepumpe.py) og lagres som timesnitt
  i time_klima. Innetemperaturen måles av pumpas IR-sensor (overflater i rommet), ikke lufta ved innedelen.
- Værvarsel per time hentes fra Open-Meteo (gratis, uten nøkkel) når [vaer] posisjon er satt, og brukes til å
  justere månedsprognosen for temperatur.
- Vedfyring kjennes igjen på at det er varmere inne enn pumpa er stilt på, selv om det er kaldt ute.
"""
import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

import aiohttp

from .db import DB
from .tid import dognstart, lokal
from .tilstand import Tilstand, sov_med_backoff

log = logging.getLogger("lading.klima")
GRADDAG_BASE = 17.0          # vanlig norsk basistemperatur for graddager
VED_OVER_SATT = 2.0          # inne minst så mye over ønsket temperatur …
VED_UTE_MAKS = 8.0           # … mens det er så kaldt ute, regnes som fyring med ved
VED_TIMER = 3                # så mange slike timer gjør døgnet til en vedfyringsdag
MIN_TIMER_DOGN = 20          # døgn med færre timer med temperatur tas ikke med
VAER_SEK = 3 * 3600
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


# --- timesnitt fra punktserier ---
def timesnitt(punkter: list[tuple[float, float]], fra: int, til: int, min_dekning: float = 0.5) -> dict[int, float]:
    """Tidsvektet snitt per klokketime av en trappefunksjon (verdien gjelder til neste punkt)."""
    p = sorted(punkter)
    ut: dict[int, float] = {}
    for h in range(fra, til, 3600):
        sum_v = dekket = 0.0
        for i, (t, v) in enumerate(p):
            neste = p[i + 1][0] if i + 1 < len(p) else max(t, min(h + 3600, time.time()))
            a, b = max(t, h), min(neste, h + 3600)
            if b > a:
                sum_v += v * (b - a)
                dekket += b - a
        if dekket >= 3600 * min_dekning:
            ut[h] = round(sum_v / dekket, 2)
    return ut


def lagre_trend(db: DB, data: list | dict, sone: str, fra: int, til: int) -> int:
    """Lagre timesnitt fra MELClouds trendsummary (Hourly). Tidsstemplene er lokal tid for enheten."""
    top = data[0] if isinstance(data, list) and data else data
    if not isinstance(top, dict):
        return 0
    tz = ZoneInfo(sone)
    serier = {}
    for ds in top.get("datasets", []):
        navn = str(ds.get("label", "")).rsplit(".", 1)[-1]
        felt = {"OUTDOOR_TEMPERATURE": "ute", "ROOM_TEMPERATURE": "inne", "SET_TEMPERATURE": "satt"}.get(navn)
        if not felt:
            continue
        pkt = []
        for x in ds.get("data", []):
            try:
                t = dt.datetime.fromisoformat(str(x["x"])[:19]).replace(tzinfo=tz).timestamp()
                pkt.append((t, float(x["y"])))
            except (KeyError, TypeError, ValueError):
                continue
        serier[felt] = timesnitt(pkt, fra, til)
    timer = sorted(set().union(*serier.values())) if serier else []
    for h in timer:
        v = {f: serier.get(f, {}).get(h) for f in ("ute", "inne", "satt")}
        db.x("INSERT INTO time_klima VALUES (?, ?, ?, ?) ON CONFLICT(time) DO UPDATE SET "
             "ute = COALESCE(excluded.ute, ute), inne = COALESCE(excluded.inne, inne), satt = COALESCE(excluded.satt, satt)",
             h, v["ute"], v["inne"], v["satt"])
    return sum(1 for h in timer if serier.get("ute", {}).get(h) is not None)


# --- analyse ---
def ute_per_time(db: DB, fra: int, til: int, varsel: bool = False) -> dict[int, float]:
    """Utetemperatur per time: målt fra varmepumpa, og (valgfritt) værvarsel for timer uten måling."""
    ut = {}
    if varsel:
        ut.update({r["time"]: r["ute"] for r in db.alle("SELECT time, ute FROM vaer_varsel WHERE time >= ? AND time < ?", fra, til)
                   if r["ute"] is not None})
    ut.update({r["time"]: r["ute"] for r in db.alle("SELECT time, ute FROM time_klima WHERE time >= ? AND time < ?", fra, til)
               if r["ute"] is not None})
    return ut


def graddager(db: DB, fra: int, til: int) -> dict:
    """Graddager (basis 17 °C) og snittemperatur for perioden, med hvor stor del av timene som har måling."""
    ute = ute_per_time(db, fra, til)
    timer = (til - fra) // 3600
    if not ute:
        return {"graddager": None, "ute_snitt": None, "dekning": 0.0}
    dekning = len(ute) / timer
    gd = sum(max(0.0, GRADDAG_BASE - v) for v in ute.values()) / 24 / dekning   # skalert opp for hull
    return {"graddager": round(gd, 1), "ute_snitt": round(sum(ute.values()) / len(ute), 1), "dekning": round(dekning, 3)}


def dogn(db: DB, serie: dict[int, dict]) -> list[dict]:
    """Døgn med hele forbruksdøgnet og nok temperaturtimer: forbruk, utetemperatur og om det ble fyrt med ved."""
    if not serie:
        return []
    fra, til = min(serie), max(serie) + 3600
    klima = {r["time"]: r for r in db.alle("SELECT * FROM time_klima WHERE time >= ? AND time < ?", fra, til)}
    per_dag: dict[dt.date, dict] = {}
    for t, v in serie.items():
        d = per_dag.setdefault(lokal(t).date(), {"timer": 0, "hus": 0.0, "lader": 0.0, "vp": 0.0, "ute": [], "inne": [], "ved": 0})
        if v.get("hus") is not None and v.get("kilde") != "prognose":
            d["timer"] += 1
            d["hus"] += v["hus"]
            d["lader"] += v.get("lader") or 0.0
            d["vp"] += v.get("vp") or 0.0
        k = klima.get(t)
        if k and k["ute"] is not None:
            d["ute"].append(k["ute"])
            if k["inne"] is not None:
                d["inne"].append(k["inne"])
            if k["inne"] is not None and k["satt"] is not None and k["inne"] >= k["satt"] + VED_OVER_SATT \
                    and k["ute"] <= VED_UTE_MAKS:
                d["ved"] += 1
    ut = []
    for dato, d in sorted(per_dag.items()):
        timer_i_dogn = (dognstart(dato + dt.timedelta(days=1)) - dognstart(dato)) // 3600
        if d["timer"] < timer_i_dogn or len(d["ute"]) < MIN_TIMER_DOGN:
            continue
        ut.append({"dato": dato.isoformat(), "ute": round(sum(d["ute"]) / len(d["ute"]), 1),
                   "inne": round(sum(d["inne"]) / len(d["inne"]), 1) if d["inne"] else None,
                   "hus": round(d["hus"], 1), "lader": round(d["lader"], 1), "vp": round(d["vp"], 1),
                   "uten_bil": round(d["hus"] - d["lader"], 1), "ved": d["ved"] >= VED_TIMER, "ved_timer": d["ved"]})
    return ut


def modell(dager: list[dict]) -> dict | None:
    """Lineær sammenheng mellom døgnforbruk uten elbil og utetemperatur: kWh = a + b·ute. None med for lite data."""
    pkt = [(d["ute"], d["uten_bil"]) for d in dager]
    if len(pkt) < 10:
        return None
    xs, ys = [x for x, _ in pkt], [y for _, y in pkt]
    if max(xs) - min(xs) < 4:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in pkt) / sxx
    a = my - b * mx
    sst = sum((y - my) ** 2 for y in ys)
    r2 = 1 - sum((y - (a + b * x)) ** 2 for x, y in pkt) / sst if sst else 0.0
    return {"a": round(a, 2), "b": round(b, 3), "r2": round(r2, 2), "n": len(pkt),
            "ute_min": min(xs), "ute_maks": max(xs), "brukes": b < 0 and r2 >= 0.15}


# --- værvarsel ---
async def hent_varsel(db: DB, http: aiohttp.ClientSession, posisjon: str) -> int:
    bredde, lengde = (float(x) for x in posisjon.split(","))
    params = {"latitude": f"{bredde:.2f}", "longitude": f"{lengde:.2f}", "hourly": "temperature_2m",
              "forecast_days": "16", "timezone": "GMT", "timeformat": "unixtime"}
    async with http.get(OPEN_METEO, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        d = await r.json()
    tider, temp = d["hourly"]["time"], d["hourly"]["temperature_2m"]
    naa = int(time.time())
    for t, v in zip(tider, temp):
        if v is not None:
            db.x("INSERT OR REPLACE INTO vaer_varsel VALUES (?, ?, ?)", int(t), float(v), naa)
    db.x("DELETE FROM vaer_varsel WHERE time < ?", naa - 7 * 86400)
    return len(tider)


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
    kilde = tilstand.kilder["vaer"]
    posisjon = str(cfg.get("vaer", {}).get("posisjon") or "").strip()
    if not posisjon:
        kilde.info = "ikke satt opp (LADING_VAER_POSISJON)"
        await asyncio.Event().wait()
    try:
        b, l = (float(x) for x in posisjon.split(","))
        assert -90 <= b <= 90 and -180 <= l <= 180
    except (ValueError, AssertionError):
        kilde.feilet("LADING_VAER_POSISJON må være «bredde,lengde», f.eks. 59.9,10.8")
        await asyncio.Event().wait()
    forsok = 0
    while True:
        try:
            n = await hent_varsel(db, http, posisjon)
            kilde.ok(f"Open-Meteo, {n // 24} døgn varsel")
            forsok = 0
            await asyncio.sleep(VAER_SEK)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("værvarsel: %s", e)
            kilde.feilet(e)
            forsok += 1
            await sov_med_backoff(forsok, 60, 3600)
