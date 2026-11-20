"""Push-varsel til ntfy.

Er NTFY_URL satt i .env (f.eks. https://ntfy.sh/mitt-hemmelige-topic), sendes varselet dit direkte, med
NTFY_TOKEN som tilgangstoken hvis serveren krever det. Ellers brukes kommandoen `varsle` hvis den finnes
(`varsle "Tittel" "Melding" [prioritet] [tagger]`), og ellers logges varselet bare.
"""
import asyncio
import logging
import shutil
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger("lading.varsel")
SKRIPT = "/usr/local/bin/varsle"
PRIORITET = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}

_oppsett: dict = {"url": None, "token": None, "lenke": None, "http": None}


def oppsett(cfg: dict, http: aiohttp.ClientSession):
    h = cfg["hemmelig"]
    _oppsett.update(url=h.get("NTFY_URL"), token=h.get("NTFY_TOKEN"),
                    lenke=cfg.get("varsel", {}).get("lenke") or None, http=http)
    if _oppsett["url"]:
        log.info("varsler går til egen ntfy-topic")
    elif shutil.which(SKRIPT):
        log.info("varsler går via %s", SKRIPT)
    else:
        log.info("ingen varselkanal satt opp (NTFY_URL); varsler logges bare")


async def _ntfy(tittel: str, tekst: str, prioritet: str, tagger: str):
    # JSON-publisering: tål æøå i tittelen (HTTP-headere bør være ASCII).
    u = urlsplit(_oppsett["url"])
    data = {"topic": u.path.strip("/"), "title": tittel, "message": tekst,
            "priority": PRIORITET.get(prioritet, 3), "tags": [t for t in tagger.split(",") if t]}
    if _oppsett["lenke"]:
        data["click"] = _oppsett["lenke"]
    headere = {"Authorization": f"Bearer {_oppsett['token']}"} if _oppsett["token"] else {}
    http = _oppsett["http"]
    async with http.post(f"{u.scheme}://{u.netloc}/", json=data, headers=headere,
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status >= 300:
            raise RuntimeError(f"ntfy svarte HTTP {r.status}")


async def send(tittel: str, tekst: str, prioritet: str = "default", tagger: str = "electric_plug"):
    log.info("varsel [%s] %s: %s", prioritet, tittel, tekst)
    try:
        if _oppsett["url"] and _oppsett["http"]:
            await _ntfy(tittel, tekst, prioritet, tagger)
            return
        if not shutil.which(SKRIPT):
            return
        p = await asyncio.create_subprocess_exec(SKRIPT, tittel, tekst, prioritet, tagger,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), 60)
    except Exception as e:
        log.warning("varsel feilet: %s", e)
