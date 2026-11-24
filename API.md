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
| `effektvarsel` | Vurdering av denne timen, eller `null` uten ferske data. Se under |
| `timer[].ute`, `timer[].vp` | Utetemperatur (°C, fra varmepumpa) og varmepumpens kWh per time |
| `kilder` | Per datakilde (`tibber`, `zaptec`, `priser`, `elvia`): `sist_ok`, `feil` og `info` |
| `styring` | Se under |

`styring`:

| Felt | Betydning |
|---|---|
| `modus` | `aktiv`, `dryrun` eller `av` |
| `handling` | Hva styringen gjør nå, i klartekst, f.eks. «lader med 24 A», «pauset», «bilen er full» eller «bilen er ikke tilkoblet» |
| `grunn` | Hvorfor, f.eks. «venter til 01:00» eller «timetak 9.7 kWh: …» |
| `lade`, `strom` | Om styringen vil lade akkurat nå, og med hvor mange ampere |
| `overstyring` | `null` (automatisk) eller `{type, satt, til, timer?}` |
| `plan.valgte` | Starttidspunktene for de planlagte ladetimene |
| `plan.timer[]` | Hver time fram til avreise: `start`, `energiledd`, `spot`, `valgt`, `ladet_kwh`, `forbi` |
| `plan.antall`, `plan.brukt`, `plan.avreise` | Ønsket antall ladetimer, hvor mange som allerede er ladet, og avreisetid |
| `tak.kwh` | Timetaket styringen holder huset under |
| `stopp_i_okt` | Stopp brukt i denne tilkoblingen |
| `ferdig` | `null`, eller `{ts, okt_kwh, grunn}` når bilen er registrert som full i denne tilkoblingen. Da lader ikke styringen mer før bilen kobles fra. `lad_naa` eller `timer` nullstiller den |
| `advarsler[]` | F.eks. at Eco-modus er på i Zaptec |
| `logg[]` | Siste beslutninger, nyeste først: `ts`, `handling`, `detaljer`, `utfort`, `feil` |

`effektvarsel`: om forbruket utenom elbilen ligger an til å øke kapasitetstrinnet.

| Felt | Betydning |
|---|---|
| `niva` | `ok`, `forvarsel` (over trinnets øvre grense, spiser av marginen) eller `varsel` (trinnet øker) |
| `prognose`, `hittil` | kWh for timen, anslått og så langt. Prognosen antar at bilen pauses, med mindre `lad_naa` |
| `grense` | kWh timen kan bli før trinnet øker. `sikker` er trinnets øvre grense |
| `kutt_kw`, `rest_min` | Hvor mange kW som må skrus av resten av timen, og hvor mange minutter som er igjen |
| `kan_rekkes` | `false` hvis det er for sent, eller kuttet er større enn forbruket |
| `nytt_trinn`, `ekstra_kr` | Trinnet og ekstrakostnaden per måned hvis grensen passeres |
| `last_kw`, `lader_kw`, `lad_naa` | Husets effekt utenom bilen (5 min snitt), bilens effekt, og om «Lad nå» er på |

Ved `niva == "varsel"` er det nyttigste en assistent kan gjøre å si fra med `kutt_kw` og `rest_min`, og
eventuelt foreslå å slå av store forbrukere: varmtvannsbereder, ovner, tørketrommel eller varmepumpe.
Tjenesten sender selv push-varsel.

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
| `inneverende.temperatur` | Om prognosen er justert for værvarselet: `modell`, `ref_ute`, `varsel_ute`, `timer_justert`, `justering_kwh` |
| `temperatur.dogn[]` | Hele døgn med utetemperatur: `dato`, `ute`, `inne`, `hus`, `lader`, `vp`, `uten_bil` (kWh), `ved` (vedfyring), `ved_timer` |
| `temperatur.modell` | `uten_bil ≈ a + b·ute` over døgnene: `b` er kWh per døgn per grad, `r2` hvor godt det passer, `brukes` om prognosen justeres |

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
| `kwh_uten_bil` | Forbruk uten elbillading (bare der laderen er logget) |
| `graddager`, `ute_snitt` | Graddager (basis 17 °C) og snittemperatur ute; `null` når under halve måneden har måling. For inneværende måned står de under `hittil` |

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

### `POST /api/varmepumpe`: styr varmepumpen

Status står i `/api/status` under `varmepumpe` (`null` når ingen innlogging er lagt inn):

