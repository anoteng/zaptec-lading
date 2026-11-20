"""Leser config.toml (standardverdier) og .env (hemmeligheter, ID-er og egne verdier).

Rekkefølge, sist vinner: config.toml → .env → miljøvariabler (f.eks. fra systemd).
Enhver enkeltverdi i config.toml kan overstyres med LADING_<SEKSJON>_<NØKKEL>, f.eks.
LADING_STYRING_MODUS=aktiv eller LADING_ZAPTEC_CHARGER_ID=… (seksjonsnavnene har ikke understrek).
Strømavtale og støtteordning lagres fra websiden i databasen og overstyrer [regning] (se avtale.py).
"""
import os
import tomllib
from pathlib import Path

ROT = Path(__file__).resolve().parent.parent
HEMMELIGHETER = ("ZAPTEC_USER", "ZAPTEC_PASSWORD", "TIBBER_TOKEN", "ELVIA_METERVALUES_TOKEN")
PAAKREVD = (("hemmelig", "ZAPTEC_USER"), ("hemmelig", "ZAPTEC_PASSWORD"), ("hemmelig", "TIBBER_TOKEN"),
            ("tibber", "home_id"), ("zaptec", "installation_id"), ("zaptec", "charger_id"))


def les_env(sti: Path) -> dict[str, str]:
    verdier = {}
    if sti.exists():
        for linje in sti.read_text().splitlines():
            linje = linje.strip()
            if linje and not linje.startswith("#") and "=" in linje:
                k, v = linje.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                verdier[k.strip()] = v
    return verdier


def _tolk(tekst: str, gammel):
    """Gjør teksten fra .env om til samme type som standardverdien i config.toml."""
    if isinstance(gammel, bool):
        if tekst.lower() in ("1", "true", "ja", "on"):
            return True
        if tekst.lower() in ("0", "false", "nei", "off"):
            return False
        raise ValueError(f"forventet ja/nei, fikk {tekst!r}")
    if isinstance(gammel, int):
        return int(tekst)
    if isinstance(gammel, float):
        return float(tekst.replace(",", "."))
    return tekst


def _overstyr(cfg: dict, verdier: dict[str, str]):
    for navn, tekst in verdier.items():
        if not navn.startswith("LADING_") or tekst == "":
            continue
        seksjon, _, nokkel = navn[len("LADING_"):].lower().partition("_")
        if seksjon not in cfg or not isinstance(cfg[seksjon], dict) or not nokkel:
            raise ValueError(f"{navn}: ukjent seksjon «{seksjon}» i config.toml")
        if nokkel in cfg[seksjon] and isinstance(cfg[seksjon][nokkel], (list, dict)):
            raise ValueError(f"{navn}: {seksjon}.{nokkel} er en tabell og må endres i config.toml")
        try:
            cfg[seksjon][nokkel] = _tolk(tekst, cfg[seksjon].get(nokkel, ""))
        except ValueError as e:
            raise ValueError(f"{navn}: {e}") from None


def last(sti: str | None = None, krev: bool = True) -> dict:
    cfg = tomllib.loads(Path(sti or ROT / "config.toml").read_text())
    verdier = les_env(ROT / ".env")
    verdier.update({k: v for k, v in os.environ.items() if k.startswith("LADING_") or k in HEMMELIGHETER})
    _overstyr(cfg, verdier)
    cfg["hemmelig"] = {k: verdier[k] for k in HEMMELIGHETER if verdier.get(k)}
    if krev:
        mangler = [k if s == "hemmelig" else f"LADING_{s}_{k}".upper() for s, k in PAAKREVD if not cfg[s].get(k)]
        if mangler:
            raise SystemExit("Mangler i .env: " + ", ".join(mangler) + ". Se .env.example og README.")
    db = Path(cfg["db"]["sti"])
    cfg["db"]["sti"] = str(db if db.is_absolute() else ROT / db)
    return cfg
