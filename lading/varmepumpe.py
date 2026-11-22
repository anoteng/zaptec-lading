"""Varmepumpe (Mitsubishi) via MELCloud Home: status, styring og strømforbruk per time.

Bruker biblioteket aiomelcloudhome, det samme som Home Assistant bruker. API-et er uoffisielt og kan endre seg.
Innloggingen legges inn på websiden (eller som MELCLOUD_USER/MELCLOUD_PASSWORD i .env) og testes før den lagres.

- Status for luft-luft-enhetene hentes hvert minutt, og rett etter en kommando.
- Strømforbruket kommer per time (UTC) i hele 100 Wh, med noen minutters forsinkelse, og bare for de siste
  23 timene. Det hentes hvert 15. minutt og lagres i time_vp, så historikken bygges opp herfra.
- Timer uten forbruk mangler i svaret fra MELCloud og regnes som 0.
- Ukeprogrammet («cloud schedule») ligger i MELCloud og utføres derfra. Det leses fra /context og endres med
  POST/DELETE /monitor/cloudschedule/{enhet}. Punktene lagres alltid som aktive (MELCloud ignorerer «enabled»).

Kompressoren skal ikke startes og stoppes ofte: koden slår aldri pumpen av eller på som bieffekt, og nye
programpunkter lagres alltid med power = true. Av/på skjer bare når brukeren ber uttrykkelig om det.
"""
import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import uuid
from zoneinfo import ZoneInfo

import aiohttp
from aiomelcloudhome import (ATAFanSpeed, ATAOperationMode, ATAVaneHorizontal, ATAVaneVertical, MELCloudHome,
                             MelCloudHomeAuthenticationError, MelCloudHomeError, UserContext)

from . import config
from .db import DB
from .tid import dognstart, naa
from .tilstand import Tilstand

log = logging.getLogger("lading.varmepumpe")
SJEKK_SEK = 60
ENERGI_SEK = 900
ENERGI_TIMER = 23
OPPFRISK_ETTER_SEK = 3

MODUSER = {"Heat": "has_heat_operation_mode", "Cool": "has_cool_operation_mode", "Automatic": "has_auto_operation_mode",
           "Dry": "has_dry_operation_mode", "Fan": "has_fan_operation_mode"}
VIFTER = ["One", "Two", "Three", "Four", "Five"]

DAGER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]   # ISO 1–7
PROG_MODUS = {"Heat": "heat", "Cool": "cool", "Automatic": "automatic", "Dry": "dry"}

_aktiv: "Varmepumpe | None" = None


def maskert(epost: str | None) -> str | None:
    """a.b@eksempel.no → a…@eksempel.no (nok til å kjenne igjen kontoen, uten å vise hele adressen)."""
    if not epost:
        return None
    navn, _, domene = epost.partition("@")
    return f"{navn[:1]}…@{domene}" if domene else f"{navn[:1]}…"


def innlogging(cfg: dict) -> dict:
    h = cfg["hemmelig"]
    return {"satt": bool(h.get("MELCLOUD_USER") and h.get("MELCLOUD_PASSWORD")), "bruker": maskert(h.get("MELCLOUD_USER"))}


def _enhetsliste(ctx) -> list[dict]:
    ut = []
    for b in ctx.buildings:
        for u in b.air_to_air_units:
            ut.append({"id": u.id, "navn": u.name, "type": "luft-luft",
                       "energimaaler": bool(u.capabilities and u.capabilities.has_energy_consumed_meter)})
        for u in b.air_to_water_units:
            ut.append({"id": u.id, "navn": u.name, "type": "luft-vann"})
    return ut


