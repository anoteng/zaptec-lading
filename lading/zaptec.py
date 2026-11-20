"""Zaptec: lesing av lader- og installasjonsstatus, og kommandoer (brukes bare av styringen i aktiv modus)."""
import asyncio
import datetime as dt
import json
import logging
import time

import aiohttp

from .db import DB
from .tid import time_av
from .tilstand import Tilstand, sov_med_backoff

log = logging.getLogger("lading.zaptec")
BASE = "https://api.zaptec.com"
MODUS = {0: "Ukjent", 1: "Ikke tilkoblet", 2: "Tilkoblet, venter", 3: "Lader", 5: "Ferdig / pause"}
FUNKSJONER = {4: "APM", 8: "Eco-modus", 16: "Planlagt lading"}

# Observasjons-ID-er fra /api/constants
OBS = {"online": -2, "spenning": (501, 502, 503), "strom": (507, 508, 509), "effekt": 513,
       "okt_kwh": 553, "signert": 554, "strom_satt": 708, "modus": 710, "final_stop": 718,
       "neste_plan": 763, "maks_strom": 510, "min_strom": 511}


class Zaptec:
    def __init__(self, http: aiohttp.ClientSession, bruker: str, passord: str):
        self.http, self.bruker, self.passord = http, bruker, passord
        self.token: str | None = None
        self.utloper = 0.0

    async def _token(self) -> str:
        if self.token and time.time() < self.utloper - 300:
            return self.token
        async with self.http.post(BASE + "/oauth/token", data={
                "grant_type": "password", "username": self.bruker, "password": self.passord}) as r:
            if r.status != 200:
                raise RuntimeError(f"innlogging feilet: HTTP {r.status}")
            d = await r.json()
        self.token, self.utloper = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self.token

    async def get(self, sti: str):
        for forsok in range(2):
            h = {"Authorization": f"Bearer {await self._token()}", "Accept": "application/json"}
            async with self.http.get(BASE + sti, headers=h) as r:
                if r.status == 401 and forsok == 0:
                    self.token = None
                    continue
                if r.status != 200:
                    raise RuntimeError(f"GET {sti}: HTTP {r.status}")
                return await r.json()

    async def post(self, sti: str, data: dict | None = None):
        for forsok in range(2):
            h = {"Authorization": f"Bearer {await self._token()}", "Accept": "application/json"}
            async with self.http.post(BASE + sti, headers=h, json=data) as r:
                if r.status == 401 and forsok == 0:
                    self.token = None
                    continue
                if r.status not in (200, 202, 204):
                    raise RuntimeError(f"POST {sti}: HTTP {r.status} {(await r.text())[:200]}")
                return

    async def kommando(self, charger_id: str, kode: int):
        """506 = pause (StopChargingFinal), 507 = gjenoppta (ResumeCharging)."""
        await self.post(f"/api/chargers/{charger_id}/sendCommand/{kode}")

    async def sett_strom(self, inst_id: str, ampere: float):
        """Tilgjengelig strøm per fase for installasjonen. Zaptec ber om høyst én endring per 15 min."""
        await self.post(f"/api/installation/{inst_id}/update", {"AvailableCurrent": ampere})

    async def state(self, charger_id: str) -> dict[int, str | None]:
        return {o["StateId"]: o.get("ValueAsString") for o in await self.get(f"/api/chargers/{charger_id}/state")}

    async def installasjon(self, inst_id: str) -> dict:
        return await self.get(f"/api/installation/{inst_id}")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _signerte_avlesninger(verdi: str | None) -> list[tuple[int, float]]:
    """Plukker (ts, kWh) ut av OCMF-strengen i observasjon 554."""
    if not verdi or "|" not in verdi:
        return []
    try:
        d = json.loads(verdi.split("|", 1)[1])
        ut = []
        for rd in d.get("RD", []):
            tm = rd["TM"].split(" ")[0].replace(",", ".")
            ut.append((int(dt.datetime.fromisoformat(tm).timestamp()), float(rd["RV"])))
        return ut
    except (ValueError, KeyError, TypeError):
        return []


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
    z = Zaptec(http, cfg["hemmelig"]["ZAPTEC_USER"], cfg["hemmelig"]["ZAPTEC_PASSWORD"])
    zc = cfg["zaptec"]
    kilde = tilstand.kilder["zaptec"]
    intervall = zc.get("poll_sekunder", 60)
    sist_inst = 0.0
    forrige: tuple[float, float] | None = None  # (ts, effekt_w) for integrering
    forsok = 0
    while True:
        try:
            if time.time() - sist_inst > 1800:
                inst = await z.installasjon(zc["installation_id"])
                feat = inst.get("EnabledFeatures", 0)
                tilstand.installasjon = {
                    "navn": inst.get("Name"), "maks_strom": inst.get("MaxCurrent"),
                    "modus": inst.get("AvailableCurrentMode"),
                    "funksjoner": [n for bit, n in FUNKSJONER.items() if feat & bit],
                    "eco_avreise": inst.get("Feature_PowerManagement_EcoMode_DepartureTime"),
                    "tilgjengelig_strom": inst.get("AvailableCurrent"),
                    "hentet": time.time()}
                sist_inst = time.time()

            s = await z.state(zc["charger_id"])
            naa = time.time()
            effekt = _f(s.get(OBS["effekt"])) or 0.0
            modus = int(_f(s.get(OBS["modus"])) or 0)
            if modus != 3:
                effekt = 0.0  # siste effektverdi kan henge igjen etter endt lading
            rad = {"ts": int(naa), "online": s.get(OBS["online"]) == "1", "modus": modus,
                   "modus_tekst": MODUS.get(modus, str(modus)), "effekt_w": effekt,
                   "okt_kwh": _f(s.get(OBS["okt_kwh"])), "strom_satt": _f(s.get(OBS["strom_satt"])),
                   "strom": [_f(s.get(i)) for i in OBS["strom"]],
                   "final_stop": s.get(OBS["final_stop"]) == "1",
                   "neste_plan": s.get(OBS["neste_plan"]),
                   "maks_strom": _f(s.get(OBS["maks_strom"])), "min_strom": _f(s.get(OBS["min_strom"]))}
            db.x("INSERT OR REPLACE INTO lader VALUES (?,?,?,?,?,?,?,?,?,?)",
                 rad["ts"], int(rad["online"]), modus, effekt, rad["okt_kwh"], rad["strom_satt"],
                 *rad["strom"], int(rad["final_stop"]))
            for ts, kwh in _signerte_avlesninger(s.get(OBS["signert"])):
                db.x("INSERT OR IGNORE INTO lader_maaler VALUES (?, ?)", ts, kwh)

            # Integrer effekten til energi per klokketime (trapes, delt ved timeskifte).
            if forrige and 0 < naa - forrige[0] < 5 * intervall:
                t0, p0 = forrige
                snitt = (p0 + effekt) / 2
                while t0 < naa:
                    slutt = min(naa, time_av(t0) + 3600)
                    db.legg_til_lader_kwh(time_av(t0), snitt / 1000 * (slutt - t0) / 3600)
                    t0 = slutt
            forrige = (naa, effekt)

            tilstand.lader = rad
            kilde.ok(f"{rad['modus_tekst']}, {effekt / 1000:.1f} kW")
            forsok = 0
            try:  # styringen kan be om ny status rett etter en kommando
                await asyncio.wait_for(tilstand.oppfrisk.wait(), intervall)
                await asyncio.sleep(5)
            except asyncio.TimeoutError:
                pass
            tilstand.oppfrisk.clear()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kilde.feilet(e)
            log.warning("Zaptec-feil (forsøk %d): %s", forsok + 1, e)
            forrige = None
            await sov_med_backoff(forsok, minst=30, maks=900)
            forsok += 1
