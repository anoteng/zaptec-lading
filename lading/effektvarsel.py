"""Effektvarsel: si fra mens timen pågår hvis forbruket (utenom elbilen) ligger an til å øke kapasitetstrinnet.

Grensen er hvor mange kWh denne klokketimen kan bli før snittet av månedens tre høyeste døgnmakser går over
trinnets øvre grense. De andre dagene er de faktiske døgnmaksene så langt, pluss et anslag for dagene som
gjenstår (fra de siste 14 dagene), så grensen ikke blir for romslig tidlig i måneden.

Prognosen for timen antar at elbilen står i pause resten av timen (den tar effektvakta seg av), med mindre
«Lad nå» er på. Husets effekt er et snitt over de siste fem minuttene, så korte topper ikke gir falsk alarm.

Nivåer: «varsel» (trinnet øker hvis ingenting gjøres, push-varsel) og «forvarsel» (timen går over den
sikre grensen og spiser av marginen for resten av måneden, bare på statussiden).
"""
import asyncio
import datetime as dt
import logging
import time

from . import status, styring, tariff, varsel
from .db import DB
from .regning import _kvantiler, neste_maaned
from .tid import lokal, maanedstart, time_av
from .tilstand import Tilstand

log = logging.getLogger("lading.effektvarsel")
SJEKK_SEK = 30
SNITT_SEK = 300
HIST_DAGER = 14
MIN_REST_MIN = 3          # mindre tid igjen enn dette: for sent å gjøre noe, ikke push
NYTT_VARSEL_KW = 1.0      # nytt varsel samme time bare hvis det må kuttes så mye mer
NYTT_VARSEL_MIN = 10


def _tall(x: float, d: int = 1) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def grenser(db: DB, cfg: dict, naa_ts: float) -> dict | None:
    """Sikker grense og grensen for at trinnet øker, for denne klokketimen. None i øverste trinn."""
    denne = time_av(naa_ts)
    idag = lokal(naa_ts).date()
    start = idag.replace(day=1)
    serie = status.timeserie(db, maanedstart(start), maanedstart(neste_maaned(start)))
    serie.pop(denne, None)                       # denne timen vurderes for seg
    dogn = status.dognmaks(serie)
    idag_hittil = dogn.pop(idag, {}).get("kwh", 0.0)
    andre = [v["kwh"] for v in dogn.values()]
    hist = status.dognmaks(status.timeserie(db, denne - HIST_DAGER * 86400, denne))
    hist_kwh = [v["kwh"] for d, v in hist.items() if d < idag]
    andre += _kvantiler(hist_kwh, (neste_maaned(start) - idag).days - 1)   # anslag for dagene etter i dag
    trinn = tariff.for_dato(cfg, idag)["trinn"]
    naa_tr = status.trinn_for(status.topp3_snitt(andre + [idag_hittil]), trinn)
    if naa_tr["til"] is None:
        return None
    b = sorted(andre, reverse=True) + [0.0, 0.0]
    return {"sikker": naa_tr["til"], "grense": round(3 * naa_tr["til"] - b[0] - b[1], 2), "trinn": naa_tr,
            "andre": andre, "idag_hittil": idag_hittil, "tabell": trinn}


