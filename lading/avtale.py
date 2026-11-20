"""Strømavtale (påslag og fastbeløp) og støtteordning (Norgespris, strømstønad eller ingen), per periode.

Standardverdiene står i config.toml under [regning]. Lagres de fra websiden, ligger de i databasen
(innstilling «stromavtale») og gjelder i stedet. Regningen regnes ut fra timeverdiene hver gang, så en
endring slår inn bakover i tid også.

Fortegn: støtte er det staten trekker fra regningen. Med Norgespris blir den negativ når spotprisen
(inkl. mva) er lavere enn Norgesprisen, for da betaler du differansen til staten.
"""
import datetime as dt
import math

from .db import DB

NOKKEL = "stromavtale"
ORDNINGER = {"norgespris": "Norgespris", "stromstotte": "Strømstønad", "ingen": "Ingen støtte"}


def _gjeldende(perioder: list[dict], d: dt.date) -> dict:
    """Perioden som gjaldt dato d. Før første periode brukes den første."""
    valgt = perioder[0]
    for p in perioder:
        if dt.date.fromisoformat(p["fra"]) <= d:
            valgt = p
    return valgt


def avtale(cfg: dict, d: dt.date) -> dict:
    return _gjeldende(cfg["regning"]["avtaler"], d)


def ordning(cfg: dict, d: dt.date) -> dict:
    return _gjeldende(cfg["regning"]["ordninger"], d)


def kwh_pris(cfg: dict, lt: dt.datetime, spot: float | None) -> tuple[float, float]:
    """(kraft, støtte) i kr per kWh inkl. mva for en klokketime, før månedstaket for støtte.

    Uten spotpris: med Norgespris blir totalen riktig (spot = Norgesprisen), ellers regnes spot som 0.
    """
    mva = 1 + cfg["regning"]["mva"]
    o, av = ordning(cfg, lt.date()), avtale(cfg, lt.date())
    if spot is None:
        spot_inkl = o["pris"] if o["ordning"] == "norgespris" else 0.0
    else:
        spot_inkl = spot * mva
    if o["ordning"] == "norgespris":
        stotte = spot_inkl - o["pris"]
    elif o["ordning"] == "stromstotte":
        stotte = o["grad"] * max(0.0, spot_inkl / mva - o["terskel"]) * mva
    else:
        stotte = 0.0
    return spot_inkl + av["paaslag"], stotte


# --- validering og lagring ---

def _tall(p: dict, felt: str, lav: float, hoy: float, navn: str) -> float:
    v = p.get(felt)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{navn} mangler eller er ikke et tall")
    if not lav <= v <= hoy:
        raise ValueError(f"{navn} må være mellom {lav} og {hoy}")
    return float(v)


def _perioder(liste, hva: str, maks: int) -> list[dict]:
    if not isinstance(liste, list) or not 1 <= len(liste) <= maks:
        raise ValueError(f"{hva}: må ha mellom 1 og {maks} perioder")
    sett = set()
    for p in liste:
        if not isinstance(p, dict):
            raise ValueError(f"{hva}: ugyldig rad")
        try:
            d = dt.date.fromisoformat(str(p.get("fra")))
        except ValueError:
            raise ValueError(f"{hva}: ugyldig dato {p.get('fra')!r}") from None
        if not dt.date(2000, 1, 1) <= d <= dt.date(2100, 1, 1):
            raise ValueError(f"{hva}: dato utenfor rimelig område")
        if d in sett:
            raise ValueError(f"{hva}: to perioder starter {d}")
        sett.add(d)
    return sorted(liste, key=lambda p: p["fra"])


def valider(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("forventet et objekt")
    avtaler = []
    for p in _perioder(data.get("avtaler"), "Strømavtale", 100):
        a = {"fra": dt.date.fromisoformat(str(p["fra"])).isoformat(),
             "paaslag": _tall(p, "paaslag", -2, 5, "Påslag"),
             "fastbelop": _tall(p, "fastbelop", 0, 5000, "Fastbeløp")}
        navn = str(p.get("navn") or "").strip()[:80]
        if navn:
            a["navn"] = navn
        avtaler.append(a)
    ordninger = []
    for p in _perioder(data.get("ordninger"), "Støtteordning", 50):
        o = {"fra": dt.date.fromisoformat(str(p["fra"])).isoformat(), "ordning": p.get("ordning")}
        if o["ordning"] == "norgespris":
            o["pris"] = _tall(p, "pris", 0, 5, "Norgespris")
        elif o["ordning"] == "stromstotte":
            o["terskel"] = _tall(p, "terskel", 0, 5, "Terskel for strømstønad")
            o["grad"] = _tall(p, "grad", 0, 1, "Dekningsgrad")
        elif o["ordning"] != "ingen":
            raise ValueError(f"ukjent støtteordning {o['ordning']!r}")
        ordninger.append(o)
    return {"avtaler": avtaler, "ordninger": ordninger}


def bruk(cfg: dict, db: DB):
    """Legger lagrede verdier fra websiden inn i cfg (ved oppstart og etter lagring)."""
    rc = cfg["regning"]
    if "_standard" not in rc:
        rc["_standard"] = valider({"avtaler": rc["avtaler"], "ordninger": rc["ordninger"]})
    lagret = db.hent(NOKKEL)
    v = lagret or rc["_standard"]
    rc["avtaler"], rc["ordninger"] = v["avtaler"], v["ordninger"]
    rc["_kilde"] = "web" if lagret else "config"


def hent(cfg: dict) -> dict:
    rc = cfg["regning"]
    return {"avtaler": rc["avtaler"], "ordninger": rc["ordninger"], "kilde": rc.get("_kilde", "config"),
            "ordningsnavn": ORDNINGER, "mva": rc["mva"]}


def lagre(cfg: dict, db: DB, data) -> dict:
    db.lagre(NOKKEL, valider(data))
    bruk(cfg, db)
    return hent(cfg)


def tilbakestill(cfg: dict, db: DB) -> dict:
    db.lagre(NOKKEL, None)
    bruk(cfg, db)
    return hent(cfg)
