"""Tid, timer og norske helligdager."""
import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")


def naa() -> dt.datetime:
    return dt.datetime.now(OSLO)


def time_av(ts: float) -> int:
    """Epoch for starten av klokketimen ts ligger i (Oslo har hele timers forskyvning mot UTC)."""
    return int(ts) // 3600 * 3600


def lokal(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, OSLO)


def dognstart(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=OSLO).timestamp())


def maanedstart(d: dt.date) -> int:
    return dognstart(d.replace(day=1))


def paaskedag(aar: int) -> dt.date:
    """1. påskedag (gregoriansk, «anonym» algoritme)."""
    a = aar % 19
    b, c = divmod(aar, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mnd, dag = divmod(h + l - 7 * m + 114, 31)
    return dt.date(aar, mnd, dag + 1)


@lru_cache(maxsize=16)
def helligdager(aar: int) -> frozenset[dt.date]:
    p = paaskedag(aar)
    faste = [dt.date(aar, 1, 1), dt.date(aar, 5, 1), dt.date(aar, 5, 17), dt.date(aar, 12, 25), dt.date(aar, 12, 26)]
    bevegelige = [p + dt.timedelta(days=n) for n in (-3, -2, 0, 1, 39, 49, 50)]
    return frozenset(faste + bevegelige)


def er_lavtariff(t: dt.datetime, dag_fra: int, dag_til: int) -> bool:
    """Natt/helg i Elvias energiledd: helg, helligdag eller utenfor dag_fra–dag_til på hverdager."""
    t = t.astimezone(OSLO)
    if t.weekday() >= 5 or t.date() in helligdager(t.year):
        return True
    return not (dag_fra <= t.hour < dag_til)
