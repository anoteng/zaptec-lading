"""Tibber Pulse: sanntidsmålinger via GraphQL-websocket (bare lesing)."""
import asyncio
import datetime as dt
import json
import logging
import time

import aiohttp

from .db import DB
from .tid import time_av
from .tilstand import Tilstand, sov_med_backoff

log = logging.getLogger("lading.tibber")
API = "https://api.tibber.com/v1-beta/gql"
SPORRING = """subscription { liveMeasurement(homeId: "%s") {
  timestamp power accumulatedConsumptionLastHour lastMeterConsumption
  currentL1 currentL3 voltagePhase1 voltagePhase2 voltagePhase3 } }"""
STILLE_MAKS = 120  # sekunder uten data før vi kobler til på nytt


class Timeteller:
    """Holder styr på inneværende klokketime og lagrer timeforbruk når timen skifter."""

    def __init__(self, db: DB):
        self.db = db
        self.time: int | None = None
        self.siste: tuple[int, float, float] | None = None  # (ts, effekt_w, time_kwh)
        self.maaler: float | None = None

    def ny_maling(self, ts: int, effekt: float, time_kwh: float | None, maaler: float | None):
        t = time_av(ts)
        if self.time is not None and t != self.time and self.siste:
            # Avslutt forrige time: siste akkumulerte verdi + effekt resten av timen.
            sts, seff, skwh = self.siste
            if skwh is not None and t == self.time + 3600:
                rest = max(0, self.time + 3600 - sts)
                self.db.sett_time_hus(self.time, "estimat", skwh + seff / 1000 * rest / 3600)
        self.time = t
        self.siste = (ts, effekt, time_kwh)
        if maaler is not None and maaler != self.maaler:
            self.maaler = maaler
            self._ny_maalerstand(ts, maaler)

    def _ny_maalerstand(self, ts: int, kwh: float):
        self.db.x("INSERT OR REPLACE INTO maalerstand VALUES (?, ?)", ts, kwh)
        grense = round(ts / 3600) * 3600
        if abs(ts - grense) > 600:
            return  # ikke en timesavlesning
        forrige = self.db.en("SELECT ts, kwh FROM maalerstand WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
                             grense - 3600 - 600, grense - 3600 + 600)
        if forrige and kwh >= forrige["kwh"]:
            self.db.sett_time_hus(grense - 3600, "maaler", round(kwh - forrige["kwh"], 4))


async def _ws_url(http: aiohttp.ClientSession, token: str) -> str:
    async with http.post(API, json={"query": "{ viewer { websocketSubscriptionUrl } }"},
                         headers={"Authorization": f"Bearer {token}"}) as r:
        r.raise_for_status()
        return (await r.json())["data"]["viewer"]["websocketSubscriptionUrl"]


def _epoch(tidsstempel: str) -> int:
    return int(dt.datetime.fromisoformat(tidsstempel).timestamp())


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, http: aiohttp.ClientSession):
    token = cfg["hemmelig"]["TIBBER_TOKEN"]
    hjem = cfg["tibber"]["home_id"]
    kilde = tilstand.kilder["tibber"]
    teller = Timeteller(db)
    forsok = 0
    while True:
        try:
            url = await _ws_url(http, token)
            async with http.ws_connect(url, protocols=("graphql-transport-ws",), heartbeat=30) as ws:
                await ws.send_json({"type": "connection_init", "payload": {"token": token}})
                svar = await ws.receive_json(timeout=30)
                if svar.get("type") != "connection_ack":
                    raise RuntimeError(f"uventet svar: {svar}")
                await ws.send_json({"id": "1", "type": "subscribe", "payload": {"query": SPORRING % hjem}})
                log.info("koblet til Tibber liveMeasurement")
                while True:
                    msg = await ws.receive(timeout=STILLE_MAKS)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        raise RuntimeError(f"websocket lukket: {msg.type.name}")
                    m = json.loads(msg.data)
                    if m["type"] == "ping":
                        await ws.send_json({"type": "pong"})
                        continue
                    if m["type"] != "next":
                        raise RuntimeError(f"uventet melding: {str(m)[:200]}")
                    if m["payload"].get("errors"):
                        raise RuntimeError(str(m["payload"]["errors"])[:200])
                    d = m["payload"]["data"]["liveMeasurement"]
                    ts = _epoch(d["timestamp"])
                    db.lagre_maling(ts, d["power"], d["accumulatedConsumptionLastHour"], d["lastMeterConsumption"],
                                    d["currentL1"], d["currentL3"],
                                    d["voltagePhase1"], d["voltagePhase2"], d["voltagePhase3"])
                    teller.ny_maling(ts, d["power"], d["accumulatedConsumptionLastHour"], d["lastMeterConsumption"])
                    tilstand.hus = {"ts": ts, "mottatt": time.time(), "effekt_w": d["power"],
                                    "time_kwh": d["accumulatedConsumptionLastHour"],
                                    "maaler_kwh": d["lastMeterConsumption"],
                                    "i1": d["currentL1"], "i3": d["currentL3"],
                                    "u": [d["voltagePhase1"], d["voltagePhase2"], d["voltagePhase3"]]}
                    kilde.ok()
                    forsok = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kilde.feilet(e)
            log.warning("Tibber-feil (forsøk %d): %s", forsok + 1, e)
            await sov_med_backoff(forsok, minst=10, maks=300)
            forsok += 1