async def lagre_innlogging(cfg: dict, bruker: str, passord: str) -> dict:
    """Test innloggingen mot MELCloud Home, og lagre den i .env bare hvis den virker."""
    bruker, passord = (bruker or "").strip(), passord or ""
    if not bruker or "@" not in bruker or not passord:
        raise ValueError("fyll inn e-post og passord")
    if len(bruker) > 200 or len(passord) > 200:
        raise ValueError("for lang e-post eller passord")
    try:
        async with MELCloudHome(username=bruker, password=passord) as m:
            enheter = _enhetsliste(await m.get_context())
    except MelCloudHomeAuthenticationError:
        raise ValueError("MELCloud Home avviste innloggingen. Sjekk e-post og passord.") from None
    except MelCloudHomeError as e:
        raise ValueError(f"fikk ikke kontakt med MELCloud Home: {e}") from None
    except Exception as e:  # uventet svar fra et uoffisielt API; ikke ta med detaljer som kan inneholde innloggingen
        log.warning("MELCloud Home-innlogging feilet: %s", type(e).__name__)
        raise ValueError(f"uventet feil fra MELCloud Home ({type(e).__name__})") from None
    config.skriv_env({"MELCLOUD_USER": bruker, "MELCLOUD_PASSWORD": passord})
    cfg["hemmelig"].update(MELCLOUD_USER=bruker, MELCLOUD_PASSWORD=passord)
    log.info("MELCloud Home-innlogging lagret (%d enheter)", len(enheter))
    if _aktiv:
        _aktiv.vekk.set()
    return {**innlogging(cfg), "enheter": enheter}


def fjern_innlogging(cfg: dict) -> dict:
    config.skriv_env({"MELCLOUD_USER": None, "MELCLOUD_PASSWORD": None})
    cfg["hemmelig"].pop("MELCLOUD_USER", None)
    cfg["hemmelig"].pop("MELCLOUD_PASSWORD", None)
    log.info("MELCloud Home-innlogging fjernet")
    if _aktiv:
        _aktiv.vekk.set()
    return innlogging(cfg)


def _verdi(x):
    return x.value if hasattr(x, "value") else x


def _evner(c) -> dict:
    if c is None:
        return {"moduser": ["Heat"], "vifter": 5, "auto_vifte": True, "halve": False, "temp": {}}
    temp = {"Heat": [c.min_temp_heat, c.max_temp_heat], "Cool": [c.min_temp_cool, c.max_temp_cool],
            "Dry": [c.min_temp_cool, c.max_temp_cool], "Automatic": [c.min_temp_auto, c.max_temp_auto]}
    return {"moduser": [m for m, f in MODUSER.items() if getattr(c, f, None)] or ["Heat"],
            "vifter": c.number_of_fan_speeds or 5, "auto_vifte": bool(c.has_automatic_fan_speed),
            "halve": bool(c.has_half_degree_increments), "lamell_v": bool(c.has_vane_vertical),
            "lamell_h": bool(c.has_vane_horizontal), "sving": bool(c.has_swing), "energimaaler": bool(c.has_energy_consumed_meter),
            "temp": {m: v for m, v in temp.items() if v[0] is not None and v[1] is not None}}


def _enhet(u) -> dict:
    return {"id": u.id, "navn": u.name, "paa": u.power, "modus": _verdi(u.operation_mode), "temperatur": u.set_temperature,
            "rom": u.room_temperature, "vifte": _verdi(u.set_fan_speed), "faktisk_vifte": _verdi(u.actual_fan_speed),
            "lamell_v": _verdi(u.vane_vertical_direction), "lamell_h": _verdi(u.vane_horizontal_direction),
            "standby": u.in_standby_mode, "feil": u.is_in_error, "feilkode": (u.settings or {}).get("ErrorCode") or None,
            "rssi": u.rssi, "evner": _evner(u.capabilities)}


