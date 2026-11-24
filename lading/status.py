"""Regner ut status for websiden: nå, inneværende time, måned/kapasitetstrinn, priser og historikk."""
import datetime as dt
import math
import time

from . import klima, tariff, tuya
from .avtale import kwh_pris, ordning
from .db import DB
from .tid import dognstart, lokal, maanedstart, naa, time_av
from .tilstand import Tilstand

FERSK = 60  # sekunder en Pulse-måling regnes som fersk


def trinn_for(kw: float, trinn: list[dict]) -> dict:
    fra = 0.0
    for i, t in enumerate(trinn, 1):
        if kw < t["til"]:
            return {"nr": i, "fra": fra, "til": None if math.isinf(t["til"]) else t["til"], "kr": t["kr"]}
        fra = t["til"]
    raise ValueError(kw)


def _beste(r) -> tuple[float | None, str | None]:
    for felt in ("elvia", "maaler", "estimat"):
        if r[felt] is not None:
            return r[felt], felt
    return None, None


def timeserie(db: DB, fra: int, til: int) -> dict[int, dict]:
    ut: dict[int, dict] = {}
    for r in db.alle("SELECT * FROM time_hus WHERE time >= ? AND time < ?", fra, til):
        kwh, kilde = _beste(r)
        ut[r["time"]] = {"hus": kwh, "kilde": kilde, "estimat": r["estimat"], "maaler": r["maaler"],
                         "elvia": r["elvia"]}
    for r in db.alle("SELECT * FROM time_lader WHERE time >= ? AND time < ?", fra, til):
        ut.setdefault(r["time"], {"hus": None, "kilde": None})["lader"] = r["kwh"]
    for r in db.alle("SELECT time, SUM(kwh) kwh FROM time_vp WHERE time >= ? AND time < ? GROUP BY time", fra, til):
        ut.setdefault(r["time"], {"hus": None, "kilde": None})["vp"] = r["kwh"]
    return ut


def time_prognose(tilstand: Tilstand, naa_ts: float) -> dict | None:
    h = tilstand.hus
    if not h or naa_ts - h["mottatt"] > FERSK or h["time_kwh"] is None:
        return None
    t = time_av(naa_ts)
    if time_av(h["ts"]) != t:
        return None
    rest = max(0.0, t + 3600 - h["ts"])
    return {"time": t, "hittil": h["time_kwh"], "prognose": h["time_kwh"] + h["effekt_w"] / 1000 * rest / 3600,
            "rest_min": rest / 60}


def dognmaks(serie: dict[int, dict]) -> dict[dt.date, dict]:
    """Høyeste time per døgn: {dato: {dato, time, kwh, kilde}}."""
    dogn: dict[dt.date, dict] = {}
    for t, v in serie.items():
        if v.get("hus") is None:
            continue
        d = lokal(t).date()
        if d not in dogn or v["hus"] > dogn[d]["kwh"]:
            dogn[d] = {"dato": d.isoformat(), "time": lokal(t).hour, "kwh": round(v["hus"], 3), "kilde": v["kilde"]}
    return dogn


def topp3_snitt(kwh: list[float]) -> float:
    """Snittet av de tre høyeste døgnmaksene (Elvia deler alltid på tre)."""
    return sum(sorted(kwh, reverse=True)[:3]) / 3


