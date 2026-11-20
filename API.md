# API for assistenter og skript

Denne beskrivelsen er skrevet for en KI-assistent eller et skript **på samme maskin** som skal lese status
eller styre ladingen gjennom tjenesten. Den er ment å kunne gis direkte til en assistent som verktøybeskrivelse.

## Kort fortalt

- **Adresse:** `http://127.0.0.1:8093`. Tjenesten lytter bare lokalt.
- **Ingen innlogging eller token.** Alle prosesser på maskinen har full tilgang. Gjør aldri porten tilgjengelig
  utenfra (se README).
- **Lesing:** `GET`, uten noen spesielle headere.
- **Endring:** `POST` med JSON-body og begge headerne `Content-Type: application/json` og `X-Lading: 1`.
- **Host-header:** må være `127.0.0.1`, `localhost` eller `::1`. Andre vertsnavn gir `421`.
- **Tid og enheter:**
  - Tidspunkter er Unix-tid i sekunder. Felt som `ts`, `start`, `til` og `avreise` er UTC.
  - Feltet `tid` er ISO 8601 i lokal tid (Europe/Oslo). Klokketimer som `"time": "04"` er lokal tid.
  - Penger er i kroner inkl. mva. Priser er i kr/kWh. `spot` er eks. mva.
  - Effekt er i watt (`*_w`), energi i kWh og strøm i ampere.

```sh
curl -s http://127.0.0.1:8093/api/status
curl -s -X POST http://127.0.0.1:8093/api/overstyring \
     -H 'Content-Type: application/json' -H 'X-Lading: 1' -d '{"type":"timer","timer":4}'
```

## Slik bør en assistent oppføre seg

1. **Les før du handler.** Hent `/api/status` og se på `styring.handling`, `styring.grunn`,
   `styring.overstyring` og `lader.modus` før du foreslår eller gjør en endring.
2. **Endre bare når brukeren ber om det.** Styringen planlegger selv de gunstigste timene. En overstyring
   gjelder til den utløper eller blir erstattet, og den kan koste penger.
3. **Forklar konsekvensen av «lad nå».** Den lader for fullt og ignorerer taket for kapasitetstrinnet.
   Midt på dagen kan det løfte hele månedens nettleie ett trinn, typisk 100–200 kr/mnd ekstra (se
   `maaned.trinn` og `maaned.tak_idag`). Foreslå heller `timer` hvis brukeren bare vil ha mer lading i natt.
4. **Ikke veksle fram og tilbake.**
   - Hver overgang mellom lading og pause bruker et av laderens få stopp per tilkobling (Zaptec Go tåler 5).
     Etter `maks_stopp` lader styringen på minimum i stedet for å pause.
   - Styringen venter minst 10 minutter mellom pause og gjenopptak, og øker strømmen høyst hvert 15. minutt.
     En endring kan derfor ta flere minutter før den synes på laderen.
5. **Bekreft etterpå.** Etter en POST kan du lese `/api/status` igjen etter 10–20 sekunder.
   `styring.handling` og `styring.logg` viser hva som faktisk ble gjort, og om kommandoen lyktes (`utfort`, `feil`).
6. **Sjekk modus.** Er `styring.modus` lik `dryrun`, sendes ingen kommandoer til laderen; da logges bare hva den
   ville gjort. Er den `av`, styres ingenting. Modus kan ikke endres via API-et.

## Endepunkter

### `GET /api/status`: tilstanden nå

De viktigste feltene:

