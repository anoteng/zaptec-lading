"""Push-varsel via serverens varsle-skript (ntfy)."""
import asyncio
import logging
import shutil

log = logging.getLogger("lading.varsel")
SKRIPT = "/usr/local/bin/varsle"


async def send(tittel: str, tekst: str, prioritet: str = "default", tagger: str = "electric_plug"):
    if not shutil.which(SKRIPT):
        log.warning("varsle finnes ikke: %s: %s", tittel, tekst)
        return
    try:
        p = await asyncio.create_subprocess_exec(SKRIPT, tittel, tekst, prioritet, tagger,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), 60)
    except Exception as e:
        log.warning("varsel feilet: %s", e)