| Felt | Betydning |
|---|---|
| `enheter[]` | Én per luft-luft-enhet: `id`, `navn`, `paa`, `modus`, `temperatur` (ønsket), `rom` (målt i innedelen), `vifte`, `faktisk_vifte`, `lamell_v`, `lamell_h`, `feil`, `feilkode` |
| `enheter[].evner` | Hva enheten kan: `moduser`, `temp` (min/maks per modus), `halve` (halve grader), `vifter`, `auto_vifte`, `lamell_v`, `lamell_h`, `sving`, `energimaaler` |
| `ute` | Utetemperatur fra utedelen |
| `energi.idag_kwh` | Strøm brukt i dag (100 Wh-oppløsning, noen minutter forsinket) |

`timer[].vp` er varmepumpens kWh per time. Den er en del av `hus`, og huset ellers er `hus − lader − vp`.

**Slå aldri pumpen av eller på (`paa`) uten at brukeren uttrykkelig ber om det.** Kompressoren tåler dårlig
hyppige start og stopp; pumpen styres med `temperatur`, og eventuelt `modus` (kjøling om sommeren). Et
modusbytte slår ikke pumpen på.

Send bare det som skal endres:

```json
{"enhet": "<id, valgfri når det bare er én>", "paa": true, "modus": "Heat", "temperatur": 21.5, "vifte": "Auto"}
```

- `modus`: `Heat`, `Cool`, `Automatic`, `Dry` eller `Fan`, så langt `evner.moduser` tillater.
- `temperatur`: innenfor `evner.temp[modus]`, i steg på 0,5 °C når `evner.halve` er `true`, ellers hele grader.
- `vifte`: `Auto`, `One` … `Five`. `lamell_v`: `Auto`, `One` … `Five`, `Swing`. `lamell_h`: `Auto`, `Left`, `LeftCentre`,
  `Centre`, `RightCentre`, `Right`, `Swing`.
- Svaret er enheten med endringen. Statusen i `/api/status` er bekreftet av MELCloud noen sekunder senere.
- Ugyldige verdier gir `400`, og en kommando MELCloud avviser gir `502`. Begge med forklaring i `feil`.
- Ikke send mange kommandoer på rad (f.eks. én per halve grad). Regn ut måltemperaturen og send den én gang.

### `POST /api/varmepumpe/program`: ukeprogrammet

Programmet ligger i MELCloud og kjøres derfra. Det står i `/api/status` under `varmepumpe.enheter[].program`:

| Felt | Betydning |
|---|---|
| `aktiv` | Om programmet er slått på |
| `punkter[]` | `id`, `dager` (ISO-ukedager, 1 = mandag … 7 = søndag), `tid` (`"HH:MM"`, lokal tid), `modus`, `temperatur`, `paa` |
| `neste` | Neste programendring: `ts` (UTC), `temperatur`, `modus`, `paa` |

Et punkt setter temperatur og modus fra klokkeslettet og gjelder til neste punkt. Nattsenking er altså et punkt
med lav temperatur om kvelden og et med vanlig temperatur om morgenen.

```json
{"handling": "lagre", "punkt": {"dager": [1, 2, 3, 4, 5], "tid": "06:00", "temperatur": 20, "modus": "Heat"}}
{"handling": "lagre", "punkt": {"id": "<eksisterende id>", "temperatur": 18}}
{"handling": "slett", "id": "<id>"}
{"handling": "aktiv", "aktiv": false}
```

- `lagre` uten `id` lager et nytt punkt; med `id` endres bare feltene som sendes. Svaret er hele programmet.
- Punktene kan ikke slå pumpen av eller på (`paa` avvises). Nye punkter lagres med pumpen på.
- `aktiv: false` stopper hele programmet; pumpen blir da stående på innstillingen den har.
- Bekreft med brukeren før du sletter punkter eller slår av programmet.

`GET /api/varmepumpe/innlogging` sier om innlogging er lagt inn (`satt`, `bruker` maskert). En assistent skal
ikke lagre eller endre innloggingen; det gjør brukeren på websiden.

### `POST /api/tuya`: luftfukter og luftavfukter

Apparatene styres lokalt på hjemmenettet (Tuya-protokollen), uten sky. Status står i `/api/status` under
`tuya` (`null` når ingen apparater er koblet til):