class Effektvarsel:
    def __init__(self, cfg: dict, db: DB, tilstand: Tilstand):
        self.cfg, self.db, self.tilstand = cfg, db, tilstand
        self.vc = cfg.get("varsel", {})
        self.margin = cfg["styring"]["margin_kwh"]
        self.varslet: dict | None = None    # {"time", "kutt_kw", "ts", "grense", "trinn"} for siste push

    def _snitt(self, tabell: str, fra: float) -> float | None:
        r = self.db.en(f"SELECT AVG(effekt_w) e, COUNT(*) n FROM {tabell} WHERE ts >= ?", int(fra))
        return r["e"] if r and r["n"] else None

    def vurder(self, naa_ts: float) -> dict | None:
        """Ren beregning: nivå, prognose og hvor mye som må kuttes. None uten ferske data."""
        h, lader = self.tilstand.hus, self.tilstand.lader or {}
        denne = time_av(naa_ts)
        if not h or naa_ts - h["mottatt"] > status.FERSK or h.get("time_kwh") is None or time_av(h["ts"]) != denne:
            return None
        g = grenser(self.db, self.cfg, naa_ts)
        if g is None:
            return None
        rest_h = max(0.0, denne + 3600 - h["ts"]) / 3600
        hus_w = self._snitt("maling", naa_ts - SNITT_SEK) or h["effekt_w"]
        lader_w = self._snitt("lader", naa_ts - SNITT_SEK)
        if lader_w is None:
            lader_w = lader.get("effekt_w") or 0.0
        o = styring.overstyring(self.db, naa_ts)
        lad_naa = bool(o and o["type"] == "lad_naa")
        # Med «lad nå» styrer ikke effektvakta bilen, så da regnes den med.
        last_kw = max(0.0, hus_w - (0.0 if lad_naa else lader_w)) / 1000
        prognose = h["time_kwh"] + last_kw * rest_h
        grense = g["grense"] - self.margin
        sikker = g["sikker"] - self.margin
        niva = "varsel" if prognose > grense else "forvarsel" if prognose > sikker else "ok"
        kutt_kw = max(0.0, (prognose - grense) / rest_h) if rest_h > 0 and niva == "varsel" else 0.0
        # Trinnet timen gir hvis den går over grensen (også når prognosen ligger i sikkerhetsmarginen under).
        over = max(g["idag_hittil"], prognose, g["grense"] + 0.01)
        ny = status.trinn_for(status.topp3_snitt(g["andre"] + [over]), g["tabell"])
        return {"ts": naa_ts, "time": denne, "niva": niva, "hittil": round(h["time_kwh"], 3),
                "prognose": round(prognose, 2), "grense": round(g["grense"], 2), "sikker": g["sikker"],
                "last_kw": round(last_kw, 2), "lader_kw": round(lader_w / 1000, 2), "lad_naa": lad_naa,
                "rest_min": round(rest_h * 60, 1), "kutt_kw": round(kutt_kw, 2),
                "kan_rekkes": niva == "varsel" and rest_h * 60 >= MIN_REST_MIN and kutt_kw <= last_kw,
                "trinn": g["trinn"]["nr"], "trinn_kr": g["trinn"]["kr"],
                "nytt_trinn": ny["nr"], "ekstra_kr": max(0, ny["kr"] - g["trinn"]["kr"])}

    def _tekst(self, v: dict) -> tuple[str, str]:
        tittel = f"Strøm: kutt ca. {_tall(v['kutt_kw'])} kW de neste {v['rest_min']:.0f} min"
        tekst = (f"Timen ligger an til {_tall(v['prognose'])} kWh. Grensen er {_tall(v['grense'])} kWh; "
                 f"over den øker nettleien til trinn {v['nytt_trinn']}"
                 + (f" (+{v['ekstra_kr']:.0f} kr/mnd)" if v["ekstra_kr"] else "") + ". "
                 f"Huset bruker nå {_tall(v['last_kw'])} kW")
        if v["lad_naa"]:
            tekst += f", med elbilen på «Lad nå» ({_tall(v['lader_kw'])} kW). Trykk «Automatisk» for å la effektvakta styre bilen."
        elif v["lader_kw"] > 0.1:
            tekst += f" utenom elbilen. Elbilen ({_tall(v['lader_kw'])} kW) styres ned automatisk."
        else:
            tekst += "."
        return tittel, tekst

    async def _oppfolging(self, naa_ts: float):
        """Etter en time det ble varslet for: fortell hvordan det gikk."""
        v = self.varslet
        if not v or time_av(naa_ts) == v["time"] or naa_ts - time_av(naa_ts) < 90:
            return
        self.varslet = None
        kwh = status.timeserie(self.db, v["time"], v["time"] + 3600).get(v["time"], {}).get("hus")
        if kwh is None:
            return
        kl = lokal(v["time"]).strftime("%H")
        if kwh <= v["grense"]:
            await varsel.send("Strøm: OK, holdt", f"Timen kl. {kl} endte på {_tall(kwh)} kWh, under grensen på "
                              f"{_tall(v['grense'])} kWh. Kapasitetstrinnet er uendret.", "low", "white_check_mark")
        else:
            await varsel.send("Strøm: timen gikk over grensen",
                              f"Timen kl. {kl} endte på {_tall(kwh)} kWh, over grensen på {_tall(v['grense'])} kWh. "
                              f"Kapasitetstrinnet ligger nå an til å øke.", "default", "warning")

    async def steg(self):
        naa_ts = time.time()
        await self._oppfolging(naa_ts)
        v = self.vurder(naa_ts)
        self.tilstand.effektvarsel = v
        if not v or not v["kan_rekkes"] or not self.vc.get("effekt", True):
            return
        if (naa_ts - v["time"]) / 60 < self.vc.get("effekt_fra_min", 5):
            return
        s = self.varslet
        if s and s["time"] == v["time"] and not (
                v["kutt_kw"] >= s["kutt_kw"] + NYTT_VARSEL_KW and naa_ts - s["ts"] >= NYTT_VARSEL_MIN * 60):
            return
        tittel, tekst = self._tekst(v)
        await varsel.send(tittel, tekst, "high", "warning,zap")
        self.varslet = {"time": v["time"], "kutt_kw": v["kutt_kw"], "ts": naa_ts, "grense": v["grense"]}


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, _http=None):
    e = Effektvarsel(cfg, db, tilstand)
    await asyncio.sleep(30)  # la Pulse og Zaptec levere første data
    while True:
        try:
            await e.steg()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.exception("effektvarsel feilet: %s", ex)
        await asyncio.sleep(SJEKK_SEK)
