"""Styring av ladingen: plan (hvilke timer), effektvakt (hvor mye strøm) og utførelse mot Zaptec.

Modus i config.toml [styring]:
  av      – styringen kjører ikke
  dryrun  – regner og logger hva den ville gjort, sender ingen kommandoer
  aktiv   – sender kommandoer til laderen (pause/gjenoppta og tilgjengelig strøm)

Overstyring fra websiden (lagres i databasen): auto, lad_naa, pause, timer (N timer i dette vinduet).
"""
import asyncio
import datetime as dt
import json
import logging
import math
import time

import aiohttp

from . import plan, status, tariff, varsel
from .db import DB
from .tid import lokal, maanedstart, naa, time_av
from .tilstand import Tilstand
from .zaptec import Zaptec

log = logging.getLogger("lading.styring")
PAUSE, GJENOPPTA = 506, 507
TICK = 15
OVERSTYRINGER = ("auto", "lad_naa", "pause", "timer")


def overstyring(db: DB, naa_ts: float) -> dict | None:
    o = db.hent("overstyring")
    if o and o.get("til") and naa_ts >= o["til"]:
        db.lagre("overstyring", None)
        return None
    return o


def sett_overstyring(db: DB, cfg: dict, type_: str, timer: int | None = None) -> dict | None:
    if type_ not in OVERSTYRINGER:
        raise ValueError(type_)
    if type_ == "auto":
        db.lagre("overstyring", None)
        return None
    naa_dt = naa()
    _, avreise = plan.vindu(cfg, naa_dt)
    til = avreise.timestamp()
    if type_ == "lad_naa":
        til = naa_dt.timestamp() + cfg["styring"]["lad_naa_timer"] * 3600
    o = {"type": type_, "satt": naa_dt.timestamp(), "til": til}
    if type_ == "timer":
        if timer is None or not 0 <= timer <= 24:
            raise ValueError("timer må være 0–24")
        o["timer"] = int(timer)
    db.lagre("overstyring", o)
    return o


