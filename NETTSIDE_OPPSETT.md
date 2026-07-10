# NETTSIDE OPPSETT: Jobbradar tavla

Ærlig premiss først: en nettside kan ikke selv sjekke vy.no eller ISS fra nettleseren din, CORS stopper det. Det som holder deg oppdatert er radaren i GitHub Actions som kjører hvert 30. minutt og skriver `docs/status.json`. Tavla leser den fila og viser tilstanden. Push varslene fra ntfy og Finn er fortsatt alarmen. Tavla er cockpiten.

---

## Filstruktur i repoet

```
job-radar/
  job_radar.py            (versjon 2, bytt ut den gamle)
  config.json
  requirements.txt        (avhengigheter, brukes av workflow)
  seen.json               (lages automatisk)
  docs/
    index.html            (tavla)
    status.json           (lages automatisk av skriptet)
  .github/
    workflows/
      radar_check.yml     (versjon 2, bytt ut den gamle)
```

---

## Full modus: GitHub Pages (ca 5 min)

1. **Flytt ntfy topicen til en secret.** Settings, Secrets and variables, Actions, ny secret med navn `NTFY_TOPIC` og topicen din som verdi. La feltet i config.json stå som CHANGE ME. Skriptet foretrekker secreten. Grunnen: gratis GitHub Pages krever at repoet er **public**, og da skal ikke topicen ligge i klartekst.
2. Gjør repoet public (Settings, General, Danger Zone, Change visibility). Det som ligger igjen er søkeord og jobblenker, ingenting sensitivt.
3. Bytt ut `job_radar.py` og `radar_check.yml` med versjon 2 filene.
4. Legg `index.html` i `docs/` mappa og push.
5. Settings, Pages, Source: Deploy from a branch, velg `main` og `/docs`, lagre.
6. Actions fanen, job radar, Run workflow. Da finnes `status.json` fra første stund.
7. Adressen blir `https://BRUKERNAVN.github.io/job-radar/`. Første deploy kan ta et par minutter.
8. **iPhone:** åpne adressen i Safari, trykk Del, Legg til på hjemskjermen. Nå er radaren et appikon.

---

## Frakoblet modus (privat repo eller null oppsett)

Åpne `index.html` rett i nettleseren, ferdig. Alt virker unntatt sanntidslinjene: triage kalkulatoren, rutinene, brevene med kopiknapp, lenkene og det manuelle laget på tavla. Radarlinja viser FRAKOBLET. Det er ærlig, ikke ødelagt.

---

## Epostvarsling

To nivåer. VARSLING linja på tavla viser hva som er aktivt.

**Nivå 1, virker fra dag én uten oppsett:** når radaren finner treff, oppretter workflowen et GitHub issue i repoet ditt, og GitHub sender deg epost om nye issues automatisk (standardinnstilling for egne repo). Én epost per kjøring uansett antall treff, med tittel, sted, frist og direktelenke til utlysningen. Sjekk at epost er på under github.com/settings/notifications, "Watching".

**Nivå 2, epost rett fra din egen Gmail (valgfritt):**
1. Skru på totrinnsverifisering på Google kontoen hvis du ikke har det.
2. Lag et app passord på myaccount.google.com/apppasswords.
3. Legg inn to secrets i GitHub: `SMTP_USER` (epostadressen din) og `SMTP_PASS` (app passordet). Da sender skriptet eposten selv, og issue varselet skrus av automatisk så du slipper dobbelt.

Mottaker er `email` feltet i config.json. Test lokalt med `python job_radar.py --test` (krever at smtp feltene i config.json eller miljøvariablene er satt).

---

## Verdt å vite

* Forhåndsvisningen inne i Claude lagrer ingenting, sandkassa blokkerer lagring. Lokalt og på Pages lagres status, notater og rutiner i nettleseren.
* Lagringen er per nettleser. Mobilen og PCen har hver sin tavle. Bruk mobilen som hovedcockpit.
* Rutinelistene nullstiller seg selv: daglig ved midnatt, ukentlig ved ny uke.
* ISS portalen er verifisert 10.07.2026: iss.no (no.issworld.com) lenker selv til iss.attract.reachmee.com/jobs, og siden viser alle utlysningene uten JavaScript. VERIFISER merket er derfor fjernet fra tavla. Det eneste som gjenstår å se, er om aviation stillingene på OSL faktisk publiseres der når de kommer.
* Vy sjekkes ikke lenger ved å skrape vy.no (siden viser bare de 8 første utlysningene uten JavaScript). Radaren spør i stedet Webcruiter sitt API direkte (`"type": "webcruiter"` i config.json) og ser ALLE utlysningene i Vygruppen, med tittel, sted og frist.

---

## Feilsøk

* **FRAKOBLET på Pages:** sjekk at Actions har kjørt etter oppsettet og at `docs/status.json` faktisk ligger i repoet.
* **GAMMEL DATA:** siste kjøring feilet eller cron henger. Åpne Actions fanen og se på loggen.
* **Ping om FEIL x5:** målsiden er flyttet eller blokkerer. Oppdater url i config.json. Mest sannsynlig ISS, du vet hvorfor.
* **Får ikke valgt /docs under Pages:** mappa må ligge på main og inneholde index.html før valget dukker opp.
