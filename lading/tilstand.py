"""Felles tilstand mellom oppgavene, og en hjelper for løkker som skal overleve feil."""
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

log = logging.getLogger("lading")


@dataclass
class Kilde:
    sist_ok: float | None = None
    sist_feil: float | None = None
    feil: str | None = None
    info: str | None = None

    def ok(self, info: str | None = None):
        self.sist_ok = time.time()
        self.feil = None
        if info is not None:
            self.info = info

    def feilet(self, e: BaseException | str):
        self.sist_feil = time.time()
        self.feil = str(e)[:300] or type(e).__name__


@dataclass
class Tilstand:
    hus: dict | None = None            # siste Pulse-måling
    lader: dict | None = None          # siste Zaptec-status
    installasjon: dict | None = None   # siste installasjonsinfo
    styring: dict | None = None        # siste beslutning fra styringen
    effektvarsel: dict | None = None   # siste vurdering av om timen øker kapasitetstrinnet
    varmepumpe: dict | None = None     # siste status fra MELCloud Home
    oppfrisk: asyncio.Event = field(default_factory=asyncio.Event)  # be Zaptec-løkka hente status nå
    styr_naa: asyncio.Event = field(default_factory=asyncio.Event)  # be styringen regne på nytt nå
    kilder: dict[str, Kilde] = field(default_factory=lambda: {
        k: Kilde() for k in ("tibber", "zaptec", "priser", "elvia", "varmepumpe", "vaer")})


async def sov_med_backoff(forsok: int, minst: float = 10, maks: float = 600):
    vent = min(maks, minst * 2 ** min(forsok, 10)) * random.uniform(0.8, 1.2)
    await asyncio.sleep(vent)
