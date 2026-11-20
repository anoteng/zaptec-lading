"""Strømregningen per måned: kraft, Norgespris/strømstønad og nettleie, med prognose for inneværende måned.

Strømavtale og støtteordning per periode ligger i avtale.py. Fortegn: «stotte» er det staten trekker fra
regningen; med Norgespris blir den negativ når spotprisen er lavere enn Norgesprisen.
"""
import dataclasses
import datetime as dt
import time

from . import status, tariff
from .avtale import ORDNINGER, avtale, kwh_pris, ordning
from .db import DB
from .tid import helligdager, lokal, maanedstart, naa, time_av
from .tilstand import Tilstand

PROFIL_DAGER = 28   # forbruksprofilen for prognosen bygger på så mange dager bakover
TOPP_DAGER = 14     # døgnmakser for resten av måneden anslås fra så mange dager bakover
CACHE_SEK = 600     # historikken (avsluttede måneder) regnes ut på nytt så ofte


def neste_maaned(d: dt.date) -> dt.date:
    return (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def spot_per_time(db: DB, fra: int, til: int) -> dict[int, float]:
    """Spotpris eks. mva per klokketime (tidsvektet snitt hvis prisene kommer per kvarter)."""
    sum_, vekt = {}, {}
    for r in db.alle("SELECT start, slutt, nok_kwh FROM pris WHERE start >= ? AND start < ?", fra, til):
        t, v = time_av(r["start"]), r["slutt"] - r["start"]
        sum_[t] = sum_.get(t, 0.0) + r["nok_kwh"] * v
        vekt[t] = vekt.get(t, 0) + v
    return {t: sum_[t] / vekt[t] for t in sum_}


def timer_i_maaned(d: dt.date) -> int:
    start = d.replace(day=1)
    return (maanedstart(neste_maaned(start)) - maanedstart(start)) // 3600


@dataclasses.dataclass
class Sum:
    """Løpende sum for en måned. Taket for støtte (5000 kWh) gjelder i rekkefølge gjennom måneden."""
    kwh: float = 0.0
    kwh_natt: float = 0.0
    kraft: float = 0.0
    stotte: float = 0.0
    energiledd: float = 0.0
    fastbelop: float = 0.0
    timer: int = 0
    uten_spot: int = 0

    def legg_til(self, cfg: dict, t: int, kwh: float, spot: float | None, hel_time: bool = True):
        rc, lt = cfg["regning"], lokal(t)
        if spot is None:
            self.uten_spot += 1  # fordelingen kraft/støtte blir usikker (totalen er riktig med Norgespris)
        kraft, stotte_kwh = kwh_pris(cfg, lt, spot)
        dekket = max(0.0, min(kwh, rc["stotte_maks_kwh"] - self.kwh))
        ledd, lav = tariff.energiledd(cfg, lt)
        av = avtale(cfg, lt.date())
        if hel_time:  # fastbeløpet fordeles på månedens timer, så avtalebytte midt i måneden blir riktig
            self.fastbelop += av["fastbelop"] / timer_i_maaned(lt.date())
        self.kwh += kwh
        self.kwh_natt += kwh if lav else 0.0
        self.kraft += kwh * kraft
        self.stotte += dekket * stotte_kwh
        self.energiledd += kwh * ledd
        self.timer += 1


def _resultat(cfg: dict, s: Sum, dogn_kwh: list[float], dato: dt.date, andel: float) -> dict:
    """Regningen ut fra summene. andel < 1 gir kapasitetsleddet forholdsmessig (for «hittil»)."""
    snitt = status.topp3_snitt(dogn_kwh)
    tr = status.trinn_for(snitt, tariff.for_dato(cfg, dato)["trinn"])
    kapasitet = tr["kr"] * andel
    fast = s.fastbelop
    total = s.kraft - s.stotte + s.energiledd + kapasitet + fast
    r2 = lambda x: round(x, 2)
    return {"kwh": round(s.kwh, 1), "kwh_natt": round(s.kwh_natt, 1),
            "kraft": r2(s.kraft), "stotte": r2(s.stotte), "kraft_netto": r2(s.kraft - s.stotte),
            "energiledd": r2(s.energiledd), "kapasitet": r2(kapasitet), "nettleie": r2(s.energiledd + kapasitet),
            "fastbelop": r2(fast), "sum": r2(total),
            "ore_per_kwh": round(total / s.kwh * 100, 1) if s.kwh else None,
            "trinn": tr["nr"], "trinn_kr": tr["kr"], "topp3_snitt": round(snitt, 2),
            "timer": s.timer, "uten_spot": s.uten_spot}


def _ordning(cfg: dict, d: dt.date) -> str:
    """Støtteordningen for måneden (den som gjaldt siste dag, så en ny ordning midt i måneden vises)."""
    return ordning(cfg, neste_maaned(d) - dt.timedelta(days=1))["ordning"]


def maaned_faktisk(db: DB, cfg: dict, start: dt.date) -> dict:
    """Regningen for en hel, avsluttet måned."""
    fra, til = maanedstart(start), maanedstart(neste_maaned(start))
    serie, spot = status.timeserie(db, fra, til), spot_per_time(db, fra, til)
    s = Sum()
    for t in sorted(serie):
        if serie[t]["hus"] is not None:
            s.legg_til(cfg, t, serie[t]["hus"], spot.get(t))
    dogn = [v["kwh"] for v in status.dognmaks(serie).values()]
    return {"maaned": start.strftime("%Y-%m"), "ordning": _ordning(cfg, start),
            "timer_mangler": (til - fra) // 3600 - s.timer, **_resultat(cfg, s, dogn, start, 1.0)}


def _fridag(d: dt.date) -> bool:
    return d.weekday() >= 5 or d in helligdager(d.year)


def _profil(serie: dict[int, dict]):
    """Snittforbruk per (fridag, klokketime), med reserve per klokketime og totalt."""
    grupper: dict = {}
    for t, v in serie.items():
        if v.get("hus") is None:
            continue
        lt = lokal(t)
        for nokkel in ((_fridag(lt.date()), lt.hour), lt.hour, "alle"):
            grupper.setdefault(nokkel, []).append(v["hus"])
    snitt = {k: sum(v) / len(v) for k, v in grupper.items()}

    def kwh(t: int) -> float:
        lt = lokal(t)
        for nokkel in ((_fridag(lt.date()), lt.hour), lt.hour, "alle"):
            if nokkel in snitt:
                return snitt[nokkel]
        return 0.0
    return kwh


def _kvantiler(utvalg: list[float], k: int) -> list[float]:
    """k jevnt fordelte verdier fra utvalget, så de anslåtte døgnmaksene får realistisk spredning."""
    u = sorted(utvalg)
    return [u[min(len(u) - 1, int((i + 0.5) / k * len(u)))] for i in range(k)] if u else []


def inneverende(db: DB, cfg: dict, tilstand: Tilstand) -> dict:
    """Hittil i måneden og prognose for hele måneden."""
    ts = time.time()
    idag = naa().date()
    start = idag.replace(day=1)
    fra, til, denne = maanedstart(start), maanedstart(neste_maaned(start)), time_av(ts)
    serie = status.timeserie(db, fra, til)
    spot = spot_per_time(db, fra, til)
    tp = status.time_prognose(tilstand, ts)

    hittil, mangler = Sum(), []
    for t in range(fra, denne, 3600):
        kwh = serie.get(t, {}).get("hus")
        if kwh is None:
            mangler.append(t)
        else:
            hittil.legg_til(cfg, t, kwh, spot.get(t))
    if tp:
        hittil.legg_til(cfg, denne, tp["hittil"], spot.get(denne), hel_time=False)
        hittil.timer -= 1  # timen er ikke ferdig

    # Prognose: faktiske timer + resten av denne timen + profil for manglende og kommende timer.
    hist_fra = denne - PROFIL_DAGER * 86400
    hist = status.timeserie(db, hist_fra, denne)
    profil = _profil(hist)
    # Timer uten publisert pris får månedens snittpris så langt (påvirker bare fordelingen kraft/støtte).
    spot_snitt = sum(spot.values()) / len(spot) if spot else None
    prog = dataclasses.replace(hittil)
    for t in mangler:
        prog.legg_til(cfg, t, profil(t), spot.get(t, spot_snitt))
    if tp:
        prog.legg_til(cfg, denne, max(0.0, tp["prognose"] - tp["hittil"]), spot.get(denne))
    else:
        prog.legg_til(cfg, denne, profil(denne), spot.get(denne, spot_snitt))
    for t in range(denne + 3600, til, 3600):
        prog.legg_til(cfg, t, profil(t), spot.get(t, spot_snitt))

    # Døgnmakser: faktiske så langt (med prognose for denne timen), anslag for dagene som gjenstår.
    if tp:
        rad = serie.setdefault(denne, {"hus": None, "kilde": None})
        if rad["hus"] is None:
            rad["hus"], rad["kilde"] = tp["prognose"], "prognose"
    dogn = {d: v["kwh"] for d, v in status.dognmaks(serie).items()}
    hist_dogn = [v["kwh"] for d, v in status.dognmaks(hist).items()
                 if d < idag and d >= idag - dt.timedelta(days=TOPP_DAGER)]
    gjenstaar = [idag + dt.timedelta(days=i) for i in range((neste_maaned(start) - idag).days)]
    anslag = _kvantiler(hist_dogn, len(gjenstaar))
    prog_dogn = {d: k for d, k in dogn.items() if d < idag}
    for d, a in zip(gjenstaar, sorted(anslag, reverse=True)):
        prog_dogn[d] = max(a, dogn.get(d, 0.0))

    andel = (ts - fra) / (til - fra)
    return {"maaned": start.strftime("%Y-%m"), "ordning": _ordning(cfg, start), "andel": round(andel, 4),
            "timer_mangler": len(mangler),
            "hittil": _resultat(cfg, hittil, list(dogn.values()), start, andel),
            "prognose": _resultat(cfg, prog, list(prog_dogn.values()), start, 1.0)}


_cache: dict = {"ts": 0.0, "nokkel": None, "data": None}


def historikk(db: DB, cfg: dict) -> list[dict]:
    """Avsluttede måneder fra starten av Elvia-historikken, eldste først (bufret)."""
    idag = naa().date()
    nokkel = idag.replace(day=1)
    if _cache["nokkel"] == nokkel and time.time() - _cache["ts"] < CACHE_SEK:
        return _cache["data"]
    d = dt.date.fromisoformat(cfg["elvia"]["fra"]).replace(day=1)
    ut = []
    while d < nokkel:
        ut.append(maaned_faktisk(db, cfg, d))
        d = neste_maaned(d)
    _cache.update(ts=time.time(), nokkel=nokkel, data=ut)
    return ut


def nullstill():
    """Tøm bufferen, f.eks. etter at strømavtalen er endret."""
    _cache.update(ts=0.0, nokkel=None, data=None)


def bygg(db: DB, cfg: dict, tilstand: Tilstand) -> dict:
    rc, idag = cfg["regning"], naa().date()
    return {"tid": naa().isoformat(timespec="seconds"),
            "inneverende": inneverende(db, cfg, tilstand),
            "historikk": historikk(db, cfg),
            "forutsetninger": {"avtale": avtale(cfg, idag), "ordning": ordning(cfg, idag),
                               "ordninger": rc["ordninger"], "ordningsnavn": ORDNINGER, "mva": rc["mva"],
                               "profil_dager": PROFIL_DAGER}}