| Felt | Betydning |
|---|---|
| `tid` | Tidspunktet svaret gjelder (lokal tid) |
| `naa.effekt_w` | Husets effekt nå, inkludert laderen |
| `naa.grunnlast_w` | Husets effekt uten elbilen |
| `naa.fersk` | `false` hvis siste måling er gammel. Da er effekttallene upålitelige |
| `naa.i1`, `naa.i3` | Fasestrøm i A (L2 måles ikke i IT-nett) |
| `time.hittil`, `time.prognose` | kWh brukt så langt i denne klokketimen, og anslag for hele timen |
| `maaned.trinn` | Kapasitetstrinn nå: `nr`, `fra`/`til` (kWh/h, snitt av de tre høyeste døgnmaksene) og `kr` per måned |
| `maaned.topp3`, `maaned.dognmaks` | De tre døgnmaksene som teller, og alle døgnmaksene i måneden |
| `maaned.tak_idag.sikkert` | Høyeste timeforbruk i dag som garantert ikke løfter trinnet |
| `tariff_naa` | `lavtariff` (natt/helg), `energiledd` (nettleie kr/kWh nå) og `ordning` (Norgespris/strømstønad) |
| `lader.modus` | 1 = ikke tilkoblet, 2 = tilkoblet og venter, 3 = lader, 5 = ferdig eller pauset |
| `lader.final_stop` | `true` når ladingen er pauset av styringen |
| `lader.effekt_w`, `lader.okt_kwh`, `lader.strom_satt` | Effekt nå, kWh denne tilkoblingen og strømgrensen i A |
| `priser[]` | Per time i dag og i morgen: `start`, `spot` (eks. mva), `energiledd`, `din_pris` (strøm + påslag − støtte + energiledd), `lavtariff` |
| `timer[]` | Siste 48 timer: `hus` (kWh totalt), `lader` (kWh elbil), `kilde` |
| `kilder` | Per datakilde (`tibber`, `zaptec`, `priser`, `elvia`): `sist_ok`, `feil` og `info` |
| `styring` | Se under |

`styring`:

| Felt | Betydning |
|---|---|
| `modus` | `aktiv`, `dryrun` eller `av` |
| `handling` | Hva styringen gjør nå, i klartekst, f.eks. «lader med 24 A», «pauset» eller «bilen er ikke tilkoblet» |
| `grunn` | Hvorfor, f.eks. «venter til 01:00» eller «timetak 9.7 kWh: …» |
| `lade`, `strom` | Om styringen vil lade akkurat nå, og med hvor mange ampere |
| `overstyring` | `null` (automatisk) eller `{type, satt, til, timer?}` |
| `plan.valgte` | Starttidspunktene for de planlagte ladetimene |
| `plan.timer[]` | Hver time fram til avreise: `start`, `energiledd`, `spot`, `valgt`, `ladet_kwh`, `forbi` |
| `plan.antall`, `plan.brukt`, `plan.avreise` | Ønsket antall ladetimer, hvor mange som allerede er ladet, og avreisetid |
| `tak.kwh` | Timetaket styringen holder huset under |
| `stopp_i_okt` | Stopp brukt i denne tilkoblingen |
| `advarsler[]` | F.eks. at Eco-modus er på i Zaptec |
| `logg[]` | Siste beslutninger, nyeste først: `ts`, `handling`, `detaljer`, `utfort`, `feil` |

### `POST /api/overstyring`: styr ladingen

Body: `{"type": "<type>"}`, og for `timer` også `"timer": N`.

| `type` | Virkning | Varer til |
|---|---|---|
| `auto` | Fjerner overstyringen. De gunstigste timene fram til avreise brukes | – |
| `lad_naa` | Lader nå med full strøm, **uten** tak for kapasitetstrinnet. Fasevakta gjelder fortsatt | 12 timer, eller til bilen kobles fra |
| `pause` | Ingen lading | Neste avreise (standard 07:00) |
| `timer` | `timer` = 0–24 ladetimer i vinduet fram til neste avreise. Timer som allerede er ladet, teller med | Neste avreise |

En ny overstyring erstatter den forrige. Bare én kan gjelde om gangen.

Svar `200`: `{"overstyring": {"type": "timer", "satt": 1795210000.0, "til": 1795240800.0, "timer": 4}}`.
For `auto` er svaret `{"overstyring": null}`.

### `GET /api/regning`: strømregning og elbilkostnad

Første kall etter omstart kan ta et par sekunder. Senere kall går raskt.

