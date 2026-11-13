# NETTSIDE OPPSETT: Jobbradar tavla

Ærlig premiss først: en nettside kan ikke selv sjekke vy.no eller ISS fra nettleseren din, CORS stopper det. Det som holder deg oppdatert er radaren i GitHub Actions, som sjekker så ofte GitHub slipper den til (den ber om hvert 30. minutt) og skriver `docs/status.json`. Tavla leser den fila og viser tilstanden. Push varslene fra ntfy og Finn er fortsatt alarmen. Tavla er cockpiten.

---

## Filstruktur i repoet

```
job-radar/
  job_radar.py            (versjon 3)
  config.json
  requirements.txt        (avhengigheter, brukes av workflow)
  requirements-dev.txt    (det samme pluss pytest, for testene)
  seen.json               (lages automatisk)
  tests/
    test_job_radar.py     (testene, kjøres med pytest)
  docs/
    index.html            (tavla: selve siden)
    app.css, app.js       (utseende og oppførsel)
    manifest.webmanifest, sw.js, icon-*.png   (gjør siden til en app på hjemskjermen)
    status.json           (lages automatisk av skriptet)
    jobs.json             (lages automatisk: alle ønskede stillinger radaren har sett)
  .github/
    workflows/
      radar_check.yml     (versjon 3, selve radaren)
      tests.yml           (kjører testene når koden endres)
```

---

## Full modus: GitHub Pages (ca 5 min)

1. **Flytt ntfy topicen til en secret.** Settings, Secrets and variables, Actions, ny secret med navn `NTFY_TOPIC` og topicen din som verdi. La feltet i config.json stå som CHANGE ME. Skriptet foretrekker secreten. Grunnen: gratis GitHub Pages krever at repoet er **public**, og da skal ikke topicen ligge i klartekst.
2. Gjør repoet public (Settings, General, Danger Zone, Change visibility). Det som ligger igjen er søkeord og jobblenker, ingenting sensitivt.
3. Legg `job_radar.py`, `config.json`, `requirements.txt` og `.github/workflows/radar_check.yml` i repoet.
4. Legg hele `docs/` mappa i repoet og push.
5. Settings, Pages, Source: Deploy from a branch, velg `main` og `/docs`, lagre.
6. Actions fanen, job radar, Run workflow. Da finnes `status.json` fra første stund.
7. Adressen blir `https://BRUKERNAVN.github.io/job-radar/`. Første deploy kan ta et par minutter.
8. **iPhone:** åpne adressen i Safari, trykk Del, Legg til på hjemskjermen. Nå er radaren et appikon.

---

## Frakoblet modus (privat repo eller null oppsett)

Åpne `index.html` rett i nettleseren. Vinduene og sikkerhetskopien virker, men stillingslista og tavla trenger `status.json` og `jobs.json` fra radaren, så radarlinja viser FRAKOBLET. Det er ærlig, ikke ødelagt.

---

## Epostvarsling

To nivåer. VARSLING linja på tavla viser hva som er aktivt.

**Nivå 1, virker fra dag én uten oppsett:** når radaren finner treff, oppretter skriptet et GitHub issue i repoet ditt, og GitHub sender deg epost om nye issues automatisk (standardinnstilling for egne repo). Hver stilling får sitt eget issue, altså én epost per stilling, med tittel, sted, frist og direktelenke til utlysningen. Kommer det mer enn 5 treff i én kjøring, samles de i ett. Sjekk at epost er på under github.com/settings/notifications, "Watching".

Samme vei går tre andre meldinger: advarsel når en side har feilet 5 sjekker på rad, beskjed når den virker igjen, og den ukentlige "Radar lever". Disse issuene lukkes med en gang, så bare ekte treff blir stående åpne til du har sett på dem.

Et treff regnes ikke som varslet før GitHub (eller en annen kanal) har tatt imot meldingen. Feiler sendingen, prøver radaren på nytt ved neste kjøring.

**Påminnelse om frist:** har stillingen en søknadsfrist, får du en påminnelse 3 dager før og en dagen før, som en kommentar på stillingens issue (GitHub sender den som epost). **Lukk issuet når du er ferdig med stillingen**, enten du har søkt eller ikke vil ha den. Da slutter påminnelsene. At et issue er lukket sier ingenting om hvorfor, så ingen kan se hva du har søkt på. SAS og Norwegian oppgir ikke frist i lista, så de får ingen påminnelse.

**Frister i kalenderen:** radaren skriver `docs/frister.ics`. Trykk ABONNER under RADAR på siden (eller åpne `webcal://BRUKERNAVN.github.io/job-radar/frister.ics` på telefonen) én gang, så ligger hver frist som en heldagshendelse i kalenderen din.

**Push rett på mobilen (valgfritt, raskest):**
1. Installer appen ntfy (App Store / Google Play).
2. Finn på et langt, tilfeldig emnenavn, for eksempel `jobbradar-` pluss 20 tilfeldige tegn. Alle som kan navnet kan lese varslene, så ikke bruk noe som kan gjettes.
3. Abonner på emnet i appen.
4. Legg emnenavnet inn som secret `NTFY_TOPIC` i GitHub. Stillingstreff kommer med høyeste prioritet, fristpåminnelser litt lavere, driftsmeldinger som vanlig varsel.