def valider(enhet: dict, d: dict) -> dict:
    """Gjør en endring fra API-et om til argumenter for control_ata_unit, sjekket mot enhetens evner."""
    ev, ut = enhet["evner"], {}
    if "paa" in d:
        if not isinstance(d["paa"], bool):
            raise ValueError("paa må være true eller false")
        ut["power"] = d["paa"]
    modus = enhet["modus"]
    if "modus" in d:
        if d["modus"] not in ev["moduser"]:
            raise ValueError(f"ukjent modus {d['modus']!r}, gyldige: {', '.join(ev['moduser'])}")
        modus = d["modus"]
        ut["operation_mode"] = ATAOperationMode(modus)
    if "temperatur" in d:
        t = d["temperatur"]
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise ValueError("temperatur må være et tall")
        lo, hi = ev["temp"].get(modus, [10, 31])
        if not lo <= t <= hi:
            raise ValueError(f"temperatur må være {lo:g}–{hi:g} °C i modus {modus}")
        steg = 0.5 if ev["halve"] else 1.0
        if abs(t / steg - round(t / steg)) > 1e-6:
            raise ValueError(f"temperatur må gå i steg på {steg:g} °C")
        ut["set_temperature"] = float(t)
    if "vifte" in d:
        gyldige = (["Auto"] if ev["auto_vifte"] else []) + VIFTER[:ev["vifter"]]
        if d["vifte"] not in gyldige:
            raise ValueError(f"ukjent viftehastighet {d['vifte']!r}, gyldige: {', '.join(gyldige)}")
        ut["set_fan_speed"] = ATAFanSpeed(d["vifte"])
    for felt, enum, arg, evne in (("lamell_v", ATAVaneVertical, "vane_vertical_direction", "lamell_v"),
                                  ("lamell_h", ATAVaneHorizontal, "vane_horizontal_direction", "lamell_h")):
        if felt in d:
            gyldige = [e.value for e in enum if e.value != "Swing" or ev.get("sving")]
            if not ev.get(evne) or d[felt] not in gyldige:
                raise ValueError(f"ugyldig {felt} {d[felt]!r}, gyldige: {', '.join(gyldige) if ev.get(evne) else 'ingen'}")
            ut[arg] = enum(d[felt])
    if not ut:
        raise ValueError("ingen endring: bruk paa, modus, temperatur, vifte, lamell_v eller lamell_h")
    return ut


def _program(raw: dict, evner: dict) -> dict:
    """MELClouds ukeprogram for én enhet, i API-ets format: dager som ISO-ukedag (1 = mandag), tid «HH:MM»."""
    tilbake = {v: k for k, v in PROG_MODUS.items()}
    punkter = []
    for p in raw.get("schedule") or []:
        try:
            dager = sorted(DAGER.index(d) + 1 for d in p.get("days", []))
        except ValueError:
            continue
        punkter.append({"id": p.get("id"), "dager": dager, "tid": str(p.get("time", ""))[:5],
                        "modus": tilbake.get(str(p.get("operationMode", "")).lower(), p.get("operationMode")),
                        "temperatur": p.get("setPoint"), "paa": bool(p.get("power", True)), "_raw": p})
    punkter.sort(key=lambda p: (p["tid"], p["dager"]))
    sone = ZoneInfo(raw.get("timeZone") or "Europe/Oslo")
    return {"aktiv": bool(raw.get("scheduleEnabled")), "punkter": punkter, "neste": _neste(punkter, sone),
            "tidssone": str(sone)}


def _neste(punkter: list[dict], sone: ZoneInfo, naa_ts: float | None = None) -> dict | None:
    naa_dt = datetime.fromtimestamp(naa_ts or time.time(), sone)
    best = None
    for p in punkter:
        try:
            h, m = (int(x) for x in p["tid"].split(":"))
        except ValueError:
            continue
        for d in range(8):
            dag = (naa_dt + timedelta(days=d)).date()
            if dag.isoweekday() not in p["dager"]:
                continue
            t = datetime(dag.year, dag.month, dag.day, h, m, tzinfo=sone)
            if t > naa_dt and (best is None or t < best[0]):
                best = (t, p)
    if not best:
        return None
    t, p = best
    return {"ts": int(t.timestamp()), "id": p["id"], "temperatur": p["temperatur"], "modus": p["modus"], "paa": p["paa"]}


