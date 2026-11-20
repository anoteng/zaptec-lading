"""Finner ID-ene som skal i .env: Tibber-hjem og Zaptec-lader/installasjon. Bare lesing.

Bruk: .venv/bin/python -m lading.finn   (krever ZAPTEC_USER, ZAPTEC_PASSWORD og TIBBER_TOKEN i .env)
"""
import asyncio

import aiohttp

from . import config
from .tibber import API as TIBBER_API
from .zaptec import Zaptec

HJEM = """{ viewer { homes { id appNickname address { address1 postalCode city }
  features { realTimeConsumptionEnabled } } } }"""


async def tibber(http: aiohttp.ClientSession, token: str):
    async with http.post(TIBBER_API, json={"query": HJEM}, headers={"Authorization": f"Bearer {token}"}) as r:
        r.raise_for_status()
        hjem = (await r.json())["data"]["viewer"]["homes"]
    print("Tibber-hjem (velg det med Pulse, «sanntid: ja»):")
    for h in hjem:
        a = h.get("address") or {}
        sanntid = (h.get("features") or {}).get("realTimeConsumptionEnabled")
        print(f"  LADING_TIBBER_HOME_ID={h['id']}")
        print(f"      {h.get('appNickname') or ''} {a.get('address1') or ''}, {a.get('postalCode') or ''} "
              f"{a.get('city') or ''} · sanntid: {'ja' if sanntid else 'nei'}")


async def zaptec(http: aiohttp.ClientSession, bruker: str, passord: str):
    z = Zaptec(http, bruker, passord)
    ladere = (await z.get("/api/chargers")).get("Data", [])
    print("Zaptec-ladere:")
    for c in ladere:
        print(f"  LADING_ZAPTEC_CHARGER_ID={c.get('Id')}")
        print(f"  LADING_ZAPTEC_INSTALLATION_ID={c.get('InstallationId')}")
        print(f"      {c.get('Name') or ''} · installasjon {c.get('InstallationName') or ''} · {c.get('DeviceId') or ''}")


async def main():
    cfg = config.last(krev=False)
    h = cfg["hemmelig"]
    async with aiohttp.ClientSession(headers={"User-Agent": "lading/0.1"}) as http:
        if h.get("TIBBER_TOKEN"):
            await tibber(http, h["TIBBER_TOKEN"])
        else:
            print("TIBBER_TOKEN mangler i .env")
        print()
        if h.get("ZAPTEC_USER") and h.get("ZAPTEC_PASSWORD"):
            await zaptec(http, h["ZAPTEC_USER"], h["ZAPTEC_PASSWORD"])
        else:
            print("ZAPTEC_USER/ZAPTEC_PASSWORD mangler i .env")


if __name__ == "__main__":
    asyncio.run(main())