def maaned(db: DB, cfg: dict, prognose: dict | None, idag: dt.date) -> dict:
    trinn = tariff.for_dato(cfg, idag)["trinn"]
    start = maanedstart(idag)
    neste = (idag.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    serie = timeserie(db, start, maanedstart(neste))
    if prognose:
        serie.setdefault(prognose["time"], {"hus": None, "kilde": None})
        if serie[prognose["time"]]["hus"] is None:
            serie[prognose["time"]]["hus"] = prognose["prognose"]
            serie[prognose["time"]]["kilde"] = "prognose"
    dogn = dognmaks(serie)
    rangert = sorted(dogn.values(), key=lambda x: -x["kwh"])
    topp3 = rangert[:3]
    snitt = sum(x["kwh"] for x in topp3) / 3 if topp3 else 0.0
    tr = trinn_for(snitt, trinn)
    # Tak for dagens høyeste time: hvor høyt kan dagens døgnmaks bli uten at snittet krysser trinnets øvre grense,
    # gitt toppene fra de andre dagene så langt. «Sikkert tak» holder uansett resten av måneden.
    andre = sorted((v["kwh"] for d, v in dogn.items() if d != idag), reverse=True) + [0.0, 0.0]
    tak = None
    if tr["til"] is not None:
        tak = {"grense": tr["til"], "sikkert": tr["til"], "gitt_toppene": 3 * tr["til"] - andre[0] - andre[1]}
    return {"topp3": topp3, "snitt": round(snitt, 3), "trinn": tr, "dager": len(dogn),
            "idag": dogn.get(idag), "tak_idag": tak,
            "dognmaks": sorted(dogn.values(), key=lambda x: x["dato"])}


def priser(db: DB, cfg: dict, fra: int, til: int) -> list[dict]:
    ut = []
    for r in db.alle("SELECT * FROM pris WHERE start >= ? AND start < ? ORDER BY start", fra, til):
        energiledd, lav = tariff.energiledd(cfg, lokal(r["start"]))
        kraft, stotte = kwh_pris(cfg, lokal(r["start"]), r["nok_kwh"])
        ut.append({"start": r["start"], "time": lokal(r["start"]).strftime("%H"), "dato": lokal(r["start"]).date().isoformat(),
                   "spot": round(r["nok_kwh"], 4), "lavtariff": lav, "energiledd": energiledd,
                   "din_pris": round(kraft - stotte + energiledd, 4)})
    return ut


def _kilder(tilstand: Tilstand) -> dict:
    return {k: {"sist_ok": v.sist_ok, "sist_feil": v.sist_feil, "feil": v.feil, "info": v.info}
            for k, v in tilstand.kilder.items()}


def bygg(db: DB, cfg: dict, tilstand: Tilstand) -> dict:
    na = naa()
    ts = time.time()
    idag = na.date()
    prognose = time_prognose(tilstand, ts)
    hus, lader = tilstand.hus, tilstand.lader
    lader_w = lader["effekt_w"] if lader else 0.0
    naa_info = None
    if hus:
        naa_info = {**hus, "fersk": ts - hus["mottatt"] < FERSK, "lader_w": lader_w,
              "grunnlast_w": max(0.0, hus["effekt_w"] - lader_w)}
    energiledd, lav = tariff.energiledd(cfg, na)
    t48 = time_av(ts) - 47 * 3600
    serie = timeserie(db, t48, time_av(ts) + 3600)
    if prognose:
        rad = serie.setdefault(prognose["time"], {"hus": None, "kilde": None})
        rad["hus"], rad["kilde"] = prognose["prognose"], "prognose"
    ute = klima.ute_per_time(db, t48, time_av(ts) + 3600)
    timer = []
    for t in range(t48, time_av(ts) + 3600, 3600):
        v = serie.get(t, {})
        timer.append({"start": t, "time": lokal(t).strftime("%H"), "hus": v.get("hus"), "lader": v.get("lader"), "vp": v.get("vp"), "ute": ute.get(t),
                      "kilde": v.get("kilde"),
                      "lavtariff": tariff.energiledd(cfg, lokal(t))[1]})
    return {
        "tid": na.isoformat(timespec="seconds"),
        "naa": naa_info,
        "sikring": cfg["sikring"],
        "time": prognose,
        "tariff_naa": {"lavtariff": lav, "energiledd": energiledd,
                       "ordning": ordning(cfg, idag)},
        "maaned": maaned(db, cfg, prognose, idag),
        "effektvarsel": tilstand.effektvarsel,
        "lader": lader,
        "installasjon": tilstand.installasjon,
        "priser": priser(db, cfg, dognstart(idag), dognstart(idag + dt.timedelta(days=2))),
        "timer": timer,
        "varmepumpe": tilstand.varmepumpe,
        "tuya": {**tilstand.tuya, "historikk": tuya.historikk(db, time_av(ts) + 3600)} if tilstand.tuya else None,
        "kilder": _kilder(tilstand),
    }
