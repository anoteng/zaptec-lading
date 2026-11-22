"""Start: python -m lading [--config sti]"""
import argparse
import asyncio
import logging
import signal

import aiohttp

from . import avtale, config, effektvarsel, elvia, priser, styring, tibber, varmepumpe, varsel, web, zaptec
from .db import DB
from .tilstand import Tilstand

log = logging.getLogger("lading")


async def main(cfg: dict):
    db = DB(cfg["db"]["sti"])
    avtale.bruk(cfg, db)  # strømavtale og støtteordning lagret fra websiden
    tilstand = Tilstand()
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "lading/0.1"}) as http:
        varsel.oppsett(cfg, http)
        await web.kjor(cfg, db, tilstand)
        oppgaver = [asyncio.create_task(m.kjor(cfg, db, tilstand, http), name=m.__name__)
                    for m in (tibber, zaptec, priser, elvia, styring, effektvarsel, varmepumpe)]
        stopp = asyncio.Event()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stopp.set)
        ferdig = asyncio.create_task(stopp.wait())
        done, _ = await asyncio.wait([ferdig, *oppgaver], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not ferdig and t.exception():
                log.error("oppgave %s stoppet: %r", t.get_name(), t.exception())
        for t in oppgaver:
            t.cancel()
        await asyncio.gather(*oppgaver, return_exceptions=True)
        log.info("avsluttet")


def cli():
    p = argparse.ArgumentParser(prog="lading")
    p.add_argument("--config")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    asyncio.run(main(config.last(a.config)))


if __name__ == "__main__":
    cli()