**Nivå 2, epost rett fra din egen Gmail (valgfritt):**
1. Skru på totrinnsverifisering på Google kontoen hvis du ikke har det.
2. Lag et app passord på myaccount.google.com/apppasswords.
3. Legg inn to secrets i GitHub: `SMTP_USER` (epostadressen din) og `SMTP_PASS` (app passordet). Da sender skriptet eposten selv, og issue varselet skrus av automatisk så du slipper dobbelt.

Mottaker er secret `ALERT_EMAIL` i GitHub (feltet `email` i config.json står tomt med vilje — repoet er public, og epostadressen skal ikke ligge i klartekst). Test lokalt med `python job_radar.py --test` (krever at smtp feltene i config.json eller miljøvariablene er satt). Fra GitHub: Actions, job radar, Run workflow, kryss av `test_alert`.

---

## Verdt å vite

* Forhåndsvisningen inne i Claude lagrer ingenting, sandkassa blokkerer lagring. Lokalt og på Pages lagres status, notater og rutiner i nettleseren.
* Lagringen er per nettleser. Mobilen og PCen har hver sin tavle. Bruk mobilen som hovedcockpit. Søknadsstatus og notater ligger bare der, ingen andre kan se hva du har søkt på.
* Flytte alt til en annen enhet: SIKKERHETSKOPI nederst på siden. Last ned kopi (eller Vis som tekst), og hent den inn på den andre enheten.
* LEDIG NÅ viser hver stilling du vil ha med nedtelling til fristen. Sett status (SKAL SØKE, SØKT, INTERVJU ...) så havner den under SØKNADER og blir stående der også etter at utlysningen er lukket.
* HISTORIKK fylles etter hvert som radaren ser utlysninger komme og gå. Over tid viser den når hver arbeidsgiver pleier å lyse ut.
* RADAR viser hvor ofte GitHub faktisk har kjørt sjekken det siste døgnet, og den lengste pausen siste uke.
* Ny arbeidsgiver i config.json dukker opp på tavla av seg selv. Feltene `id`, `label` og `sub` styrer hva som står på kortet. Ikke endre `id` på en arbeidsgiver du har notater på: notatene er lagret under den.
* ISS portalen er verifisert 10.07.2026: iss.no (no.issworld.com) lenker selv til iss.attract.reachmee.com/jobs, og siden viser alle utlysningene uten JavaScript. VERIFISER merket er derfor fjernet fra tavla. Det eneste som gjenstår å se, er om aviation stillingene på OSL faktisk publiseres der når de kommer.
* Norwegian leses fra november 2026 fra SAP sin åpne stillingsfeed (`"type": "successfactors"` i config.json), ikke fra careers.norwegian.com. Karrieresiden deres ligger bak Cloudflare, som avviser GitHub sine servere med 403 på alle sider. Feeden har alle Norwegian stillingene, men ikke noe eget stedsfelt. Derfor: står det Oslo, OSL eller Gardermoen i tittel eller annonsetekst, er det treff. Står det et annet sted i tittelen (lista `location_elsewhere`), er det stille. Står det ingen steder, varsler radaren likevel, merket "sted ikke bekreftet, sjekk selv".
* Kjører du `python job_radar.py` på egen maskin, er det bare en forhåndsvisning. Den sender ingenting og lagrer ingenting. `seen.json` og `docs/status.json` eies av kjøringene på GitHub. Ekte kjøring lokalt krever `--write`.
* Vy sjekkes ikke lenger ved å skrape vy.no (siden viser bare de 8 første utlysningene uten JavaScript). Radaren spør i stedet Webcruiter sitt API direkte (`"type": "webcruiter"` i config.json) og ser ALLE utlysningene i Vygruppen, med tittel, sted og frist.

---

## Feilsøk

* **FRAKOBLET på Pages:** sjekk at Actions har kjørt etter oppsettet og at `docs/status.json` faktisk ligger i repoet.
* **GAMMEL DATA:** siste kjøring feilet eller cron henger. Åpne Actions fanen og se på loggen. GitHub hopper over mange planlagte kjøringer, så noen timer mellom sjekkene er normalt. Trykk Kjør sjekk nå når det haster.
* **Epost om at en side ikke svarer (FEIL x5):** målsiden er flyttet, nede eller blokkerer. Tavla viser årsaken ved siden av FEIL, for eksempel `HTTP 403 Forbidden`. Kjør workflowen med `dry_run` for å se hva siden svarer GitHub. Oppdater url i config.json hvis siden er flyttet.
* **Treff merket "sted ikke bekreftet":** Norwegian stilling der verken tittel eller tekst nevner Oslo. Åpne lenka og se. Er det en flyplass som går igjen, legg navnet i `location_elsewhere` i config.json.
* **Får ikke valgt /docs under Pages:** mappa må ligge på main og inneholde index.html før valget dukker opp.