def timetak(db: DB, cfg: dict, tilstand: Tilstand, naa_ts: float) -> dict:
    """Grensen for kWh i denne klokketimen så månedens kapasitetstrinn ikke øker.

    Tidlig i måneden er topp-3 basert på få dager, så trinnet fra forrige måned brukes som gulv.
    """
    sc = cfg["styring"]
    idag = lokal(naa_ts).date()
    m = status.maaned(db, cfg, status.time_prognose(tilstand, naa_ts), idag)
    grense, kilde = m["trinn"]["til"], "denne måneden"
    if idag.day <= sc["tak_tidlig_dager"]:
        forrige = (idag.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        serie = status.timeserie(db, maanedstart(forrige), maanedstart(idag.replace(day=1)))
        snitt = status.topp3_snitt([v["kwh"] for v in status.dognmaks(serie).values()])
        tr = status.trinn_for(snitt, tariff.for_dato(cfg, idag)["trinn"])
        if grense is not None and (tr["til"] is None or tr["til"] > grense):
            grense, kilde = tr["til"], "forrige måned (tidlig i måneden)"
    if grense is None:
        return {"kwh": None, "trinn_til": None, "kilde": kilde}
    return {"kwh": round(grense - sc["margin_kwh"], 2), "trinn_til": grense, "kilde": kilde}


class Styring:
    def __init__(self, cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
        self.cfg, self.sc, self.db, self.tilstand = cfg, cfg["styring"], db, tilstand
        self.aktiv = self.sc["modus"] == "aktiv"
        self.z = Zaptec(http, cfg["hemmelig"]["ZAPTEC_USER"], cfg["hemmelig"]["ZAPTEC_PASSWORD"])
        self.satt_strom: float | None = None     # det vi sist satte (aktiv modus)
        self.sist_strom = 0.0
        self.sist_pause = 0.0
        self.stopp_i_okt = 0
        self.var_tilkoblet = False
        self.gjenopptak_uten_lading = 0
        self.sist_logget: tuple | None = None

    # --- beslutning ---
    def onske(self, naa_ts: float) -> dict:
        """Hva vi vil: lade eller ikke, og med hvilken strøm. Ren beregning uten sideeffekter."""
        sc, t = self.sc, self.tilstand
        o = overstyring(self.db, naa_ts)
        antall = o["timer"] if o and o["type"] == "timer" else sc["timer_per_dogn"]
        p = plan.lag(self.db, self.cfg, lokal(naa_ts), antall)
        denne = time_av(naa_ts)
        tak = timetak(self.db, self.cfg, t, naa_ts)
        ut = {"ts": naa_ts, "overstyring": o, "plan": p, "tak": tak, "lade": False, "strom": None, "grunn": ""}

        if o and o["type"] == "pause":
            ut["grunn"] = "pauset manuelt"
            return ut
        lad_naa = bool(o and o["type"] == "lad_naa")
        if not lad_naa and denne not in p["valgte"]:
            nest = next((s for s in p["valgte"] if s > denne), None)
            ut["grunn"] = f"venter til {lokal(nest):%H:%M}" if nest else "ingen flere ladetimer før avreise"
            return ut

        # Effektvakt: hvor mye kan laderen bruke resten av timen uten å bryte taket?
        hus, lader = t.hus, t.lader or {}
        maks = min(sc["maks_strom"], lader.get("maks_strom") or sc["maks_strom"])
        fersk = hus and naa_ts - hus["mottatt"] < 60 and hus.get("time_kwh") is not None \
            and time_av(hus["ts"]) == denne
        if lad_naa:
            strom, grunn = maks, "lad nå (full effekt)"
        elif not fersk:
            strom, grunn = sc["trygg_strom"], "mangler ferske data fra Pulse, lader forsiktig"
        elif tak["kwh"] is None:
            strom, grunn = maks, "ingen trinngrense"
        else:
            rest_h = max(300.0, denne + 3600 - hus["ts"]) / 3600
            grunnlast_kw = max(0.0, hus["effekt_w"] - (lader.get("effekt_w") or 0.0)) / 1000
            budsjett = tak["kwh"] - hus["time_kwh"] - grunnlast_kw * rest_h
            strom = math.floor(max(0.0, budsjett / rest_h) * 1000 / sc["volt"])
            grunn = (f"timetak {tak['kwh']:.1f} kWh: brukt {hus['time_kwh']:.2f}, "
                     f"huset ellers {grunnlast_kw:.1f} kW, {rest_h * 60:.0f} min igjen")
        # Fasevakt: hold fasestrømmen under varselgrensen.
        if hus and fersk:
            fase = max(x or 0.0 for x in (hus.get("i1"), hus.get("i3")))
            over = fase - self.cfg["sikring"]["varsel_a"]
            if over > 0:
                naa_strom = self.satt_strom or lader.get("strom_satt") or maks
                strom = min(strom, math.floor(naa_strom - over - 2))
                grunn += f"; fasevakt ({fase:.0f} A)"
                ut["fasevakt"] = True
        strom = min(maks, strom)
        if strom < sc["min_strom"]:
            ut["grunn"] = f"timetaket er nådd ({grunn})"
            return ut
        ut.update(lade=True, strom=strom, grunn=grunn)
        return ut

    # --- utførelse ---
    async def _utfor(self, handling: str, detaljer: dict, fn):
        """Kjører kommandoen i aktiv modus, logger alltid (dry-run: bare ved endring)."""
        nokkel = (handling, detaljer.get("strom"))
        if not self.aktiv and nokkel == self.sist_logget:
            return
        self.sist_logget = nokkel
        feil = None
        if self.aktiv:
            try:
                await fn()
                self.tilstand.oppfrisk.set()
            except Exception as e:
                feil = str(e)[:300]
                log.warning("kommando %s feilet: %s", handling, feil)
                await varsel.send("Lading: kommando feilet", f"{handling}: {feil}", "high")
        log.info("%s%s %s", "" if self.aktiv else "[dry-run] ", handling, json.dumps(detaljer, ensure_ascii=False))
        self.db.x("INSERT INTO styring_logg VALUES (?,?,?,?,?)", time.time(), handling,
                  json.dumps(detaljer, ensure_ascii=False), int(self.aktiv and feil is None), feil)

    async def steg(self):
        naa_ts = time.time()
        u = self.onske(naa_ts)
        lader = self.tilstand.lader or {}
        modus, final_stop = lader.get("modus"), lader.get("final_stop")
        tilkoblet = modus in (2, 3, 5)
        if lader.get("ts") is not None:
            if tilkoblet and not self.var_tilkoblet:
                self.stopp_i_okt = 0
            if not tilkoblet and self.var_tilkoblet and (u["overstyring"] or {}).get("type") == "lad_naa":
                self.db.lagre("overstyring", None)  # «lad nå» gjelder til bilen kobles fra
            self.var_tilkoblet = tilkoblet
        if modus == 3:
            self.gjenopptak_uten_lading = 0
        satt = self.satt_strom if self.satt_strom is not None else lader.get("strom_satt")
        u["satt_strom"], u["stopp_i_okt"], u["handling"] = satt, self.stopp_i_okt, None
        inst = self.tilstand.installasjon or {}
        u["advarsler"] = []
        if "Eco-modus" in inst.get("funksjoner", []):
            u["advarsler"].append("Eco-modus er på i Zaptec og styrer ladingen selv. Slå den av før aktiv styring.")
        self.tilstand.styring = u
        if lader.get("ts") is None or naa_ts - lader["ts"] > 300:
            u["handling"] = "venter på status fra laderen"
            return
        if not tilkoblet:
            u["handling"] = "bilen er ikke tilkoblet"
            return
        siden_pause = naa_ts - self.sist_pause
        siden_strom = naa_ts - self.sist_strom
        d = {"grunn": u["grunn"], "modus": modus}

        if u["lade"]:
            # Strømmen først, så den er riktig når ladingen starter.
            if satt is None or u["strom"] < satt:
                if satt is None or u.get("fasevakt") or siden_strom >= self.sc["strom_ned_min"] * 60:
                    await self._sett_strom(u["strom"], d)
            elif u["strom"] >= satt + 2 and siden_strom >= self.sc["strom_opp_min"] * 60:
                await self._sett_strom(u["strom"], d)
            if modus == 3:
                u["handling"] = f"lader med {u['strom']} A"
            elif modus == 5 and not final_stop:
                u["handling"] = "bilen er ferdig (eller vil ikke lade)"
            elif siden_pause >= self.sc["pause_min"] * 60:
                u["handling"] = "gjenopptar ladingen"
                self.sist_pause = naa_ts
                if self.aktiv:
                    self.gjenopptak_uten_lading += 1
                    if self.gjenopptak_uten_lading == 3:
                        await varsel.send("Lading starter ikke",
                                          "Tre forsøk på å gjenoppta ladingen uten at bilen lader. Sjekk bilen.", "high")
                await self._utfor("gjenoppta", d, lambda: self.z.kommando(self.cfg["zaptec"]["charger_id"], GJENOPPTA))
            else:
                u["handling"] = "venter litt før gjenopptak"
        else:
            if modus in (2, 3) and not final_stop:
                if self.stopp_i_okt >= self.sc["maks_stopp"]:
                    u["handling"] = "for mange stopp i denne økta, lader på minimum"
                    if satt is None or satt > self.sc["min_strom"]:
                        await self._sett_strom(self.sc["min_strom"], d)
                elif siden_pause >= self.sc["pause_min"] * 60:
                    u["handling"] = "pauser ladingen"
                    self.sist_pause = naa_ts
                    if self.aktiv:
                        self.stopp_i_okt += 1
                    await self._utfor("pause", d, lambda: self.z.kommando(self.cfg["zaptec"]["charger_id"], PAUSE))
                else:
                    u["handling"] = "venter litt før pause"
            else:
                u["handling"] = "pauset" if final_stop else "bilen er ferdig" if modus == 5 else "lader ikke"

    async def _sett_strom(self, strom: int, d: dict):
        self.sist_strom = time.time()
        if self.aktiv:
            self.satt_strom = strom
        await self._utfor("sett strøm", {**d, "strom": strom},
                          lambda: self.z.sett_strom(self.cfg["zaptec"]["installation_id"], strom))


def logg(db: DB, antall: int = 15) -> list[dict]:
    return [{"ts": r["ts"], "handling": r["handling"], "detaljer": json.loads(r["detaljer"] or "{}"),
             "utfort": bool(r["utfort"]), "feil": r["feil"]}
            for r in db.alle("SELECT * FROM styring_logg ORDER BY ts DESC LIMIT ?", antall)]


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
    if cfg["styring"]["modus"] == "av":
        tilstand.styring = {"handling": "styringen er av"}
        await asyncio.Event().wait()
    s = Styring(cfg, db, tilstand, http)
    log.info("styring startet i modus %s", cfg["styring"]["modus"])
    await asyncio.sleep(20)  # la Pulse og Zaptec levere første data
    while True:
        try:
            await s.steg()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("styringsfeil: %s", e)
            if tilstand.styring is not None:
                tilstand.styring["feil"] = str(e)[:300]
        try:  # en endret overstyring fra websiden vekker løkka med en gang
            await asyncio.wait_for(tilstand.styr_naa.wait(), TICK)
        except asyncio.TimeoutError:
            pass
        tilstand.styr_naa.clear()
