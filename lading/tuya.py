"""Luftfukter og luftavfukter (Tuya/Smart Life) styrt lokalt på hjemmenettet.

Mange apparater fra Wilfa, Jula/Anslut, Clas Ohlson m.fl. er Tuya-enheter. De snakker en kryptert protokoll på
LAN-et (biblioteket tinytuya), men nøkkelen per enhet («local key») finnes bare i Tuyas sky. Den hentes én gang:

1. Enhetene legges i Smart Life-appen (Wilfas egen app, WiLife, gir ikke tilgang).
2. Brukerkoden fra Smart Life (Meg → ⚙ → Konto og sikkerhet → Brukerkode) legges inn på websiden.
3. Websiden viser en QR-kode som skannes med Smart Life. Tjenesten henter enhetsliste og nøkler, logger ut av
   skyen igjen og finner enhetene på nettet. Nøklene lagres i .env (TUYA_NOKLER), resten i databasen.

Innloggingen bruker samme mekanisme og klient-ID som Home Assistants Tuya-integrasjon (tuya-device-sharing-sdk).
Etter det går status og styring direkte mot apparatene, uten sky. Nøkkelen endres hvis et apparat pares på nytt;
da må innloggingen gjøres om igjen.

Luftavfukteren har kompressor: koden slår den aldri av eller på som bieffekt, og websiden ber om bekreftelse.
"""
import asyncio
import json
import logging
import time

import segno
import tinytuya
from tuya_sharing import LoginControl, Manager

from . import config
from .db import DB
from .tid import time_av
from .tilstand import Tilstand

log = logging.getLogger("lading.tuya")
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"   # Home Assistants klient-ID for Smart Life-deling
SCHEMA = "haauthorize"
SJEKK_SEK = 30
FINN_ETTER_FEIL = 3                  # så mange feil på rad før enheten letes etter på nettet (ny IP?)
OPPFRISK_ETTER_SEK = 2
QR_GYLDIG_SEK = 300
HISTORIKK_TIMER = 48
META = "tuya_enheter"

# Kjente modeller (datapunkter fra tuya-local, github.com/make-all/tuya-local). Ukjente vises bare med rådata.
PROFILER = {
    "7naqdm8whbezn28v": {
        "type": "luftfukter", "modell": "Wilfa Haze HU-400BC",
        "paa": 1, "fukt": 14, "temp": 10,
        "maal": {"dp": 13, "min": 30, "maks": 90, "steg": 5, "ikke_i_modus": ["auto", "manual"]},
        "modus": {"dp": 24, "valg": {"humidity": "Konstant fuktighet", "manual": "Manuell", "auto": "Auto"}},
        "niva": {"dp": 23, "navn": "Tåke", "valg": {f"level_{i}": str(i) for i in range(1, 6)}},
        "brytere": {"varme": (26, "Varme"), "ionisator": (35, "Ionisator"), "lys": (5, "Stemningslys"), "lyd": (8, "Lyd")},
        "feil": {"dp": 22, "bits": {1: "tanken er tom"}},
    },
    "36sgz5ez8f4kaap7": {
        "type": "avfukter", "modell": "Anslut avfukter 20 l", "kompressor": True,
        "paa": 1, "fukt": 6, "temp": 7,
        "maal": {"dp": 2, "min": 30, "maks": 80, "steg": 5},
        "modus": {"dp": 5, "valg": {"Auto": "Auto", "Continuities": "Sterk", "Sleep": "Søvn"}},
        "niva": {"dp": 4, "navn": "Vifte", "valg": {"low": "Lav", "high": "Høy"}},
        "brytere": {"barnesikring": (16, "Barnesikring")},
        "feil": {"dp": 19, "bits": {1: "tanken er full", 2: "filteret må rengjøres", 4: "E1: feil på temperaturføleren",
                                    8: "CL: for kaldt", 16: "CH: for varmt", 32: "LO: for lav luftfuktighet",
                                    64: "feil på kjøleelementet", 128: "feil på viften"}},
        "filter": 23,
    },
}

