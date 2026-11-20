"""Hva elbilen koster i strøm: laderens energi per time priset med samme regler som strømregningen.

Energikostnaden er kraft + påslag − Norgespris/strømstøtte + energiledd for timene bilen lader.
Kapasitetsleddet regnes for hver måned som differansen mellom trinnet med og uten laderens forbruk.
Fastbeløpet i strømavtalen tas ikke med; det betales uansett.
"""
import datetime as dt
import time

from . import regning, status, tariff
from .db import DB
from .tid import lokal, maanedstart, naa
from .tilstand import Tilstand

MIN_KWH = 0.05      # timer med mindre enn dette regnes ikke som lading
OKT_PAUSE_T = 3     # timer uten lading før en ny økt starter


def _okt(cfg: dict, timer: list[tuple[int, float]], spot: dict[int, float]) -> dict:
    s = regning.Sum()
    for t, kwh in timer:
        s.legg_til(cfg, t, kwh, spot.get(t), hel_time=False)
    kost = s.kraft - s.stotte + s.energiledd
    return {"start": timer[0][0], "slutt": timer[-1][0] + 3600, "kwh": round(s.kwh, 2),
            "kraft_netto": round(s.kraft - s.stotte, 2), "energiledd": round(s.energiledd, 2),
            "sum": round(kost, 2), "ore_per_kwh": round(kost / s.kwh * 100, 1) if s.kwh else None,
            "andel_natt": round(s.kwh_natt / s.kwh, 3) if s.kwh else None}


def _kapasitet(db: DB, cfg: dict, start: dt.date, fra_lader: int) -> dict:
    """Kapasitetstrinnet i måneden med og uten laderens forbruk (bare timer etter at laderdata finnes)."""
    fra, til = maanedstart(start), maanedstart(regning.neste_maaned(start))
    serie = status.timeserie(db, fra, til)
    uten = {t: {**v, "hus": max(0.0, v["hus"] - (v.get("lader") or 0.0))} if v.get("hus") is not None else v
            for t, v in serie.items()}
    trinn = tariff.for_dato(cfg, start)["trinn"]
    med_tr = status.trinn_for(status.topp3_snitt([v["kwh"] for v in status.dognmaks(serie).values()]), trinn)
    uten_tr = status.trinn_for(status.topp3_snitt([v["kwh"] for v in status.dognmaks(uten).values()]), trinn)
    return {"maaned": start.strftime("%Y-%m"), "trinn_med": med_tr["nr"], "trinn_uten": uten_tr["nr"],
            "ekstra_kr": med_tr["kr"] - uten_tr["kr"], "delvis": fra < fra_lader}


def bygg(db: DB, cfg: dict, tilstand: Tilstand) -> dict:
    forste = db.en("SELECT MIN(time) t FROM time_lader")["t"]
    if forste is None:
        return {"fra": None, "okter": [], "maaneder": [], "totalt": None}
    rader = [(r["time"], r["kwh"]) for r in db.alle("SELECT time, kwh FROM time_lader WHERE kwh >= ? ORDER BY time",
                                                    MIN_KWH)]
    spot = regning.spot_per_time(db, forste, int(time.time()) + 2 * 86400)

    okter, naa_okt = [], []
    for t, kwh in rader:
        if naa_okt and t - naa_okt[-1][0] > OKT_PAUSE_T * 3600:
            okter.append(naa_okt)
            naa_okt = []
        naa_okt.append((t, kwh))
    if naa_okt:
        okter.append(naa_okt)
    lader = tilstand.lader or {}
    ut_okter = [_okt(cfg, o, spot) for o in okter]
    if ut_okter and lader.get("modus") == 3:
        ut_okter[-1]["pagar"] = True

    totalt = _okt(cfg, rader, spot) if rader else None
    maaneder = []
    d = lokal(forste).date().replace(day=1)
    while d <= naa().date():
        m_rader = [(t, k) for t, k in rader if lokal(t).date().replace(day=1) == d]
        m = _okt(cfg, m_rader, spot) if m_rader else {"kwh": 0.0, "sum": 0.0, "ore_per_kwh": None}
        kap = _kapasitet(db, cfg, d, forste)
        maaneder.append({**kap, "kwh": m["kwh"], "energi_kr": m["sum"], "sum": round(m["sum"] + kap["ekstra_kr"], 2),
                         "ore_per_kwh": m["ore_per_kwh"]})
        d = regning.neste_maaned(d)
    return {"fra": forste, "totalt": totalt, "kapasitet_ekstra": sum(m["ekstra_kr"] for m in maaneder),
            "okter": list(reversed(ut_okter)), "maaneder": maaneder}