| Felt | Betydning |
|---|---|
| `enheter[]` | Ett per apparat: `id`, `navn`, `type` (`luftfukter`, `avfukter` eller `ukjent`), `modell`, `online`, `sist_ok` |
| `enheter[].fukt`, `.temp` | Målt luftfuktighet (%) og temperatur (°C) der apparatet står |
| `enheter[].paa`, `.modus`, `.maal`, `.niva` | Av/på, modus, ønsket luftfuktighet (%) og tåke-/viftenivå |
| `enheter[].brytere` | Tilleggsfunksjoner, f.eks. `varme`, `ionisator`, `lys`, `lyd` eller `barnesikring` (`true`/`false`) |
| `enheter[].varsler` | Tekster som «tanken er tom», «tanken er full» eller «filteret må rengjøres». Tom liste når alt er i orden |
| `enheter[].filter` | Gjenstående filterlevetid i % (bare avfukteren) |
| `enheter[].evner` | `maal` (`min`, `maks`, `steg`, `ikke_i_modus`), `moduser` og `nivaer` (verdi → visningsnavn), `niva_navn`, `brytere`, `kompressor` |
| `enheter[].raa` | Bare for ukjente modeller: rådata, kan ikke styres |
| `historikk` | `{id: [[time, fukt, temp, andel på], …]}`: timesnitt de siste 48 timene, `time` er UTC-start på timen |

Send bare det som skal endres:

```json
{"enhet": "<id>", "maal": 45, "modus": "manual", "niva": "level_2", "varme": false}
```

- `maal`: innenfor `evner.maal`, i hele steg (`steg`, typisk 5 %). Kan ikke settes når `modus` står i
  `evner.maal.ikke_i_modus` (Wilfa i auto styrer selv).
- `modus` og `niva`: en av nøklene i `evner.moduser` og `evner.nivaer`.
- Brytere: navnet fra `evner.brytere` med `true` eller `false`.
- `paa`: **Slå aldri avfukteren (`evner.kompressor: true`) av eller på uten at brukeren uttrykkelig ber om det.**
  Den har kompressor og styres med `maal`. Luftfukteren kan slås av og på når brukeren ber om det.
- Svaret er apparatet etter endringen. Ugyldige verdier gir `400`, og et apparat som ikke svarer gir `502`.

`GET /api/tuya/innlogging` viser hvilke apparater som er koblet til (`satt`, `enheter`). Tilkoblingen gjøres av
brukeren på websiden med Smart Life-appen; en assistent skal ikke starte eller fjerne den.

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
| Er bilen full? | `styring.ferdig` (ikke `null`), eller `lader.modus` 5 med `lader.final_stop` `false` |
| Hva koster strømmen nå? | Raden i `priser` der `start <= nå < start + 3600`, feltet `din_pris` |
| Hvor varmt er det inne/ute? | `varmepumpe.enheter[0].rom` og `varmepumpe.ute` |
| Når senkes/økes temperaturen? | `varmepumpe.enheter[0].program.punkter` og `.neste` |
| Brukte vi mye fordi det var kaldt? | `/api/regning`: `graddager` per måned, og `temperatur.modell` (`b`, `r2`) |
| Fyrte vi med ved i går? | `/api/regning`: `temperatur.dogn[]` (`ved`) |
| Hvor mye strøm bruker varmepumpen? | `varmepumpe.energi.idag_kwh`, og `timer[].vp` per time |
| Hvor fuktig er det inne / i kjelleren? | `tuya.enheter[].fukt` (og `.temp`), med `navn` |
| Må tanken tømmes eller fylles? | `tuya.enheter[].varsler` |
| Er vi nær neste kapasitetstrinn? | `effektvarsel.niva`, `effektvarsel.prognose` mot `effektvarsel.grense` |
| Hva blir strømregningen? | `/api/regning`: `inneverende.prognose.sum` |
| Hva har elbilen kostet? | `/api/regning`: `elbil.totalt.sum + elbil.kapasitet_ekstra` |

## Begrensninger

- **Bilen:** batterinivå er ikke kjent. Styringen bruker et fast antall ladetimer per natt.
- **Laderen:** ladestrømmen i ampere kan ikke settes direkte, og det finnes ikke noe kWh-mål.
- **Varmepumpen:** styres via MELCloud Home sitt uoffisielle API, med noen sekunders forsinkelse.
  Innetemperaturen (`rom`) måles av pumpas IR-sensor, altså overflater i rommet, ikke lufta. Ved vedfyring viser
  den gjerne mer enn ønsket temperatur.
- **Luftfukter/avfukter:** strømforbruket måles ikke. `historikk[][3]` (andel av timen apparatet var på) kan
  brukes til et grovt anslag. Apparatene må være på samme nett som serveren.
- **Utetemperaturen** kommer fra varmepumpas utedel og finnes fra slutten av august 2026. Eldre måneder har
  ikke graddager.
- **Faste innstillinger:** modus, avreisetid og normalt antall timer settes i `.env` og krever omstart.