_aktiv: "Tuya | None" = None


# --- nøkler i .env: «id:hex(nøkkel),…» (hex, så tegn i nøkkelen ikke kan ødelegge fila) ---
def les_nokler(cfg: dict) -> dict[str, str]:
    ut = {}
    for del_ in (cfg["hemmelig"].get("TUYA_NOKLER") or "").split(","):
        if ":" in del_:
            i, h = del_.split(":", 1)
            try:
                ut[i.strip()] = bytes.fromhex(h.strip()).decode()
            except ValueError:
                log.warning("ugyldig nøkkel for %s i TUYA_NOKLER", i)
    return ut


def _skriv_nokler(cfg: dict, nokler: dict[str, str] | None):
    verdi = ",".join(f"{i}:{k.encode().hex()}" for i, k in nokler.items()) if nokler else None
    config.skriv_env({"TUYA_NOKLER": verdi})
    if verdi:
        cfg["hemmelig"]["TUYA_NOKLER"] = verdi
    else:
        cfg["hemmelig"].pop("TUYA_NOKLER", None)


# --- profiler og verdier ---
def _profil(meta: dict) -> dict | None:
    p = PROFILER.get(meta.get("produkt") or "")
    if not p:
        return None
    p = json.loads(json.dumps(p))  # kopi; tupler blir lister
    # Begrens valgene til det skyen sier enheten faktisk har, når det er kjent.
    for felt in ("modus", "niva"):
        if felt in p:
            lov = (meta.get("dps") or {}).get(str(p[felt]["dp"]), {}).get("valg")
            if lov:
                p[felt]["valg"] = {k: v for k, v in p[felt]["valg"].items() if k in lov} or p[felt]["valg"]
    return p


