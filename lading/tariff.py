"""Nettleietariff som gjaldt på en gitt dato (Elvia endrer satsene et par ganger i året)."""
import datetime as dt

from .tid import er_lavtariff

_cache: dict[int, list[tuple[dt.date, dict]]] = {}


def _perioder(tc: dict) -> list[tuple[dt.date, dict]]:
    """Periodene sortert etter startdato, med dag/natt-grenser og arvede trinn fylt inn."""
    if id(tc) not in _cache:
        ut, trinn = [], None
        for p in sorted(tc["perioder"], key=lambda p: p["fra"]):
            trinn = p.get("trinn", trinn)
            if trinn is None:
                raise ValueError(f"tariffperioden fra {p['fra']} mangler trinn")
            ut.append((dt.date.fromisoformat(p["fra"]),
                       {**p, "trinn": trinn, "dag_fra": tc["dag_fra"], "dag_til": tc["dag_til"]}))
        _cache[id(tc)] = ut
    return _cache[id(tc)]


def for_dato(cfg: dict, d: dt.date) -> dict:
    """Satsene som gjaldt dato d. Før første periode brukes den første."""
    perioder = _perioder(cfg["tariff"])
    valgt = perioder[0][1]
    for fra, p in perioder:
        if fra <= d:
            valgt = p
    return valgt


def energiledd(cfg: dict, t: dt.datetime) -> tuple[float, bool]:
    """(kr/kWh, lavtariff) for det lokale tidspunktet t."""
    tf = for_dato(cfg, t.date())
    lav = er_lavtariff(t, tf["dag_fra"], tf["dag_til"])
    return (tf["energiledd_natt"] if lav else tf["energiledd_dag"]), lav