| Felt | Betydning |
|---|---|
| `inneverende.hittil`, `inneverende.prognose` | Denne måneden så langt og anslått for hele måneden |
| `historikk[]` | Avsluttede måneder (`maaned` = `"ÅÅÅÅ-MM"`) med samme felter |
| `elbil.totalt` | All registrert elbillading: `kwh`, `sum` (kr) og `ore_per_kwh` |
| `elbil.kapasitet_ekstra` | Ekstra kapasitetsledd ladingen har kostet, i kr |
| `elbil.okter[]` | Ladeøkter, nyeste først. `pagar: true` betyr at økta pågår |
| `elbil.maaneder[]` | Per måned: `kwh`, `energi_kr`, `ekstra_kr` og `sum` |
| `forutsetninger` | Gjeldende strømavtale, støtteordning og mva |

Feltene i en månedsregning:

| Felt | Betydning |
|---|---|
| `sum` | Totalt i kr |
| `kwh` | Forbruk |
| `kraft` | Spot inkl. mva + påslag |
| `stotte` | Det staten trekker fra. Negativ betyr at du betaler til staten (Norgespris når spot er lav) |
| `kraft_netto` | `kraft − stotte` |
| `energiledd`, `kapasitet`, `nettleie` | Nettleie: energiledd, kapasitetsledd og summen av dem |
| `fastbelop` | Fastbeløp i strømavtalen |
| `trinn` | Kapasitetstrinn |
| `ore_per_kwh` | Snittpris per kWh |

Den totale elbilkostnaden er `elbil.totalt.sum + elbil.kapasitet_ekstra`.

### `GET /api/innstillinger` og `POST /api/innstillinger`: strømavtale og støtte

```json
{"avtaler":   [{"fra": "2026-11-10", "paaslag": -0.012, "fastbelop": 0, "navn": "valgfri merknad"}],
 "ordninger": [{"fra": "2025-01-01", "ordning": "stromstotte", "terskel": 0.75, "grad": 0.9},
               {"fra": "2025-10-01", "ordning": "norgespris", "pris": 0.5}]}
```

- `paaslag` og `pris` er i kr/kWh inkl. mva, `fastbelop` i kr/mnd og `terskel` i kr/kWh eks. mva.
- `ordning` er `norgespris`, `stromstotte` eller `ingen`.
- POST **erstatter hele lista**. Hent den først, endre det som skal endres, og send alt tilbake.
- `{"tilbakestill": true}` går tilbake til verdiene i `config.toml`.
- Endringen slår inn i regningen med en gang, også bakover i tid.

### `GET /helse`

`200 {"ok": true}` når Tibber og Zaptec svarer, ellers `503 {"ok": false}`.

## Feil

| Kode | Betyr |
|---|---|
| `400` | Ugyldig innhold. Svaret er `{"feil": "<forklaring på norsk>"}` |
| `403` | Mangler `X-Lading: 1` eller `Content-Type: application/json` |
| `421` | Ukjent vertsnavn i Host-headeren. Bruk `127.0.0.1` |

## Vanlige spørsmål og hvor svaret står

| Spørsmål | Svar |
|---|---|
| Lader bilen nå? | `lader.modus == 3`, effekt i `lader.effekt_w` |
| Når lader den i natt? | `styring.plan.valgte` (sammenhengende timer kan slås sammen til intervaller) |
| Hvorfor lader den ikke? | `styring.handling` og `styring.grunn` |
| Hva koster strømmen nå? | Raden i `priser` der `start <= nå < start + 3600`, feltet `din_pris` |
| Er vi nær neste kapasitetstrinn? | `time.prognose` mot `maaned.tak_idag.sikkert`, og `maaned.trinn` |
| Hva blir strømregningen? | `/api/regning`: `inneverende.prognose.sum` |
| Hva har elbilen kostet? | `/api/regning`: `elbil.totalt.sum + elbil.kapasitet_ekstra` |

## Begrensninger

- **Bilen:** batterinivå er ikke kjent. Styringen bruker et fast antall ladetimer per natt.
- **Laderen:** ladestrømmen i ampere kan ikke settes direkte, og det finnes ikke noe kWh-mål.
- **Faste innstillinger:** modus, avreisetid og normalt antall timer settes i `.env` og krever omstart.