def valider_punkt(d: dict, evner: dict, gammel: dict | None) -> dict:
    """Programpunkt fra API-et → MELClouds format. Beholder vifte/luftretning (og av/på) fra et eksisterende punkt."""
    dager = d.get("dager", gammel["dager"] if gammel else None)
    if not isinstance(dager, list) or not dager or any(not isinstance(x, int) or isinstance(x, bool) or not 1 <= x <= 7 for x in dager):
        raise ValueError("dager må være en liste med ukedager 1–7 (1 = mandag)")
    tid = d.get("tid", gammel["tid"] if gammel else None)
    try:
        h, m = (int(x) for x in str(tid).split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59) or len(str(tid)) != 5:
            raise ValueError
    except ValueError:
        raise ValueError("tid må være «HH:MM», f.eks. «22:00»") from None
    modus = d.get("modus", gammel["modus"] if gammel else "Heat")
    if modus not in PROG_MODUS or modus not in evner["moduser"]:
        raise ValueError(f"ugyldig modus {modus!r}, gyldige: {', '.join(m for m in PROG_MODUS if m in evner['moduser'])}")
    t = d.get("temperatur", gammel["temperatur"] if gammel else None)
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        raise ValueError("temperatur må være et tall")
    lo, hi = evner["temp"].get(modus, [10, 31])
    steg = 0.5 if evner["halve"] else 1.0
    if not lo <= t <= hi or abs(t / steg - round(t / steg)) > 1e-6:
        raise ValueError(f"temperatur må være {lo:g}–{hi:g} °C i steg på {steg:g} i modus {modus}")
    if "paa" in d:
        raise ValueError("programpunkter kan ikke slå pumpen av eller på her; bare temperatur og modus")
    raw = dict(gammel["_raw"]) if gammel else {"power": True, "vaneVerticalDirection": "auto",
                                                "vaneHorizontalDirection": "swing", "setFanSpeed": "auto"}
    raw.update(id=gammel["id"] if gammel else str(uuid.uuid4()), days=[DAGER[x - 1] for x in sorted(set(dager))],
               time=f"{h:02d}:{m:02d}:00", enabled=True, operationMode=PROG_MODUS[modus], setPoint=t)
    return raw


def _offentlig(e: dict) -> dict:
    """Enheten slik API-et viser den (uten MELClouds rådata for programpunktene)."""
    prog = e.get("program")
    if not prog:
        return dict(e)
    return {**e, "program": _offentlig_prog(prog)}


def _offentlig_prog(prog: dict) -> dict:
    return {**prog, "punkter": [{k: v for k, v in p.items() if k != "_raw"} for p in prog["punkter"]]}


