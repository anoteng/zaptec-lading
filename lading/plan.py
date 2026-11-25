"""Ladeplan: hvilke timer bilen skal lade i vinduet fram til avreise.

Bilen melder ikke batterinivå. Antall ladetimer er enten et fast antall per døgn, eller regnes ut fra
batteriprosenten brukeren har lagt inn (se styring.batteribehov). Timene rangeres etter nettleiens
energiledd (pri 1) og deretter spotpris (pri 3, bra for nettet). Effekten i hver time styres av vakta.
"""
import datetime as dt

from . import tariff
from .db import DB
from .regning import spot_per_time
from .tid import OSLO, lokal


def vindu(cfg: dict, naa: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """(start, avreise) for ladevinduet som pågår eller kommer: fra vindu_fra dagen før til avreise."""
    sc = cfg["styring"]
    t, m = (int(x) for x in sc["avreise"].split(":"))
    avreise = naa.replace(hour=t, minute=m, second=0, microsecond=0)
    if avreise <= naa:
        avreise += dt.timedelta(days=1)
    start = dt.datetime.combine(avreise.date() - dt.timedelta(days=1), dt.time(sc["vindu_fra"]), OSLO)
    return start, avreise


def _blokker(timer: list[int]) -> int:
    timer = sorted(timer)
    return sum(1 for i, t in enumerate(timer) if i == 0 or t - timer[i - 1] > 3600)


def lag(db: DB, cfg: dict, naa: dt.datetime, antall: int = 0, behov: int | None = None) -> dict:
    """Velger de `antall` gunstigste timene i vinduet. Timer som allerede er ladet, teller med.

    Med `behov` gis antallet timer som trengs fra nå av i stedet (ladede timer kommer i tillegg).
    """
    sc = cfg["styring"]
    start, avreise = vindu(cfg, naa)
    s_ts, a_ts = int(start.timestamp()), int(avreise.timestamp())
    denne = int(naa.timestamp()) // 3600 * 3600
    spot = spot_per_time(db, s_ts, a_ts)
    kjent = list(spot.values())
    snitt = sum(kjent) / len(kjent) if kjent else None
    ladet = {r["time"]: r["kwh"] for r in db.alle("SELECT time, kwh FROM time_lader WHERE time >= ? AND time < ?",
                                                  s_ts, a_ts)}

    timer = []
    for t in range(s_ts, a_ts, 3600):
        energiledd, lav = tariff.energiledd(cfg, lokal(t))
        timer.append({"start": t, "energiledd": energiledd, "lavtariff": lav, "spot": spot.get(t),
                      "ladet_kwh": round(ladet.get(t, 0.0), 2), "forbi": t < denne, "valgt": False})

    brukt = sum(1 for x in timer if x["forbi"] and x["ladet_kwh"] >= sc["ladetime_min_kwh"])
    if behov is None:
        behov = max(0, antall - brukt)
    else:
        antall = brukt + behov
    kandidater = [x for x in timer if not x["forbi"]]
    nokkel = lambda x: (round(x["energiledd"], 4), x["spot"] if x["spot"] is not None else (snitt or 0.0), x["start"])
    valgt = sorted(kandidater, key=nokkel)[:behov]
    # Mange separate blokker gir mange stopp (laderen tåler få per økt). Da velges beste sammenhengende rekke.
    if behov and _blokker([x["start"] for x in valgt]) > sc["maks_blokker"] and len(kandidater) >= behov:
        kost = lambda rekke: sum(nokkel(x)[0] * 1000 + nokkel(x)[1] for x in rekke)
        valgt = min((kandidater[i:i + behov] for i in range(len(kandidater) - behov + 1)), key=kost)
    for x in valgt:
        x["valgt"] = True
    return {"start": s_ts, "avreise": a_ts, "antall": antall, "brukt": brukt, "behov": behov,
            "timer": timer, "valgte": sorted(x["start"] for x in valgt),
            "mangler_spot": sum(1 for x in kandidater if x["spot"] is None), "rekker_ikke": behov > len(kandidater)}
