"""Webgrensesnitt: statusside, JSON-API og overstyring av ladingen."""
import logging
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import avtale, elbil, regning, status, styring, varmepumpe
from .db import DB
from .tilstand import Tilstand

log = logging.getLogger("lading.web")
STATISK = Path(__file__).parent / "static"


LOKALE = {"127.0.0.1", "localhost", "::1"}


def tillatte_verter(cfg: dict) -> set[str]:
    wc = cfg["web"]
    verter = set(LOKALE) | {v.strip().lower() for v in str(wc.get("verter", "")).split(",") if v.strip()}
    if wc["host"] not in ("0.0.0.0", "::", ""):
        verter.add(wc["host"].lower())
    return verter


def lag_app(cfg: dict, db: DB, tilstand: Tilstand) -> web.Application:
    verter, avvist = tillatte_verter(cfg), set()

    @web.middleware
    async def vertsjekk(req: web.Request, handler):
        # Uten innlogging må vi i det minste avvise ukjente vertsnavn: ellers kan en ondsinnet nettside
        # bruke DNS rebinding til å nå 127.0.0.1 fra en nettleser på samme maskin.
        try:
            vert = (urlsplit("//" + (req.host or "")).hostname or "").lower()
        except ValueError:
            vert = ""
        if vert not in verter:
            if vert not in avvist and len(avvist) < 100:
                avvist.add(vert)
                log.warning("avviste forespørsel med ukjent vertsnavn %r (legg det i LADING_WEB_VERTER hvis det er ønsket)", vert)
            return web.json_response({"feil": "ukjent vertsnavn"}, status=421)
        return await handler(req)

    async def forside(_):
        return web.FileResponse(STATISK / "index.html", headers={"Cache-Control": "no-cache"})

    async def api_status(_):
        data = status.bygg(db, cfg, tilstand)
        data["styring"] = {**(tilstand.styring or {}), "modus": cfg["styring"]["modus"], "logg": styring.logg(db)}
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    def ikke_csrf(req: web.Request) -> bool:
        # Krev egen header og JSON: hindrer at andre nettsider kan poste hit via nettleseren (CSRF).
        return req.headers.get("X-Lading") == "1" and req.content_type == "application/json"

    async def api_overstyring(req: web.Request):
        if not ikke_csrf(req):
            return web.json_response({"feil": "mangler X-Lading-header"}, status=403)
        try:
            d = await req.json()
            o = styring.sett_overstyring(db, cfg, d.get("type"), d.get("timer"))
        except (ValueError, TypeError) as e:
            return web.json_response({"feil": str(e)}, status=400)
        log.info("overstyring: %s", o or "auto")
        tilstand.styr_naa.set()
        return web.json_response({"overstyring": o})

    async def api_regning(_):
        data = {**regning.bygg(db, cfg, tilstand), "elbil": elbil.bygg(db, cfg, tilstand)}
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    async def api_innstillinger(_):
        return web.json_response(avtale.hent(cfg), headers={"Cache-Control": "no-store"})

    async def api_innstillinger_lagre(req: web.Request):
        if not ikke_csrf(req):
            return web.json_response({"feil": "mangler X-Lading-header"}, status=403)
        try:
            d = await req.json()
            if isinstance(d, dict) and d.get("tilbakestill"):
                ut = avtale.tilbakestill(cfg, db)
            else:
                ut = avtale.lagre(cfg, db, d)
        except (ValueError, TypeError, KeyError) as e:
            return web.json_response({"feil": str(e)}, status=400)
        regning.nullstill()
        log.info("strømavtale/støtte lagret fra websiden (%s)", ut["kilde"])
        return web.json_response(ut)

    async def api_vp_innlogging(_):
        return web.json_response(varmepumpe.innlogging(cfg), headers={"Cache-Control": "no-store"})

    async def api_vp_innlogging_lagre(req: web.Request):
        # Passordet sendes hit fra skjemaet på websiden, lagres i .env og gis aldri tilbake.
        if not ikke_csrf(req):
            return web.json_response({"feil": "mangler X-Lading-header"}, status=403)
        try:
            d = await req.json()
            if not isinstance(d, dict):
                raise ValueError("forventet et JSON-objekt")
            if d.get("fjern"):
                ut = varmepumpe.fjern_innlogging(cfg)
            else:
                ut = await varmepumpe.lagre_innlogging(cfg, str(d.get("bruker") or ""), str(d.get("passord") or ""))
        except (ValueError, TypeError) as e:
            return web.json_response({"feil": str(e)}, status=400)
        return web.json_response(ut, headers={"Cache-Control": "no-store"})

    async def api_vp_styr(req: web.Request):
        if not ikke_csrf(req):
            return web.json_response({"feil": "mangler X-Lading-header"}, status=403)
        try:
            d = await req.json()
            if not isinstance(d, dict):
                raise ValueError("forventet et JSON-objekt")
            enhet = d.pop("enhet", None)
            ut = await varmepumpe.styr(enhet, d)
        except (ValueError, TypeError) as e:
            return web.json_response({"feil": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"feil": str(e)}, status=502)
        return web.json_response(ut)

    async def api_vp_program(req: web.Request):
        if not ikke_csrf(req):
            return web.json_response({"feil": "mangler X-Lading-header"}, status=403)
        try:
            d = await req.json()
            if not isinstance(d, dict):
                raise ValueError("forventet et JSON-objekt")
            ut = await varmepumpe.program(d.pop("enhet", None), d)
        except (ValueError, TypeError) as e:
            return web.json_response({"feil": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"feil": str(e)}, status=502)
        return web.json_response(ut)

    async def helse(_):
        k = tilstand.kilder
        ok = all(v.feil is None and v.sist_ok for v in (k["tibber"], k["zaptec"]))
        return web.json_response({"ok": ok}, status=200 if ok else 503)

    app = web.Application(middlewares=[vertsjekk])
    app.router.add_get("/", forside)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/regning", api_regning)
    app.router.add_post("/api/overstyring", api_overstyring)
    app.router.add_get("/api/innstillinger", api_innstillinger)
    app.router.add_post("/api/innstillinger", api_innstillinger_lagre)
    app.router.add_get("/api/varmepumpe/innlogging", api_vp_innlogging)
    app.router.add_post("/api/varmepumpe/innlogging", api_vp_innlogging_lagre)
    app.router.add_post("/api/varmepumpe", api_vp_styr)
    app.router.add_post("/api/varmepumpe/program", api_vp_program)
    app.router.add_get("/helse", helse)
    return app


async def kjor(cfg: dict, db: DB, tilstand: Tilstand):
    runner = web.AppRunner(lag_app(cfg, db, tilstand), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg["web"]["host"], cfg["web"]["port"])
    await site.start()
    log.info("web på http://%s:%s", cfg["web"]["host"], cfg["web"]["port"])