def _tall(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _enhet(meta: dict, dps: dict, online: bool, sist_ok: float | None) -> dict:
    p = _profil(meta)
    ut = {"id": meta["id"], "navn": meta.get("navn") or meta["id"], "online": online, "sist_ok": sist_ok,
          "type": p["type"] if p else "ukjent", "modell": p["modell"] if p else meta.get("produktnavn")}
    if not p:
        koder = meta.get("dps") or {}
        ut["raa"] = {koder.get(k, {}).get("kode") or k: v for k, v in dps.items()}
        return ut
    g = lambda dp: dps.get(str(dp))  # noqa: E731
    feil = g(p["feil"]["dp"]) if "feil" in p else None
    varsler = []
    if isinstance(feil, int) and feil:
        varsler = [t for b, t in p["feil"]["bits"].items() if feil & int(b)]
        rest = feil & ~sum(int(b) for b in p["feil"]["bits"])
        if rest:
            varsler.append(f"feilkode {rest}")
    ut.update({
        "paa": g(p["paa"]) if g(p["paa"]) is not None else None,
        "fukt": _tall(g(p["fukt"])), "temp": _tall(g(p["temp"])),
        "maal": _tall(g(p["maal"]["dp"])) if "maal" in p else None,
        "modus": g(p["modus"]["dp"]) if "modus" in p else None,
        "niva": g(p["niva"]["dp"]) if "niva" in p else None,
        "brytere": {k: g(dp) for k, (dp, _) in p.get("brytere", {}).items() if g(dp) is not None},
        "varsler": varsler,
        "filter": _tall(g(p["filter"])) if "filter" in p else None,
        "evner": {"maal": {k: p["maal"][k] for k in ("min", "maks", "steg")} | {"ikke_i_modus": p["maal"].get("ikke_i_modus", [])}
                  if "maal" in p else None,
                  "moduser": p.get("modus", {}).get("valg", {}), "nivaer": p.get("niva", {}).get("valg", {}),
                  "niva_navn": p.get("niva", {}).get("navn"),
                  "brytere": {k: navn for k, (_, navn) in p.get("brytere", {}).items()},
                  "kompressor": bool(p.get("kompressor"))},
    })
    return ut


def valider(meta: dict, naa: dict, d: dict) -> dict[str, object]:
    """Gjør en kommando fra API-et om til datapunkter. Kaster ValueError ved ukjente eller ugyldige verdier."""
    p = _profil(meta)
    if not p:
        raise ValueError("enheten er ikke en kjent modell og kan bare leses")
    ut: dict[str, object] = {}
    for k, v in d.items():
        if k == "paa":
            if not isinstance(v, bool):
                raise ValueError("paa må være true eller false")
            ut[str(p["paa"])] = v
        elif k == "modus" and "modus" in p:
            if v not in p["modus"]["valg"]:
                raise ValueError(f"modus må være en av {', '.join(p['modus']['valg'])}")
            ut[str(p["modus"]["dp"])] = v
        elif k == "niva" and "niva" in p:
            if v not in p["niva"]["valg"]:
                raise ValueError(f"niva må være en av {', '.join(p['niva']['valg'])}")
            ut[str(p["niva"]["dp"])] = v
        elif k == "maal" and "maal" in p:
            m = p["maal"]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not m["min"] <= v <= m["maks"] or v % m["steg"]:
                raise ValueError(f"maal må være {m['min']}–{m['maks']} % i steg på {m['steg']}")
            modus = d.get("modus", naa.get("modus"))
            if modus in m.get("ikke_i_modus", []):
                raise ValueError(f"ønsket luftfuktighet kan ikke settes i modus {modus}")
            ut[str(m["dp"])] = int(v)
        elif k in p.get("brytere", {}):
            if not isinstance(v, bool):
                raise ValueError(f"{k} må være true eller false")
            ut[str(p["brytere"][k][0])] = v
        else:
            raise ValueError(f"ukjent innstilling «{k}»")
    if not ut:
        raise ValueError("ingen endring")
    return ut


# --- innlogging via Smart Life (QR) ---
def _qr_svg(tekst: str) -> str:
    return segno.make(tekst, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")


def _hent_fra_sky(kode: str, info: dict) -> list[dict]:
    """Enhetsliste med nøkler og datapunkt-beskrivelser fra Smart Life. Logger ut av skyen etterpå."""
    tok = {k: info[k] for k in ("t", "uid", "expire_time", "access_token", "refresh_token")}
    m = Manager(CLIENT_ID, kode, info["terminal_id"], info["endpoint"], tok, None)
    try:
        m.update_device_cache()
        ut = []
        for dev in m.device_map.values():
            dps = {}
            for dp, s in (getattr(dev, "local_strategy", None) or {}).items():
                ci = s.get("config_item") or {}
                valg = None
                try:
                    desc = json.loads(ci.get("valueDesc") or "{}")
                    valg = desc.get("range") if isinstance(desc, dict) else None
                except (TypeError, ValueError):
                    pass
                dps[str(dp)] = {"kode": s.get("status_code"), "type": ci.get("valueType"), "valg": valg}
            ut.append({"id": dev.id, "navn": dev.name, "nokkel": dev.local_key, "produkt": dev.product_id,
                       "produktnavn": dev.product_name, "kategori": dev.category, "dps": dps})
        return ut
    finally:
        try:
            m.user_repository.unload(info["terminal_id"])
        except Exception as e:  # ikke kritisk: tilgangen utløper av seg selv
            log.info("utlogging fra Smart Life feilet: %s", type(e).__name__)


def _finn_pa_nett() -> dict[str, dict]:
    """Lytt etter kringkastinger fra Tuya-enheter (~20 s). Gir {id: {ip, versjon, produkt}}."""
    funnet = tinytuya.deviceScan(verbose=False, maxretry=20)
    return {v.get("gwId") or v.get("id"): {"ip": v.get("ip"), "versjon": v.get("version"), "produkt": v.get("productKey")}
            for v in funnet.values()}


class Tuya:
    def __init__(self, cfg: dict, db: DB, tilstand: Tilstand):
        self.cfg, self.db, self.tilstand = cfg, db, tilstand
        self.kilde = tilstand.kilder["tuya"]
        self.meta: list[dict] = db.hent(META, []) or []
        self.dps: dict[str, dict] = {}
        self.feil: dict[str, int] = {}
        self.sist_ok: dict[str, float] = {}
        self.enheter: dict[str, tinytuya.Device] = {}
        self.laas = asyncio.Lock()
        self.vekk = asyncio.Event()
        self.login: dict | None = None   # pågående QR-innlogging {kode, token, qr, startet}
        self.login_feil: str | None = None

    # --- innlogging ---
    def innlogging(self) -> dict:
        nokler = les_nokler(self.cfg)
        ut = {"satt": bool(nokler), "enheter": [{"id": m["id"], "navn": m.get("navn"), "modell": (PROFILER.get(m.get("produkt") or "") or {}).get("modell") or m.get("produktnavn"),
                                                   "ip": m.get("ip"), "nokkel": m["id"] in nokler} for m in self.meta]}
        if self.login:
            ut["venter"] = {"qr": _qr_svg(self.login["qr"]), "gyldig_til": self.login["startet"] + QR_GYLDIG_SEK}
        if self.login_feil:
            ut["feil"] = self.login_feil
        return ut

    async def start_innlogging(self, kode: str) -> dict:
        kode = (kode or "").strip()
        if not kode or len(kode) > 64 or not kode.replace("-", "").replace("_", "").isalnum():
            raise ValueError("lim inn brukerkoden fra Smart Life (Meg → ⚙ → Konto og sikkerhet → Brukerkode)")
        try:
            r = await asyncio.to_thread(LoginControl().qr_code, CLIENT_ID, SCHEMA, kode)
        except Exception as e:
            raise RuntimeError(f"fikk ikke kontakt med Smart Life ({type(e).__name__})") from None
        if not r.get("success"):
            raise ValueError(f"Smart Life avviste brukerkoden: {r.get('msg') or r.get('code') or 'ukjent feil'}")
        token = r["result"]["qrcode"]
        self.login = {"kode": kode, "token": token, "qr": f"tuyaSmart--qrLogin?token={token}", "startet": time.time()}
        self.login_feil = None
        return self.innlogging()

    async def sjekk_innlogging(self) -> dict:
        """Kalles når websiden spør om status: sjekk om QR-koden er skannet, og fullfør i så fall."""
        lg = self.login
        if not lg:
            return self.innlogging()
        if time.time() > lg["startet"] + QR_GYLDIG_SEK:
            self.login, self.login_feil = None, "QR-koden gikk ut. Prøv igjen."
            return self.innlogging()
        try:
            ok, info = await asyncio.to_thread(LoginControl().login_result, lg["token"], CLIENT_ID, lg["kode"])
        except Exception as e:
            log.info("Smart Life-innlogging: %s", type(e).__name__)
            return self.innlogging()
        if not ok:
            return self.innlogging()   # ikke skannet ennå
        self.login = None
        try:
            enheter = await asyncio.to_thread(_hent_fra_sky, lg["kode"], info)
        except Exception as e:
            log.warning("henting av enheter fra Smart Life feilet: %s", type(e).__name__)
            self.login_feil = f"innloggingen virket, men enhetslista kunne ikke hentes ({type(e).__name__})"
            return self.innlogging()
        nett = await asyncio.to_thread(_finn_pa_nett)
        meta, nokler = [], {}
        for e in enheter:
            if not e.get("nokkel"):
                continue
            n = nett.get(e["id"], {})
            nokler[e["id"]] = e.pop("nokkel")
            meta.append({**e, "ip": n.get("ip"), "versjon": n.get("versjon")})
        _skriv_nokler(self.cfg, nokler)
        self.db.lagre(META, meta)
        self.meta, self.enheter, self.dps, self.feil = meta, {}, {}, {}
        log.info("Smart Life: %d enheter lagret (%d funnet på nettet)", len(meta), sum(1 for m in meta if m.get("ip")))
        self.vekk.set()
        return {**self.innlogging(), "ferdig": True}

    def fjern(self) -> dict:
        _skriv_nokler(self.cfg, None)
        self.db.lagre(META, [])
        self.meta, self.enheter, self.dps, self.login = [], {}, {}, None
        self.tilstand.tuya = None
        log.info("Smart Life-enheter og nøkler fjernet")
        return self.innlogging()

    # --- lokal kontakt ---
    def _enhet(self, m: dict) -> tinytuya.Device | None:
        nokkel = les_nokler(self.cfg).get(m["id"])
        if not nokkel or not m.get("ip"):
            return None
        d = self.enheter.get(m["id"])
        if d is None or d.address != m["ip"]:
            d = tinytuya.Device(m["id"], m["ip"], nokkel, version=float(m.get("versjon") or 3.3))
            d.set_socketTimeout(5)
            d.set_socketRetryLimit(1)
            self.enheter[m["id"]] = d
        return d

    async def _les(self, m: dict):
        d = self._enhet(m)
        if d is None:
            raise RuntimeError("ikke funnet på nettet" if not m.get("ip") else "mangler nøkkel")
        r = await asyncio.to_thread(d.status)
        if not isinstance(r, dict) or "dps" not in r:
            raise RuntimeError((r or {}).get("Error") or "ukjent svar")
        self.dps.setdefault(m["id"], {}).update(r["dps"])

    async def _let_etter(self, m: dict):
        r = await asyncio.to_thread(tinytuya.find_device, m["id"])
        if r.get("ip") and (r["ip"] != m.get("ip") or str(r.get("version")) != str(m.get("versjon"))):
            log.info("%s funnet på %s (v%s)", m.get("navn"), r["ip"], r.get("version"))
            m["ip"], m["versjon"] = r["ip"], r.get("version")
            self.db.lagre(META, self.meta)

    def _logg_time(self, e: dict):
        if e.get("fukt") is None and e.get("temp") is None:
            return
        t = time_av(time.time())
        r = self.db.en("SELECT fukt, temp, paa, n FROM time_fukt WHERE time = ? AND enhet = ?", t, e["id"])
        n = r["n"] if r else 0
        snitt = lambda gml, ny: ny if gml is None else gml if ny is None else (gml * n + ny) / (n + 1)  # noqa: E731
        paa = None if e.get("paa") is None else float(bool(e["paa"]))
        self.db.x("INSERT OR REPLACE INTO time_fukt VALUES (?, ?, ?, ?, ?, ?)", t, e["id"],
                  snitt(r["fukt"] if r else None, e.get("fukt")), snitt(r["temp"] if r else None, e.get("temp")),
                  snitt(r["paa"] if r else None, paa), n + 1)

    async def oppdater(self):
        if not self.meta:
            self.kilde.info = "ikke satt opp (logg inn med Smart Life på websiden)"
            self.tilstand.tuya = None
            return
        ok = 0
        for m in self.meta:
            try:
                async with self.laas:
                    await self._les(m)
                self.feil[m["id"]] = 0
                self.sist_ok[m["id"]] = time.time()
                ok += 1
            except Exception as e:
                self.feil[m["id"]] = self.feil.get(m["id"], 0) + 1
                log.debug("%s: %s", m.get("navn"), e)
                if self.feil[m["id"]] % FINN_ETTER_FEIL == 0 or not m.get("ip"):
                    await self._let_etter(m)
            if self.feil.get(m["id"]) == 0:
                self._logg_time(_enhet(m, self.dps.get(m["id"], {}), True, self.sist_ok.get(m["id"])))
        self._publiser()
        navn = ", ".join(m.get("navn") or m["id"] for m in self.meta if self.feil.get(m["id"]))
        if ok:
            self.kilde.ok(f"{ok} av {len(self.meta)} enheter svarer lokalt" + (f" (ikke: {navn})" if navn else ""))
        else:
            self.kilde.feilet(f"ingen enheter svarer ({navn})")

    def _publiser(self):
        self.tilstand.tuya = {"ts": time.time(), "enheter": [
            _enhet(m, self.dps.get(m["id"], {}), not self.feil.get(m["id"]), self.sist_ok.get(m["id"])) for m in self.meta]}

    async def styr(self, enhet_id: str | None, d: dict) -> dict:
        m = next((m for m in self.meta if m["id"] == enhet_id), None) if enhet_id else (self.meta[0] if len(self.meta) == 1 else None)
        if m is None:
            raise ValueError("ukjent enhet" if enhet_id else "oppgi enhet")
        naa = _enhet(m, self.dps.get(m["id"], {}), True, None)
        dps = valider(m, naa, d)
        dev = self._enhet(m)
        if dev is None:
            raise RuntimeError("enheten er ikke funnet på nettet")
        # Wilfa setter ønsket fuktighet til 30 % ved modusbytte; send den gamle verdien på nytt etterpå.
        p = _profil(m)
        behold = None
        if "modus" in d and "maal" not in d and p and "maal" in p and naa.get("maal") is not None \
                and d["modus"] not in p["maal"].get("ikke_i_modus", []):
            behold = {str(p["maal"]["dp"]): int(naa["maal"])}
        for kommando in (dps, behold):
            if not kommando:
                continue
            async with self.laas:
                r = await asyncio.to_thread(dev.set_multiple_values, kommando)
            if isinstance(r, dict) and r.get("Error"):
                raise RuntimeError(f"{m.get('navn')} svarte ikke: {r['Error']}")
            # Svaret kan mangle noen av datapunktene; det som ble sendt, gjelder til neste avlesning.
            self.dps.setdefault(m["id"], {}).update(kommando)
            if isinstance(r, dict) and isinstance(r.get("dps"), dict):
                self.dps[m["id"]].update({k: v for k, v in r["dps"].items() if k not in kommando})
            if behold and kommando is dps:
                await asyncio.sleep(1)
        log.info("%s: %s", m.get("navn"), d)
        self._publiser()
        self.vekk.set()
        return _enhet(m, self.dps[m["id"]], True, time.time())


def historikk(db: DB, til: int) -> dict[str, list]:
    """Timesnitt av luftfuktighet og temperatur per enhet, de siste 48 timene: {id: [[time, fukt, temp, andel på], …]}."""
    ut: dict[str, list] = {}
    for r in db.alle("SELECT * FROM time_fukt WHERE time >= ? ORDER BY time", til - HISTORIKK_TIMER * 3600):
        ut.setdefault(r["enhet"], []).append([r["time"], None if r["fukt"] is None else round(r["fukt"], 1),
                                              None if r["temp"] is None else round(r["temp"], 1),
                                              None if r["paa"] is None else round(r["paa"], 2)])
    return ut


def _krev() -> "Tuya":
    if _aktiv is None:
        raise ValueError("Tuya-tjenesten er ikke startet")
    return _aktiv


def innlogging() -> dict:
    return _krev().innlogging()


async def sjekk_innlogging() -> dict:
    return await _krev().sjekk_innlogging()


async def start_innlogging(kode: str) -> dict:
    return await _krev().start_innlogging(kode)


def fjern() -> dict:
    return _krev().fjern()


async def styr(enhet_id: str | None, d: dict) -> dict:
    return await _krev().styr(enhet_id, d)


async def kjor(cfg: dict, db: DB, tilstand: Tilstand, http=None):
    global _aktiv
    t = _aktiv = Tuya(cfg, db, tilstand)
    while True:
        etter_kommando = t.vekk.is_set()
        t.vekk.clear()
        if etter_kommando:
            await asyncio.sleep(OPPFRISK_ETTER_SEK)
        try:
            await t.oppdater()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("tuya: %s", e)
            t.kilde.feilet(e)
        try:
            await asyncio.wait_for(t.vekk.wait(), SJEKK_SEK)
        except asyncio.TimeoutError:
            pass