class Varmepumpe:
    def __init__(self, cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
        self.cfg, self.db, self.tilstand = cfg, db, tilstand
        self.http: aiohttp.ClientSession | None = None
        self.kilde = tilstand.kilder["varmepumpe"]
        self.m: MELCloudHome | None = None
        self.nokkel: tuple | None = None
        self.enheter: dict[str, dict] = {}
        self.ute: float | None = None
        self.sist_energi = 0.0
        self.laas = asyncio.Lock()
        self.vekk = asyncio.Event()

    def _innlogget(self) -> MELCloudHome | None:
        h = self.cfg["hemmelig"]
        nokkel = (h.get("MELCLOUD_USER"), h.get("MELCLOUD_PASSWORD"))
        if nokkel != self.nokkel:
            self.nokkel, self.m, self.sist_energi = nokkel, None, 0.0
            if self.http and not self.http.closed:  # egen økt (med egne cookies fra innloggingen) per innlogging
                asyncio.ensure_future(self.http.close())
            self.http = None
            if all(nokkel):
                self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
                self.m = MELCloudHome(username=nokkel[0], password=nokkel[1], session=self.http, request_timeout=20)
        return self.m

    def _publiser(self):
        self.tilstand.varmepumpe = {"ts": time.time(), "enheter": [_offentlig(e) for e in self.enheter.values()], "ute": self.ute,
                                    "energi": self._energi_siste()}

    def _energi_siste(self) -> dict:
        idag = dognstart(naa().date())
        r = self.db.en("SELECT SUM(kwh) k FROM time_vp WHERE time >= ?", idag)
        s = self.db.en("SELECT time, SUM(kwh) k FROM time_vp GROUP BY time ORDER BY time DESC LIMIT 1")
        return {"idag_kwh": round(r["k"] or 0.0, 1) if r else 0.0,
                "siste_time": s["time"] if s else None, "siste_kwh": s["k"] if s else None}

    async def oppdater(self):
        m = self._innlogget()
        if m is None:
            self.enheter, self.ute = {}, None
            self.tilstand.varmepumpe = None
            self.kilde.info = "ikke satt opp (logg inn under Varmepumpe på websiden)"
            return
        async with self.laas:
            raw = await m._request("/context")  # rått svar: modellen i biblioteket tar ikke med programmet
        ctx = UserContext.model_validate(raw)
        rawenh = {u.get("id"): u for b in raw.get("buildings", []) for u in b.get("airToAirUnits", [])}
        self.enheter = {}
        for b in ctx.buildings:
            for u in b.air_to_air_units:
                e = _enhet(u)
                e["program"] = _program(rawenh.get(u.id, {}), e["evner"])
                self.enheter[u.id] = e
        if time.time() - self.sist_energi >= ENERGI_SEK:
            await self._hent_energi(m)
            self.sist_energi = time.time()
        self._publiser()
        e = next(iter(self.enheter.values()), None)
        self.kilde.ok(f"{e['navn']}: {'på' if e['paa'] else 'av'}, rom {e['rom']:g} °C" if e and e["rom"] is not None
                      else f"{len(self.enheter)} enheter")

    async def _hent_energi(self, m: MELCloudHome):
        til = datetime.now(UTC).replace(second=0, microsecond=0)
        fra = til - timedelta(hours=ENERGI_TIMER)
        for uid, e in self.enheter.items():
            if not e["evner"].get("energimaaler"):
                continue
            async with self.laas:
                verdier = await m.get_energy_telemetry(uid, fra, til, "Hour")
            for v in verdier:
                try:
                    t = int(datetime.strptime(v.time[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp())
                    kwh = float(v.value) / 1000
                except ValueError:
                    continue
                if 0 <= kwh <= 50 and t % 3600 == 0:   # 100 Wh-oppløsning; avvis åpenbart feile tall
                    self.db.x("INSERT INTO time_vp VALUES (?, ?, ?) ON CONFLICT(time, enhet) DO UPDATE SET kwh = excluded.kwh",
                              t, uid, kwh)
        uid = next(iter(self.enheter), None)
        if uid:  # utetemperaturen fra utedelen
            try:
                async with self.laas:
                    self.ute = await m.get_outdoor_temperature(uid)
            except MelCloudHomeError as e:
                log.info("utetemperatur: %s", e)

    async def _finn(self, enhet_id: str | None) -> tuple[MELCloudHome, dict]:
        m = self._innlogget()
        if m is None:
            raise ValueError("varmepumpen er ikke satt opp (logg inn under Varmepumpe)")
        if not self.enheter:
            await self.oppdater()
        enhet = self.enheter.get(enhet_id) if enhet_id else next(iter(self.enheter.values()), None)
        if enhet is None:
            raise ValueError(f"fant ikke enheten {enhet_id!r}" if enhet_id else "fant ingen luft-luft-enhet")
        return m, enhet

    async def program(self, enhet_id: str | None, d: dict) -> dict:
        """Endre ukeprogrammet i MELCloud: lagre (nytt eller eksisterende) punkt, slette punkt, eller slå av/på programmet."""
        m, enhet = await self._finn(enhet_id)
        prog = enhet.get("program") or {"punkter": []}
        handling = d.get("handling")
        if handling == "lagre":
            punkt = d.get("punkt")
            if not isinstance(punkt, dict):
                raise ValueError("punkt mangler")
            gammel = None
            if punkt.get("id"):
                gammel = next((p for p in prog["punkter"] if p["id"] == punkt["id"]), None)
                if gammel is None:
                    raise ValueError(f"fant ikke programpunktet {punkt['id']!r}")
            if gammel is None and len(prog["punkter"]) >= 40:
                raise ValueError("for mange programpunkter")
            data = valider_punkt(punkt, enhet["evner"], gammel)
            kall = ("POST", f"/monitor/cloudschedule/{enhet['id']}", data)
            logg = f"lagret programpunkt {data['time'][:5]} {','.join(data['days'])} {data['setPoint']:g} °C"
        elif handling == "slett":
            pid = d.get("id")
            if not any(p["id"] == pid for p in prog["punkter"]):
                raise ValueError(f"fant ikke programpunktet {pid!r}")
            kall = ("DELETE", f"/monitor/cloudschedule/{enhet['id']}/{pid}", None)
            logg = f"slettet programpunkt {pid}"
        elif handling == "aktiv":
            if not isinstance(d.get("aktiv"), bool):
                raise ValueError("aktiv må være true eller false")
            kall = ("PUT", f"/monitor/cloudschedule/{enhet['id']}/enabled", {"enabled": d["aktiv"]})
            logg = f"programmet {'slått på' if d['aktiv'] else 'slått av'}"
        else:
            raise ValueError("handling må være lagre, slett eller aktiv")
        async with self.laas:
            try:
                await m._request(kall[1], method=kall[0], json=kall[2])
            except MelCloudHomeError as e:
                log.warning("program for %s feilet: %s", enhet["navn"], e)
                raise RuntimeError(f"MELCloud Home avviste endringen: {e}") from None
        log.info("varmepumpe %s: %s", enhet["navn"], logg)
        await self.oppdater()
        return _offentlig_prog(self.enheter.get(enhet["id"], enhet).get("program") or {"punkter": []})

    async def styr(self, enhet_id: str | None, d: dict) -> dict:
        m, enhet = await self._finn(enhet_id)
        args = valider(enhet, d)
        async with self.laas:
            try:
                await m.control_ata_unit(enhet["id"], **args)
            except MelCloudHomeError as e:
                log.warning("kommando til %s feilet: %s", enhet["navn"], e)
                raise RuntimeError(f"MELCloud Home avviste kommandoen: {e}") from None
        log.info("varmepumpe %s: %s", enhet["navn"], {k: _verdi(v) for k, v in args.items()})
        # Vis endringen med en gang, og hent fasit litt etter.
        for felt, arg in (("paa", "power"), ("modus", "operation_mode"), ("temperatur", "set_temperature"),
                          ("vifte", "set_fan_speed"), ("lamell_v", "vane_vertical_direction"),
                          ("lamell_h", "vane_horizontal_direction")):
            if arg in args:
                enhet[felt] = _verdi(args[arg])
        self._publiser()
        self.vekk.set()
        return _offentlig(enhet)


async def styr(enhet_id: str | None, d: dict) -> dict:
    if _aktiv is None:
        raise ValueError("varmepumpen er ikke startet")
    return await _aktiv.styr(enhet_id, d)


async def program(enhet_id: str | None, d: dict) -> dict:
    if _aktiv is None:
        raise ValueError("varmepumpen er ikke startet")
    return await _aktiv.program(enhet_id, d)


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
    global _aktiv
    v = _aktiv = Varmepumpe(cfg, db, tilstand, http)
    feil = 0
    while True:
        etter_kommando = v.vekk.is_set()
        v.vekk.clear()
        if etter_kommando:
            await asyncio.sleep(OPPFRISK_ETTER_SEK)  # gi MELCloud tid til å ta imot endringen
        try:
            await v.oppdater()
            feil = 0
        except asyncio.CancelledError:
            raise
        except MelCloudHomeAuthenticationError:
            v.nokkel = None  # logg inn på nytt neste gang
            v.kilde.feilet("innloggingen ble avvist av MELCloud Home")
            feil += 1
        except Exception as e:
            log.warning("varmepumpe: %s", e)
            v.kilde.feilet(e)
            feil += 1
        vent = SJEKK_SEK if feil == 0 else min(600, SJEKK_SEK * 2 ** min(feil, 4))
        try:
            await asyncio.wait_for(v.vekk.wait(), vent)
        except asyncio.TimeoutError:
            pass
