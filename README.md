# Lading

Smart styring av elbillading med Zaptec-lader, for husholdninger med effekttariff i nettleien.
Tjenesten kjører på en liten hjemmeserver. Den følger husets forbruk i sanntid med Tibber Pulse og styrer laderen
gjennom Zaptecs API, slik at bilen lader billigst mulig uten å løfte månedens kapasitetstrinn.

Prioriteringene er, i rekkefølge:

1. **Lav nettleie (energiledd):** lad på natt-/helgetariff.
2. **Lave effekttopper (kapasitetsledd):** hold timeforbruket under grensen for månedens trinn.
3. **Lav spotpris:** blant ellers like timer, velg de billigste.

> **Viktig:** Websiden har **ingen innlogging**. Alle som når den, kan starte og stoppe ladingen og endre
> innstillinger. Den lytter derfor bare på `127.0.0.1` og må **ikke** åpnes direkte mot internett.
> Se [Tilgang utenfra](#tilgang-utenfra-cloudflare-tunnel).

## Hva den gjør

**Logging (alltid)**
- Husets forbruk og fasestrøm hvert ~10. sekund fra Tibber Pulse (sanntids-API via websocket).
- Laderens status hvert minutt fra Zaptec: tilkoblet/lader/pause, effekt, strøm og målerstand.
- Spotpriser per time for prisområdet, med historikk bakover (fra hvakosterstrommen.no).
- Avregnede timeverdier fra Elvia (valgfritt), for å kontrollere og fylle hull i Pulse-dataene.

**Ladestyring** (`modus = "aktiv"`)
- **Plan:** velger de *N* gunstigste timene fra kl. 12 dagen før til avreise (standard 6 timer og avreise 07:00).
  Lav nettleie kommer først, så lav spotpris. Timer som allerede er ladet i perioden, teller med. Planen samler
  timene i høyst to blokker, fordi laderen tåler få stopp per tilkobling.
- **Effektvakt:** i hver ladetime settes ladestrømmen slik at timeforbruket for hele huset holder seg under
  grensen for månedens kapasitetstrinn, med en liten margin. De første ti dagene i måneden brukes forrige måneds
  trinn som gulv. Blir det for lite plass igjen, pauses ladingen resten av timen.
- **Fasevakt:** strømmen senkes straks hvis en fase nærmer seg hovedsikringen.
- **Effektvarsel:** push-varsel mens timen pågår hvis forbruket *utenom* bilen ligger an til å øke
  kapasitetstrinnet. Varselet sier hvor mye som må skrus av, og hvor lenge (se [Varsler](#varsler)).
- **Overstyring fra websiden:**
  - **Lad nå:** full effekt uten trinngrense, til bilen kobles fra.
  - **Ikke lad i natt.**
  - **Annet antall timer** i natt.
  - **Automatisk:** tilbake til planen.
- **Sikkerhetsnett:**
  - Mangler data fra Pulse, lader den med forsiktig strøm.
  - Strømmen økes høyst hvert 15. minutt, som Zaptec anbefaler.
  - Feilende kommandoer gir varsel. Det samme gjør en bil som ikke starter igjen etter pause (se [Varsler](#varsler)).
  - Etter for mange stopp i én tilkobling lader den på minimum i stedet for å pause.

**Strømregning og elbilkostnad**
- Regningen hittil i måneden og en prognose for hele måneden. Den er delt opp i strøm, Norgespris eller
  strømstønad, energiledd, kapasitetsledd og fastbeløp.
- Regning per måned bakover så langt det finnes data.
- Hva elbilen koster: per ladeøkt og per måned, og om ladingen har løftet måneden opp et kapasitetstrinn.
- Strømavtale (påslag, fastbeløp) og støtteordning (Norgespris, strømstønad eller ingen) kan endres per
  periode på websiden. Alt regnes om, også bakover i tid.

## Det du trenger

- **Zaptec-lader** (laget for Zaptec Go) og innloggingen fra Zaptec-appen. Kontoen må ha tilgang til installasjonen.
- **Tibber Pulse** på HAN-porten og et personlig token fra
  [developer.tibber.com](https://developer.tibber.com/settings/access-token). Tibber som strømleverandør er
  ikke nødvendig.
- **Elvia som nettselskap** for tariffen som følger med. Med et annet nettselskap må du legge inn deres
  tariff i `config.toml` (se [Innstillinger](#innstillinger)). Elvia-tokenet for timeverdier er valgfritt.
- **En Linux-maskin som alltid er på**, med Python 3.11 eller nyere. En Raspberry Pi holder. Tjenesten bruker
  ca. 35 MB minne.

## Oppsett

### 1. Installer

```sh
git clone <repo> ~/zaptec && cd ~/zaptec
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Legg inn tilganger

```sh
cp .env.example .env
chmod 600 .env
```

Fyll inn `ZAPTEC_USER`, `ZAPTEC_PASSWORD` og `TIBBER_TOKEN` i `.env`. Finn så ID-ene for hjemmet og laderen:

```sh
.venv/bin/python -m lading.finn
```

Kommandoen skriver ut ferdige linjer: `LADING_TIBBER_HOME_ID`, `LADING_ZAPTEC_INSTALLATION_ID` og
`LADING_ZAPTEC_CHARGER_ID`. Velg Tibber-hjemmet med «sanntid: ja» og lim linjene inn i `.env`.
Har du Elvia, legg også inn `ELVIA_METERVALUES_TOKEN`. Det opprettes på Elvia Min side → Tilganger.

Vil du ha push-varsler på mobilen, sett opp en ntfy-topic nå (se [Varsler](#varsler)).

Sjekk resten av `.env.example`: prisområde, hovedsikring, avreisetid og antall ladetimer.
Alle standardverdier står der, kommentert ut.

### 3. Start i prøvemodus

```sh
.venv/bin/python -m lading          # -v for mer logging
```

Åpne <http://127.0.0.1:8093/>. Med `modus = "dryrun"`, som er standard, logger styringen hva den *ville*
gjort under «Siste beslutninger», uten å sende noe til laderen. La den gå et døgn eller to og sjekk at planen og
strømgrensene virker fornuftige.

Legg samtidig inn strømavtalen og støtteordningen under **Strømavtale og støtte** nederst på siden.
Påslag og fastbeløp finner du på fakturaen fra strømleverandøren. Ta med tidligere avtaler hvis du vil ha
riktig regning bakover.

### 4. Kjør som tjeneste

Som brukertjeneste med systemd (`~/.config/systemd/user/lading.service`):

```ini
[Unit]
Description=Lading: styring av elbillading
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=%h/zaptec
ExecStart=%h/zaptec/.venv/bin/python -m lading
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now lading
loginctl enable-linger $USER       # så tjenesten kjører uten at du er logget inn
journalctl --user -u lading -f     # logg
```

### 5. Slå på aktiv styring

1. I Zaptec-appen: slå av **Eco-modus** og **planlagt lading**. Ellers styrer Zaptec ladingen samtidig, og API-et
   avviser endring av ladestrøm (feilkode 527).
2. Sett `LADING_STYRING_MODUS=aktiv` i `.env` og start tjenesten på nytt.
3. Følg med første natta. «Siste beslutninger» viser hver kommando og om den lyktes.

## Innstillinger

Innstillingene ligger tre steder. Den som står senere i lista, vinner:

| Hvor | Hva | Endres |
|---|---|---|
| `config.toml` (i git) | Standardverdier og tabeller: nettleiens tariffperioder og kapasitetstrinn | Rediger fila og start på nytt |
| `.env` (ikke i git) | Tilganger, ID-er og egne verdier. Enhver enkeltverdi i `config.toml` kan overstyres med `LADING_<SEKSJON>_<NØKKEL>` | Rediger fila og start på nytt |
| Websiden (lagres i databasen) | Strømavtale og støtteordning per periode | Med en gang, uten omstart |

`.env.example` viser alle verdiene som kan settes, med forklaring og standardverdi.

**Nettleie.** `config.toml` har Elvias standardtariff for husholdninger fra april 2025: energiledd dag/natt
og kapasitetstrinn, inkludert avgifter og mva. Når nettselskapet endrer prisene, legges en ny
`[[tariff.perioder]]` til med `fra`-dato. En periode uten `trinn` arver trinnene fra perioden før.
For et annet nettselskap erstattes periodene med deres tariff.

**Strømavtale og støtte** (websiden, under «Strømavtale og støtte»):
- **Påslag** (øre/kWh) og **fastbeløp** (kr/mnd) med mva, slik de står på fakturaen. Hver rad gjelder fra
  og med datoen sin.
- **Norgespris:** fast pris per kWh. Staten dekker forskjellen når spotprisen er høyere, og du betaler
  forskjellen når den er lavere.
- **Strømstønad:** staten dekker en andel (90 %) av spotprisen over en terskel (eks. mva).
- **Ingen støtte**, f.eks. for næring eller fritidsbolig.
- Hvis ingenting er lagret på siden, brukes `[[regning.avtaler]]` og `[[regning.ordninger]]` fra `config.toml`.
  «Tilbakestill til config.toml» sletter det som er lagret på siden.

## Tilgang utenfra (Cloudflare Tunnel)

Tjenesten har ingen innlogging eller brukerstyring. Den har bare to enkle vern mot misbruk via nettleser:
- krav om en egen header, så *andre nettsider* ikke kan sende kommandoer,
- avvisning av ukjente vertsnavn, som verner mot DNS rebinding.

Ingen av dem beskytter mot direkte tilgang. Alle prosesser og brukere på maskinen kan bruke API-et fullt ut.
Den som når porten, kan blant annet:
- starte lading for fullt midt på dagen og løfte kapasitetstrinnet for hele måneden,
- stoppe ladingen før du skal kjøre,
- endre strømavtalen,
- se forbruksmønsteret ditt, som også viser når ingen er hjemme.

Derfor:
- La `LADING_WEB_HOST` stå på `127.0.0.1`.
- Ikke sett opp portviderekobling i ruteren.
- Ikke legg tjenesten bak en vanlig omvendt proxy uten innlogging.

**Anbefalt løsning: Cloudflare Tunnel med Cloudflare Access foran.** `cloudflared` på serveren kobler seg *ut* til
Cloudflare. Ingen porter åpnes inn, og Access krever innlogging (f.eks. engangskode på e-post) før noen
forespørsel slipper gjennom til tjenesten. Det er gratis for private brukere og krever et domene i Cloudflare.

1. Lag Access-applikasjonen **først**, så adressen aldri er åpen uten innlogging:
   - Zero Trust-dashbordet → Access → Applications → Add → *Self-hosted*.
   - Domene: f.eks. `lading.ditt-domene.no`.
   - Policy: *Allow*, regel *Emails* med din e-post (og andre i husstanden).
   - Øktvarighet etter smak, f.eks. 1 måned.
2. Installer `cloudflared` og lag tunnelen:

   ```sh
   cloudflared tunnel login
   cloudflared tunnel create lading
   ```

3. `~/.cloudflared/config.yml`:

   ```yaml
   tunnel: <tunnel-ID fra forrige steg>
   credentials-file: /home/<bruker>/.cloudflared/<tunnel-ID>.json
   ingress:
     - hostname: lading.ditt-domene.no
       service: http://127.0.0.1:8093
     - service: http_status:404
   ```

4. Koble adressen til tunnelen og start den som tjeneste:

   ```sh
   cloudflared tunnel route dns lading lading.ditt-domene.no
   sudo cloudflared service install
   ```

5. Test fra mobilnettet. Du skal få innloggingssiden til Access, ikke statussiden direkte.

Legg også adressen inn i `.env`, så tjenesten godtar den: `LADING_WEB_VERTER=lading.ditt-domene.no`.

Har du allerede en tunnel for andre tjenester, legger du bare til en ny `hostname` under `ingress`.
`/helse` kan unntas fra Access med en egen *Bypass*-policy på stien hvis ekstern overvåking trenger den.
Den viser bare `{"ok": true/false}`.

**Alternativer:**
- **Tailscale eller WireGuard:** siden er bare tilgjengelig for dine egne enheter, uten offentlig adresse.
- **SSH-tunnel ved behov:** `ssh -L 8093:127.0.0.1:8093 server`, og åpne så <http://127.0.0.1:8093/> lokalt.

## Varsler

Varsler sendes som push via [ntfy](https://ntfy.sh). Det er gratis og krever ingen konto.

1. Lag et langt, tilfeldig topic-navn. På ntfy.sh kan alle som kjenner navnet, lese varslene, så det må ikke
   kunne gjettes:

   ```sh
   python3 -c "import secrets; print('lading-' + secrets.token_hex(12))"
   ```

2. Installer ntfy-appen (Android/iOS) og abonner på topic-navnet på serveren `ntfy.sh`.
3. Legg adressen i `.env`:

   ```sh
   NTFY_URL=https://ntfy.sh/lading-…
   ```

4. Send et testvarsel, og start tjenesten på nytt så den tar i bruk adressen:

   ```sh
   .venv/bin/python -m lading.varsel
   systemctl --user restart lading
   ```

**Egen ntfy-server:** bruk dens adresse i `NTFY_URL`. Krever topicen innlogging, legges et tilgangstoken (`tk_…`)
i `NTFY_TOKEN`.

Hold gjerne varslene fra lading på en egen topic, skilt fra annen serverovervåking. Da blir det tydelig hva som
krever handling i huset. Uten `NTFY_URL` brukes kommandoen
`/usr/local/bin/varsle "Tittel" "Melding" [prioritet] [tagger]` hvis den finnes, ellers havner varslene bare i loggen.

| Varsel | Når |
|---|---|
| **Kutt ca. X kW de neste N min** (høy prioritet) | Timen ligger an til å øke kapasitetstrinnet, se under |
| OK, holdt / timen gikk over grensen | Etter en time det ble varslet for |
| Kommando feilet | En kommando til laderen ble avvist |
| Lading starter ikke | Tre forsøk på å gjenoppta uten at bilen lader |

**Effektvarsel.** Hvert 30. sekund regner tjenesten ut hvor mye denne klokketimen kan bli før snittet av
månedens tre høyeste døgnmakser går over trinnets grense. Den regner med de faktiske døgnmaksene så langt og et
anslag for resten av måneden. Prognosen antar at elbilen pauses, siden effektvakta tar seg av den, med mindre
«Lad nå» er på. Husets effekt er snittet av de siste fem minuttene, så vannkoker og induksjon ikke gir falsk alarm.
- **Push:** når prognosen er over grensen minus marginen, tidligst 5 minutter ut i timen (`LADING_VARSEL_EFFEKT_FRA_MIN`).
  Varselet sier hvor mange kW som må kuttes resten av timen, og hva det koster ellers.
- **Hvor ofte:** høyst ett varsel per time. Nytt varsel samme time bare hvis det må kuttes minst 1 kW mer.
- **Når det er for sent:** med mindre enn 3 minutter igjen, eller når kuttet er større enn forbruket, går det
  ikke push. Det vises bare på siden.
- **Forvarsel:** når timen går over trinnets øvre grense, men ikke øker trinnet, vises det bare på statussiden.
  Timen spiser da av marginen for resten av måneden.
- **Slå av:** `LADING_VARSEL_EFFEKT=false`. `LADING_VARSEL_LENKE` gir en adresse som åpnes når man trykker på varselet.

## API

| Sti | Innhold |
|---|---|
| `GET /` | Statusside |
| `GET /api/status` | Nå-tilstand: huset, laderen, denne timen, måneden, priser, styring og plan |
| `GET /api/regning` | Strømregning: hittil, prognose, per måned og elbilkostnad |
| `GET /api/innstillinger` | Strømavtale og støtteordning |
| `POST /api/innstillinger` | Lagre strømavtale og støtteordning, eller `{"tilbakestill": true}` |
| `POST /api/overstyring` | `{"type": "auto" \| "lad_naa" \| "pause" \| "timer", "timer": N}` |
| `GET /helse` | 200 når Tibber og Zaptec svarer, ellers 503 (for overvåking) |

POST krever `Content-Type: application/json` og headeren `X-Lading: 1`.
Forespørsler med andre vertsnavn enn `127.0.0.1`, `localhost`, `::1` og `LADING_WEB_VERTER` avvises med 421.
[API.md](API.md) beskriver alle felt og er skrevet slik at den kan gis til en KI-assistent på samme maskin.

## Data og backup

Alt lagres i SQLite-fila `data/lading.db` (WAL-modus):

| Tabell | Innhold |
|---|---|
| `maling` | Pulse-målinger hvert ~10. sekund |
| `time_hus` | Husets forbruk per time fra tre kilder: anslag, måleren og Elvia |
| `lader`, `time_lader`, `lader_maaler` | Laderens status, energi per time og laderens godkjente måler |
| `pris` | Spotpriser |
| `styring_logg` | Hver beslutning og kommando fra styringen |
| `innstilling` | Overstyring og strømavtale/støtte fra websiden |

Strømregningen lagres ikke. Den regnes ut fra tabellene og innstillingene hver gang.

Pulse-dataene kan ikke hentes på nytt, så ta backup. Bruk SQLite sin egen backup og ikke vanlig filkopi,
fordi databasen skrives til hele tiden:

```sh
sqlite3 data/lading.db ".backup '/sti/til/backup/lading-$(date +%F).db'"
```

Databasen vokser med ca. 9 000 rader i døgnet, noen hundre MB i året.

## Feilsøking

- **`journalctl --user -u lading`**, og «Datakilder» nederst på statussiden. Den viser siste kontakt og
  feil per kilde.
- **«Mangler i .env: …» ved oppstart:** en påkrevd verdi er tom. Se steg 2.
- **Kommando feilet med kode 527:** Eco-modus er på i Zaptec. Slå den av i appen.
- **Bilen starter ikke igjen etter pause:** noen biler går i dvale. Sjekk ladeinnstillingene i bilen og
  `LADING_STYRING_MAKS_STOPP`.
- **«Mangler ferske data fra Pulse»:** Pulse eller Tibber-API-et er nede. Styringen lader da med
  `TRYGG_STROM` til data kommer tilbake.

## Begrensninger

- Antall ladetimer per natt er fast. Batterinivå fra bilen er ikke på plass ennå.
- Tariffen som følger med, er Elvias. Henting av timeverdier fra nettselskapet virker bare for Elvia.
- Én lader og én bil.
- Fasevakta ser på L1 og L3. Det er det måleren gir i IT-nett.

## Kode

```
lading/
  __main__.py   oppstart: starter alle oppgavene
  config.py     config.toml + .env + miljøvariabler
  tibber.py     Pulse i sanntid, timeforbruk
  zaptec.py     laderstatus og kommandoer
  priser.py     spotpriser og historikk
  elvia.py      timeverdier fra nettselskapet
  tariff.py     nettleie per dato
  status.py     data til statussiden, kapasitetstrinn
  plan.py       hvilke timer bilen skal lade
  styring.py    effektvakt, fasevakt, overstyring og utførelse
  effektvarsel.py  varsel når timen ligger an til å øke kapasitetstrinnet
  varsel.py     push-varsler via ntfy (python -m lading.varsel sender et testvarsel)
  avtale.py     strømavtale og støtteordning
  regning.py    strømregning, prognose og historikk
  elbil.py      hva elbilen koster
  web.py        statusside og API
  finn.py       finner ID-ene til .env
  static/index.html
```

## Lisens

Copyright © 2026 Andreas Noteng

Dette programmet er fri programvare: du kan distribuere det videre og/eller endre det under vilkårene i
GNU General Public License, slik den er publisert av Free Software Foundation, enten versjon 3 av lisensen
eller (etter ditt valg) en hvilken som helst senere versjon (`GPL-3.0-or-later`).

Programmet distribueres i håp om at det er nyttig, men UTEN NOEN GARANTI, heller ikke underforstått garanti
om SALGBARHET eller EGNETHET FOR ET BESTEMT FORMÅL. Se [LICENSE](LICENSE) for hele lisensteksten.
