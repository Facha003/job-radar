"use strict";
/* JOBBRADAR, versjon 6: alt som beveger seg.
   Radarskjerm, avgangstavle med vippebokstaver, logg, løpende tekst, trendlinjer, TV-modus.
   Ingenting her er pynt uten dekning: sveipet går over ekte status, tavla viser ekte
   stillinger, loggen er det radaren faktisk gjorde, og klokkene teller ekte tid.
   Bruker hjelperne fra app.js (esc, trygLenke, kortDato, relTid, dagerTil, jobbStatus, maalNavn, $). */

const display = (() => {
  const rolig = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const SVEIP_S = 6;                 // sekunder per runde på radarskjermen
  const kortNavn = t => String((t && (t.label || t.name)) || "").split("·")[0].trim().split(/\s+/)[0].toUpperCase();
  const maalFor = navn => ((STATUS && STATUS.targets) || []).find(t => t.name === navn);
  const to = n => String(n).padStart(2, "0");
  const maalMedId = id => ((STATUS && STATUS.targets) || []).find(t => t.id === id);
  /* "ny i denne sjekken": først sett i minuttene før status ble skrevet. Varselet
     stemples noen sekunder før status.json, så likhet på sekundet holder ikke. */
  const nyISjekken = j => {
    const ms = Date.parse(STATUS && STATUS.generated) - Date.parse(j.first_seen);
    return ms >= 0 && ms < 10 * 60 * 1000;
  };
  const klokke = iso => { const d = new Date(iso); return isNaN(d) ? "" : to(d.getHours()) + ":" + to(d.getMinutes()); };

  /* ================= radarskjerm ================= */
  function visScope() {
    const el = $("scope");
    if (!el) return;
    const maal = (STATUS && STATUS.targets) || [];
    if (!maal.length) { el.innerHTML = `<div class="tom">INGEN RADARDATA.</div>`; return; }
    const indre = maal.filter(t => t.id !== "nav"), ytre = maal.filter(t => t.id === "nav");
    const plasser = [];
    indre.forEach((t, i) => plasser.push({ t, vinkel: (i * 360 / indre.length + 24) % 360, r: i % 2 ? 66 : 44 }));
    ytre.forEach((t, i) => plasser.push({ t, vinkel: (200 + i * 40) % 360, r: 92 }));

    const blipper = plasser.map(({ t, vinkel, r }) => {
      const rad = (vinkel - 90) * Math.PI / 180, x = +(Math.cos(rad) * r).toFixed(1), y = +(Math.sin(rad) * r).toFixed(1);
      const aapne = JOBS.filter(j => j.state === "open" && j.target === t.name);
      const nytt = aapne.some(nyISjekken);
      const klasse = !t.ok ? "feil" : aapne.length ? "ledig" : "ok";
      const tips = (t.label || t.name) + ": " + (!t.ok ? "FEIL · " + (t.error || "svarer ikke") : aapne.length ? aapne.length + " ledig" : "OK, ingen ønskede stillinger ute");
      const venstre = x < -8;
      /* blinket starter idet sveipet passerer: negativ forsinkelse holder alle i takt */
      const forsinkelse = (vinkel / 360 * SVEIP_S - SVEIP_S).toFixed(2);
      return `<g class="blip ${klasse}${nytt ? " nytt" : ""}" data-blip="${esc(t.id || "")}" transform="translate(${x} ${y})" tabindex="0" role="button" aria-label="${esc(tips)}">
        <title>${esc(tips)}</title>
        ${klasse === "ledig" ? `<circle class="ring" r="9"/>` : ""}
        <circle class="prikk" r="4.2" style="animation-delay:${forsinkelse}s"/>
        ${klasse === "feil" ? `<path class="kryss" d="M-3-3 3 3M3-3-3 3"/>` : ""}
        <text x="${venstre ? -8 : 8}" y="3" text-anchor="${venstre ? "end" : "start"}">${esc(kortNavn(t))}${aapne.length ? " " + aapne.length : ""}</text>
      </g>`;
    }).join("");

    const ledig = JOBS.filter(j => j.state === "open").length, feil = maal.filter(t => !t.ok).length;
    el.innerHTML = `<svg viewBox="-118 -112 236 224" role="img" aria-label="Radarskjerm: ${maal.length} kilder, ${ledig} ledig, ${feil} feiler">
      <defs><linearGradient id="hale" x1="0" y1="0" x2="1" y2="0"><stop class="halestopp" offset="0" stop-opacity="0"/><stop class="halestopp" offset="1" stop-opacity=".34"/></linearGradient></defs>
      <circle class="skive" r="104"/>
      <g class="rutenett"><circle r="34"/><circle r="69"/><circle r="104"/><path d="M-104 0H104M0-104V104"/></g>
      <g class="sveip"><path d="M0 0 L-52 -90.07 A104 104 0 0 1 0 -104 Z" fill="url(#hale)"/><path class="straale" d="M0 0V-104"/></g>
      ${blipper}
      <circle class="senter" r="2.4"/>
    </svg>
    <div class="scopetekst"><b class="${ledig ? "gul" : ""}">${ledig} LEDIG</b> · ${maal.length} KILDER · <span class="${feil ? "rod" : ""}">${feil} FEILER</span></div>`;
  }

  /* ================= avgangstavle ================= */
  const TEGN = " ABCDEFGHIJKLMNOPQRSTUVWXYZÆØÅ0123456789.:-/";
  let tavleTimer = null, cellebredde = 0;

  function sisteFrist(iso) {            // fristen regnes ut dagen: 23:59:59 din tid
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    return m ? new Date(+m[1], +m[2] - 1, +m[3], 23, 59, 59) : null;
  }
  function nedtelling(iso) {
    const ms = sisteFrist(iso) - Date.now();
    if (!(ms > 0)) return "00:00:00";
    const s = Math.floor(ms / 1000);
    return to(Math.floor(s / 3600)) + ":" + to(Math.floor(s / 60) % 60) + ":" + to(s % 60);
  }
  const ute = j => { const d = dagerTil(j.deadline); return d !== null && d < 0 ? 1 : 0; };   // frist ute: nederst
  function tavleRader() {
    const skjul = ["AVSLAG", "IKKE AKTUELL"];
    const aapne = JOBS.filter(j => j.state === "open" && !skjul.includes(jobbStatus(j.key)))
      .sort((a, b) => ute(a) - ute(b) || (a.deadline || "9999").localeCompare(b.deadline || "9999") || String(b.first_seen || "").localeCompare(String(a.first_seen || "")));
    const rader = aapne.slice(0, 8).map(j => {
      const d = dagerTil(j.deadline), egen = jobbStatus(j.key);
      let status = "NY", klasse = "";
      if (d !== null && d < 0) { status = "GATE STENGT"; klasse = "graa"; }
      else if (d === 0) { status = "SISTE UTROP"; klasse = "rod"; }
      else if (d !== null && d <= 3) { status = "BOARDING"; klasse = "gul"; }
      if (egen !== "NY") { status = egen; klasse = egen === "SKAL SØKE" ? (klasse || "gul") : "gronn"; }
      const frist = d === null ? "UKJENT" : d === 0 ? nedtelling(j.deadline) : j.deadline.slice(8, 10) + "." + j.deadline.slice(5, 7);
      return { key: j.key, klasse, ny: nyISjekken(j), lenke: j.link,
        sett: kortDato(j.first_seen), arb: kortNavn(maalFor(j.target) || { name: j.target }), stilling: j.title || j.text || "", frist, status };
    });
    if (aapne.length > 8) rader.push({ key: "+", klasse: "graa", melding: "OG " + (aapne.length - 8) + " TIL · SE LEDIG NÅ UNDER", kort: "OG " + (aapne.length - 8) + " TIL" });
    if (!aapne.length) rader.push({ key: "tom", klasse: "graa", melding: "INGEN AVGANGER · RADAREN LYTTER", kort: "INGEN AVGANGER" });
    JOBS.filter(j => j.state === "closed").sort((a, b) => String(b.closed_at || "").localeCompare(String(a.closed_at || ""))).slice(0, 2)
      .forEach(j => rader.push({ key: j.key, klasse: "avgaatt", lenke: j.link, sett: kortDato(j.first_seen), arb: kortNavn(maalFor(j.target) || { name: j.target }),
        stilling: j.title || j.text || "", frist: kortDato(j.closed_at), status: "AVGÅTT" }));
    /* få stillinger ute: fyll tavla med når arbeidsgiverne pleier å lyse ut. Det er
       anslag fra config.json, så de står under en egen strek og aldri over en ekte stilling. */
    const ledigPlass = 6 - Math.max(1, aapne.length);
    const vinduer = (STATUS && Array.isArray(STATUS.windows)) ? STATUS.windows : [];
    if (ledigPlass > 0 && vinduer.length) {
      const ventes = vinduer.map((w, i) => ({ w, i, d: w.opens ? dagerTil(w.opens) : null }))
        .filter(v => v.d === null || v.d >= -90)
        .sort((a, b) => (a.d === null) - (b.d === null) || a.d - b.d || a.i - b.i)
        .slice(0, ledigPlass);
      if (ventes.length) rader.push({ key: "skille", skille: "VENTES · ANSLAG FRA TIDLIGERE ÅR" });
      ventes.forEach(({ w, i, d }) => rader.push({ key: "v" + i, klasse: "ventes", vindu: true, sett: "",
        arb: kortNavn(maalMedId(w.target) || { name: w.target }), stilling: w.label,
        frist: w.opens ? w.opens.slice(8, 10) + "." + w.opens.slice(5, 7) : "",
        status: d === null ? "LØPENDE" : d > 1 ? "OM " + d + " D" : "ÅPENT NÅ" }));
    }
    return rader;
  }
  const fyll = (s, n) => String(s || "").toUpperCase().replace(/\s+/g, " ").slice(0, n).padEnd(n, " ");

  /* hvor mange tegn får plass, og hvilke kolonner gir det */
  function oppsett(el) {
    const probe = document.createElement("span");
    probe.className = "fl"; probe.textContent = "M"; probe.style.visibility = "hidden";
    el.appendChild(probe);
    const st = getComputedStyle(probe);
    cellebredde = probe.getBoundingClientRect().width + parseFloat(st.marginRight || 0);
    probe.remove();
    const cs = getComputedStyle(el);
    const plass = el.clientWidth - parseFloat(cs.paddingLeft || 0) - parseFloat(cs.paddingRight || 0);
    const n = Math.max(24, Math.floor((plass - 1) / (cellebredde || 10)));
    if (n >= 62) return { n, linjer: [[["sett", 6], ["arb", 10], ["stilling", n - 6 - 10 - 8 - 11 - 4], ["frist", 8], ["status", 11]]] };
    if (n >= 46) return { n, linjer: [[["arb", 9], ["stilling", n - 9 - 8 - 11 - 3], ["frist", 8], ["status", 11]]] };
    return { n, linjer: [[["stilling", n]], [["arb", n - 8 - 11 - 2], ["frist", 8], ["status", 11]]] };   // mobil: to linjer per stilling
  }
  const TITLER = { sett: "SETT", arb: "ARBEIDSGIVER", stilling: "STILLING", frist: "FRIST", status: "STATUS" };

  function visTavle(animer) {
    const el = $("avganger");
    if (!el) return;
    const o = oppsett(el), rader = tavleRader();
    const linjetekst = (rad, linje) => rad.melding !== undefined
      ? (linje === o.linjer[0] ? fyll(rad.melding.length <= o.n ? rad.melding : rad.kort, o.n) : null)   // smal tavle: kort beskjed, ikke en avkuttet
      : linje.map(([felt, bredde]) => fyll(rad[felt], bredde)).join(" ");
    const hode = `<div class="fhode">` + o.linjer[o.linjer.length - 1].map(([felt, bredde]) =>
      `<span style="width:${(bredde * cellebredde).toFixed(1)}px">${TITLER[felt]}</span>`).join(`<span style="width:${cellebredde.toFixed(1)}px"></span>`) + `</div>`;

    /* behold cellene som finnes, så bare det som er endret vipper. En celle som
       fortsatt ruller tar med seg landingstiden sin, så den lander når den skulle
       selv om tavla tegnes om hvert sekund (nedtellingen på fristdagen). */
    const gamle = new Map([...el.querySelectorAll(".flinje")].map(l =>
      [l.dataset.id, [...l.children].map(c => c.dataset.maal !== undefined ? { land: c._land } : c.textContent)]));
    const forsteGang = !el.querySelector(".flinje");
    let html = hode, linjenr = 0;
    rader.forEach(rad => {
      if (rad.skille !== undefined) { html += `<div class="fskille"><span>${esc(rad.skille)}</span></div>`; return; }
      const linjer = o.linjer.map(l => linjetekst(rad, l)).filter(t => t !== null);
      html += `<${rad.lenke ? `a href="${trygLenke(rad.lenke)}" target="_blank" rel="noopener"` : "div"} class="frad ${rad.klasse}${rad.ny ? " blink" : ""}" data-key="${esc(rad.key)}"${rad.vindu ? " data-vindu" : ""}>`;
      linjer.forEach((tekst, i) => {
        const id = rad.key + "#" + i, forrige = gamle.get(id);
        html += `<div class="flinje" data-id="${esc(id)}" data-tekst="${esc(tekst)}" data-nr="${linjenr++}">` +
          [...tekst].map((c, k) => {
            const foer = forrige !== undefined ? forrige[k] : undefined;
            const lik = c === " " || foer === c;                                    // tomme felt vipper ikke
            const rullet = foer !== null && typeof foer === "object";               // var midt i en vipp
            return `<span class="fl${c === " " ? " blank" : ""}"${lik ? "" : ` data-maal="${esc(c)}"${rullet ? ` data-rull="${Math.round(foer.land) || 0}"` : ""}`}>${lik ? esc(c) : " "}</span>`;
          }).join("") + `</div>`;
      });
      html += `</${rad.lenke ? "a" : "div"}>`;
    });
    el.innerHTML = html;
    vipp(el, animer !== false && !rolig, forsteGang);
  }

  /* la bokstavene rulle og lande, venstre mot høyre. Én timer for hele tavla. */
  function vipp(el, animer, forsteGang) {
    clearInterval(tavleTimer);
    let celler = [...el.querySelectorAll(".fl[data-maal]")];
    if (!animer) {
      /* stille oppdatering (nedtellingen): sett nye tegn rett inn, men la det som alt rullet rulle ferdig */
      celler.forEach(c => { if (rolig || c.dataset.rull === undefined) { c.textContent = c.dataset.maal; c.removeAttribute("data-maal"); } });
      celler = celler.filter(c => c.dataset.maal !== undefined);
      if (!celler.length) return;
    }
    const start = performance.now();
    celler.forEach(c => {
      const kol = [...c.parentNode.children].indexOf(c), linje = +c.parentNode.dataset.nr;
      c._land = +c.dataset.rull > 0 ? +c.dataset.rull                       // arvet: lander når den opprinnelig skulle
        : start + (forsteGang ? 260 + linje * 130 + kol * 26 : 120 + kol * 14);
    });
    tavleTimer = setInterval(() => {
      const naa = performance.now();
      let igjen = 0;
      for (const c of celler) {
        if (!c.dataset.maal) continue;
        if (naa >= c._land) { c.textContent = c.dataset.maal; c.removeAttribute("data-maal"); c.classList.add("landet"); }
        else { igjen++; if (naa > c._land - 520) c.textContent = TEGN[Math.floor(Math.random() * TEGN.length)]; }
      }
      if (!igjen) clearInterval(tavleTimer);
    }, 45);
  }

  /* ================= logg og løpende tekst ================= */
  const TYPE = {
    new: ["●", "NY STILLING", "gul"], closed: ["○", "LUKKET", "graa"], fail: ["✕", "FEIL", "rod"],
    back: ["✓", "TILBAKE", "gronn"], reminder: ["◷", "FRIST", "gul"], check: ["·", "SJEKK", "graa"],
    deadline: ["◷", "FRIST FUNNET", "gul"], empty: ["!", "TOM SIDE", "rod"]
  };
  const hendelser = () => (STATUS && Array.isArray(STATUS.events)) ? STATUS.events : null;
  function hendelseTekst(e) {
    const hvem = e.target ? maalNavn(e.target).split("·")[0].trim() : "";
    return (hvem ? hvem + ": " : "") + (e.text || "");
  }
  function loggHtml(antall, nyeFra) {
    const ev = hendelser();
    if (!ev) return `<div class="tom">LOGGEN STARTER VED NESTE SJEKK.</div>`;
    if (!ev.length) return `<div class="tom">INGENTING I LOGGEN ENNÅ.</div>`;
    return `<div class="logg">` + ev.slice(0, antall).map(e => {
      const [ikon, navn, farge] = TYPE[e.type] || TYPE.check;
      const tekst = esc(hendelseTekst(e));
      return `<div class="lrad ${farge}${e.type === "check" ? " svak" : ""}${nyeFra && e.t === nyeFra ? " inn" : ""}">
        <span class="lt">${esc(kortDato(e.t))} ${esc(klokke(e.t))}</span><span class="li">${ikon}</span>
        <span class="lx"><b>${navn}</b> ${e.link ? `<a href="${trygLenke(e.link)}" target="_blank" rel="noopener">${tekst}</a>` : tekst}</span></div>`;
    }).join("") + `</div>`;
  }
  function visLogg(nyeFra) {
    if ($("loggliste")) $("loggliste").innerHTML = loggHtml(30, nyeFra);
    if ($("tvlogg")) $("tvlogg").innerHTML = loggHtml(11, nyeFra);
  }
  function visTicker() {
    const el = $("ticker");
    if (!el) return;
    const ev = (hendelser() || []).slice(0, 10);
    if (!ev.length) { el.hidden = true; document.body.classList.remove("harticker"); return; }
    const biter = ev.map(e => {
      const [ikon, navn, farge] = TYPE[e.type] || TYPE.check;
      return `<span class="tb ${farge}">${ikon} ${navn}</span> ${esc(hendelseTekst(e))} <span class="tt">${esc(relTid(e.t).toUpperCase())}</span>`;
    }).join(`<span class="tsk">✦</span>`) + `<span class="tsk">✦</span>`;
    const tegn = ev.reduce((n, e) => n + hendelseTekst(e).length + 30, 0);
    el.hidden = false; document.body.classList.add("harticker");
    /* to like halvdeler: når første er rullet ut, står den andre der den første begynte */
    el.innerHTML = `<div class="spor" style="animation-duration:${Math.max(30, Math.round(tegn * 0.16))}s"><span>${biter}</span><span aria-hidden="true">${biter}</span></div>`;
  }

  /* ================= trendlinjer ================= */
  function trendSvg(id, b, h) {
    const hist = (STATUS && Array.isArray(STATUS.history)) ? STATUS.history.slice(-60) : [];
    const pkt = hist.map(x => (x.n && typeof x.n[id] === "number") ? x.n[id] : null);
    const tall = pkt.filter(v => v !== null);
    if (tall.length >= 3) {
      const maks = Math.max(...tall), min = Math.min(...tall), spenn = (maks - min) || 1;
      const xy = [];
      pkt.forEach((v, i) => { if (v !== null) xy.push([(pkt.length === 1 ? 0 : i / (pkt.length - 1)) * (b - 6) + 3, h - 4 - (v - min) / spenn * (h - 8)]); });
      /* samme regel som ukesmailen: null, eller under halvparten av det vanlige (medianen før) */
      const siste = tall[tall.length - 1], foer = tall.slice(0, -1).sort((a, b) => a - b), vanlig = foer[Math.floor(foer.length / 2)];
      const fall = siste === 0 || siste < vanlig / 2;
      return { html: `<svg class="spark${fall ? " fall" : ""}" viewBox="0 0 ${b} ${h}" data-punkter="${tall.length}" preserveAspectRatio="none">
          <polyline points="${xy.map(p => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ")}"/><circle cx="${xy[xy.length - 1][0].toFixed(1)}" cy="${xy[xy.length - 1][1].toFixed(1)}" r="2.2"/></svg>`,
        tekst: "ANNONSER, SISTE " + tall.length + " SJEKKER · " + min + (min === maks ? "" : "–" + maks) + (fall ? " · FALL" : ""), fall };
    }
    /* ingen tall (NAV teller ikke annonser, eller for få sjekker): vis om sjekkene gikk bra */
    const gikk = hist.map(x => (x.fail || []).includes(id) ? 0 : (x.n && id in x.n) ? 1 : null).filter(v => v !== null).slice(-30);
    if (gikk.length < 3) return null;
    const bb = b / gikk.length;
    return { html: `<svg class="streker" viewBox="0 0 ${b} ${h}" data-punkter="${gikk.length}" preserveAspectRatio="none">` +
        gikk.map((ok, i) => `<rect class="${ok ? "ok" : "feil"}" x="${(i * bb + 0.6).toFixed(1)}" y="${ok ? h * 0.35 : 2}" width="${Math.max(1, bb - 1.2).toFixed(1)}" height="${ok ? h * 0.65 - 2 : h - 4}"/>`).join("") + `</svg>`,
      tekst: "SISTE " + gikk.length + " SJEKKER · " + gikk.filter(v => !v).length + " FEILET", fall: false };
  }
  function visTrender() {
    document.querySelectorAll("[data-trend]").forEach(el => {
      const t = trendSvg(el.dataset.trend, 150, 26);
      el.innerHTML = t ? t.html + `<span${t.fall ? ' class="rod"' : ""}>${esc(t.tekst)}</span>` : "";
    });
    const tv = $("tvtrender");
    if (tv) tv.innerHTML = ((STATUS && STATUS.targets) || []).map(m => {
      const t = trendSvg(m.id, 300, 46);
      return `<div class="tvtrend"><b>${esc(kortNavn(m))}</b>${t ? t.html + `<span${t.fall ? ' class="rod"' : ""}>${esc(t.tekst)}</span>` : `<span>TREND KOMMER ETTER TRE SJEKKER</span>`}</div>`;
    }).join("");
  }

  /* ================= klokker som teller ================= */
  function vanligPause() {
    const kj = ((STATUS && STATUS.runs) || []).map(Date.parse).filter(n => !isNaN(n)).sort((a, b) => a - b);
    if (kj.length < 4) return "";
    const hull = kj.slice(1).map((t, i) => t - kj[i]).sort((a, b) => a - b);
    return " · VANLIG PAUSE ~" + varighet(hull[Math.floor(hull.length / 2)] / 60000);
  }
  function tikk() {
    if (STATUS && STATUS.generated) {
      const s = Math.max(0, Math.floor((Date.now() - Date.parse(STATUS.generated)) / 1000));
      if (s <= 12 * 3600) $("rtekst").textContent = "RADAR: SIST SJEKKET " + to(Math.floor(s / 3600)) + ":" + to(Math.floor(s / 60) % 60) + ":" + to(s % 60) + " SIDEN" + vanligPause();
    }
    document.querySelectorAll("[data-siste]").forEach(el => { el.textContent = "FRIST I DAG · " + nedtelling(el.dataset.siste) + " IGJEN"; });
    document.querySelectorAll("[data-ned]").forEach(el => { el.textContent = nedtelling(el.dataset.ned); });
    /* siste dag: fristkolonnen på tavla teller ned uten å vippe hele raden */
    if (JOBS.some(j => j.state === "open" && dagerTil(j.deadline) === 0) && !document.hidden) visTavle(false);
  }

  /* ================= ny data mens siden står åpen ================= */
  let toastTimer = null;
  function nyData() {
    const t = $("toast");
    if (t) {
      t.textContent = "NY DATA · " + klokke(STATUS.generated);
      t.classList.add("vis");
      clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("vis"), 6000);
    }
    const s = $("scope");
    if (s) { s.classList.add("glimt"); setTimeout(() => s.classList.remove("glimt"), 2200); }
    const nye = JOBS.filter(j => j.state === "open" && nyISjekken(j));
    if (nye.length) nyAvgang(nye);
  }

  /* ================= NY AVGANG: en ny stilling tar skjermen i fem sekunder ================= */
  let avgangTimer = null;
  function brekk(tekst, bredde, maks) {       // hele ord per linje, så lange titler ikke kuttes midt i
    const linjer = [];
    let linje = "";
    for (const ord of String(tekst).toUpperCase().replace(/\s+/g, " ").trim().split(" ")) {
      if (linje && (linje + " " + ord).length > bredde) { linjer.push(linje); linje = ord.slice(0, bredde); }
      else linje = (linje ? linje + " " + ord : ord).slice(0, bredde);
    }
    if (linje) linjer.push(linje);
    if (linjer.length > maks) linjer[maks - 1] = linjer[maks - 1].slice(0, bredde - 1) + "…";   // kuttet: si fra
    return linjer.slice(0, maks);
  }
  function nyAvgang(nye) {
    const el = $("avgang");
    if (!el) return;
    const j = nye[0], d = dagerTil(j.deadline);
    const linjer = [["NY AVGANG", "topp"], [kortNavn(maalFor(j.target) || { name: j.target }), "arb"]]
      .concat(brekk(j.title || j.text || "", 22, 3).map(l => [l, ""]))
      .concat([[d === null ? "FRIST UKJENT" : "FRIST " + j.deadline.slice(8, 10) + "." + j.deadline.slice(5, 7) + "." + j.deadline.slice(0, 4), "avfrist"]])
      .concat(nye.length > 1 ? [["+ " + (nye.length - 1) + " TIL", "avfrist"]] : []);
    let n = 0;
    el.innerHTML = `<div class="avkort" role="alert">` + linjer.map(([tekst, klasse]) =>
      `<div class="avlinje ${klasse}" data-tekst="${esc(tekst)}">` + [...tekst].map(c =>
        `<span class="fl${c === " " ? " blank" : ""}" style="animation-delay:${n++ * 14}ms">${esc(c)}</span>`).join("") + `</div>`).join("")
      + `<div class="avhint">${tv ? "" : "TRYKK FOR BOARDINGKORT"}</div></div>`;
    el.dataset.key = j.key;
    el.hidden = false;
    clearTimeout(avgangTimer);
    avgangTimer = setTimeout(lukkAvgang, 5000);
  }
  function lukkAvgang() {
    clearTimeout(avgangTimer);
    const el = $("avgang");
    if (el) { el.hidden = true; el.innerHTML = ""; }
  }

  /* ================= kort for én kilde: trykk på en blip ================= */
  let valgtBlip = null;
  function visBlipkort() {
    const el = $("blipkort");
    if (!el) return;
    const t = valgtBlip !== null && !tv ? maalMedId(valgtBlip) : null;
    if (!t) { el.hidden = true; el.innerHTML = ""; return; }
    const aapne = JOBS.filter(j => j.state === "open" && j.target === t.name);
    const trend = trendSvg(t.id, 150, 26);
    el.hidden = false;
    el.innerHTML = `<div class="bkhode"><b>${esc(t.label || t.name)}</b><button class="plukk" data-bliplukk aria-label="Lukk">✕</button></div>
      <div class="bkrad"><span>RADAR</span><b class="${t.ok ? "gronn" : "rod"}">${t.ok ? "OK" : "FEIL x" + esc(t.consecutive_failures || 1)}</b></div>
      ${!t.ok && t.error ? `<div class="bkfeil">${esc(t.error)}</div>` : ""}
      <div class="bkrad"><span>ANNONSER PÅ SIDEN NÅ</span><b>${esc(t.listings ?? "–")}</b></div>
      <div class="bkrad"><span>STILLINGER DU VIL HA${t.ok ? "" : " · SIST KJENT"}</span><b class="${aapne.length ? "gul" : ""}">${aapne.length}</b></div>
      ${aapne.map(j => `<a class="bkjobb" href="#jobb=${encodeURIComponent(j.key)}" data-kort="${esc(j.key)}">${esc(j.title || j.text)}</a>`).join("")}
      ${trend ? `<div class="trend">${trend.html}<span${trend.fall ? ' class="rod"' : ""}>${esc(trend.tekst)}</span></div>` : ""}
      <div class="bact"><button class="btn smal" data-tilmaal="${esc(t.id)}">Gå til arbeidsgiveren</button>
        <a class="btn smal" href="${trygLenke(t.url)}" target="_blank" rel="noopener">Karrieresiden ↗</a></div>`;
  }
  function velgBlip(id) {
    valgtBlip = valgtBlip === id ? null : id;
    visBlipkort();
    document.querySelectorAll(".blip").forEach(b => b.classList.toggle("valgt", b.dataset.blip === valgtBlip));
  }

  /* ================= TV-modus ================= */
  const VISNINGER = ["avganger", "logg", "trender"];
  let tv = false, tvRunde = null, tvHent = null, tvSkift = null, laas = null, visning = 0;
  async function holdVaaken() {
    try { if (tv && navigator.wakeLock && !document.hidden) laas = await navigator.wakeLock.request("screen"); } catch (e) {}
  }
  function settVisning(i) {
    visning = i % VISNINGER.length;
    const p = $("tvpanel");
    if (p) p.dataset.vis = VISNINGER[visning];
    if (VISNINGER[visning] === "avganger") visTavle(true);
  }
  function tvPaa(fullskjerm) {
    if (tv) return;
    tv = true;
    document.body.classList.add("tv");
    valgtBlip = null;
    if (fullskjerm) { try { const r = document.documentElement.requestFullscreen && document.documentElement.requestFullscreen(); if (r && r.catch) r.catch(() => {}); } catch (e) {} }
    holdVaaken();
    settVisning(0);
    tvRunde = setInterval(() => settVisning(visning + 1), 20000);
    tvHent = setInterval(() => { if (!document.hidden) lastStatus(true); }, 60000);
    /* flytt bildet noen piksler hvert tiende minutt, så ingenting brenner seg fast */
    let n = 0;
    tvSkift = setInterval(() => { n++; document.body.style.setProperty("--skift", `${(n % 3 - 1) * 5}px, ${(n % 2) * 5}px`); }, 600000);
    window.scrollTo(0, 0);
    render();
  }
  function tvAv() {
    if (!tv) return;
    tv = false;
    document.body.classList.remove("tv");
    [tvRunde, tvHent, tvSkift].forEach(clearInterval);
    try { if (laas) laas.release(); } catch (e) {}
    laas = null;
    try { if (document.fullscreenElement) document.exitFullscreen(); } catch (e) {}
    const p = $("tvpanel"); if (p) p.dataset.vis = "avganger";
    if (new URLSearchParams(location.search).has("tv")) history.replaceState(null, "", location.pathname);
    render();
  }
  document.addEventListener("keydown", e => { if (e.key === "Escape") tvAv(); });
  const rullTil = el => { if (el) el.scrollIntoView({ behavior: rolig ? "auto" : "smooth", block: "center" }); };
  document.addEventListener("click", e => {
    const av = e.target.closest("#avgang");
    if (av) {                                   // NY AVGANG: et trykk lukker, og åpner boardingkortet
      const key = av.dataset.key;
      lukkAvgang();
      if (!tv && key && window.aapneKort) aapneKort(key);
      return;
    }
    if (e.target.closest("#tvknapp")) { e.preventDefault(); tvPaa(true); return; }
    if (tv) { if (!e.target.closest("a")) tvAv(); return; }
    if (e.target.closest("[data-bliplukk]")) { velgBlip(valgtBlip); return; }
    const til = e.target.closest("[data-tilmaal]");
    if (til) { rullTil(document.querySelector(`#board [data-maal="${CSS.escape(til.dataset.tilmaal)}"]`)); return; }
    const rad = e.target.closest("#avganger .frad");
    if (rad) {
      if (rad.dataset.vindu !== undefined) { rullTil($("vinduer")); return; }
      /* vanlig trykk åpner boardingkortet; ctrl/cmd-trykk åpner fortsatt annonsen i ny fane */
      if (!e.metaKey && !e.ctrlKey && !e.shiftKey && window.aapneKort && aapneKort(rad.dataset.key)) e.preventDefault();
      return;
    }
    const b = e.target.closest("[data-blip]");
    if (b) velgBlip(b.dataset.blip);
  });
  document.addEventListener("keydown", e => {   // blipper er knapper: Enter og mellomrom virker
    const b = (e.key === "Enter" || e.key === " ") && !tv && e.target.closest && e.target.closest("[data-blip]");
    if (b) { e.preventDefault(); velgBlip(b.dataset.blip); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) holdVaaken(); });
  let bredde = window.innerWidth;
  window.addEventListener("resize", () => { if (Math.abs(window.innerWidth - bredde) > 20) { bredde = window.innerWidth; visTavle(false); } });

  /* ================= samlet ================= */
  let forsteTavle = true;
  function render() {
    const endret = window.radarEndret === true;
    window.radarEndret = false;
    visScope();
    document.querySelectorAll(".blip").forEach(b => b.classList.toggle("valgt", b.dataset.blip === valgtBlip));
    visBlipkort();
    visTavle(forsteTavle || endret ? true : false);
    forsteTavle = false;
    visLogg(endret && STATUS ? STATUS.generated : null);
    visTicker();
    visTrender();
    if (endret && STATUS) nyData();
    tikk();
  }
  setInterval(tikk, 1000);
  if (new URLSearchParams(location.search).has("tv")) tvPaa(false);

  return { render, tvPaa, tvAv, er: () => tv };
})();
window.display = display;
/* app.js kan ha rukket å hente data før denne fila var lastet */
if (STATUS) display.render();
