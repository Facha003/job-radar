"use strict";
/* JOBBRADAR, versjon 5: alt som beveger seg.
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
  const klokke = iso => { const d = new Date(iso); return isNaN(d) ? "" : to(d.getHours()) + ":" + to(d.getMinutes()); };

  /* ================= radarskjerm ================= */
  function visScope() {
    const el = $("scope");
    if (!el) return;
    const maal = (STATUS && STATUS.targets) || [];
    if (!maal.length) { el.innerHTML = `<div class="tom">INGEN RADARDATA.</div>`; return; }
    const ferske = STATUS.generated;
    const indre = maal.filter(t => t.id !== "nav"), ytre = maal.filter(t => t.id === "nav");
    const plasser = [];
    indre.forEach((t, i) => plasser.push({ t, vinkel: (i * 360 / indre.length + 24) % 360, r: i % 2 ? 66 : 44 }));
    ytre.forEach((t, i) => plasser.push({ t, vinkel: (200 + i * 40) % 360, r: 92 }));

    const blipper = plasser.map(({ t, vinkel, r }) => {
      const rad = (vinkel - 90) * Math.PI / 180, x = +(Math.cos(rad) * r).toFixed(1), y = +(Math.sin(rad) * r).toFixed(1);
      const aapne = JOBS.filter(j => j.state === "open" && j.target === t.name);
      const nytt = aapne.some(j => j.first_seen === ferske);
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
      <defs><linearGradient id="hale" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#3ec97e" stop-opacity="0"/><stop offset="1" stop-color="#3ec97e" stop-opacity=".34"/></linearGradient></defs>
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
      return { key: j.key, klasse, ny: j.first_seen === (STATUS && STATUS.generated), lenke: j.link,
        sett: kortDato(j.first_seen), arb: kortNavn(maalFor(j.target) || { name: j.target }), stilling: j.title || j.text || "", frist, status };
    });
    if (aapne.length > 8) rader.push({ key: "+", klasse: "graa", melding: "OG " + (aapne.length - 8) + " TIL · SE LEDIG NÅ UNDER" });
    if (!aapne.length) rader.push({ key: "tom", klasse: "graa", melding: "INGEN AVGANGER · RADAREN LYTTER" });
    JOBS.filter(j => j.state === "closed").sort((a, b) => String(b.closed_at || "").localeCompare(String(a.closed_at || ""))).slice(0, 2)
      .forEach(j => rader.push({ key: j.key, klasse: "avgaatt", lenke: j.link, sett: kortDato(j.first_seen), arb: kortNavn(maalFor(j.target) || { name: j.target }),
        stilling: j.title || j.text || "", frist: kortDato(j.closed_at), status: "AVGÅTT" }));
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
      ? (linje === o.linjer[0] ? fyll(rad.melding, o.n) : null)
      : linje.map(([felt, bredde]) => fyll(rad[felt], bredde)).join(" ");
    const hode = `<div class="fhode">` + o.linjer[o.linjer.length - 1].map(([felt, bredde]) =>
      `<span style="width:${(bredde * cellebredde).toFixed(1)}px">${TITLER[felt]}</span>`).join(`<span style="width:${cellebredde.toFixed(1)}px"></span>`) + `</div>`;

    /* behold cellene som finnes, så bare det som er endret vipper. En celle som
       fortsatt ruller regnes som ulandet (null), så den ruller videre etter omtegning. */
    const gamle = new Map([...el.querySelectorAll(".flinje")].map(l =>
      [l.dataset.id, [...l.children].map(c => c.dataset.maal !== undefined ? null : c.textContent)]));
    const forsteGang = !el.querySelector(".flinje");
    let html = hode, linjenr = 0;
    rader.forEach(rad => {
      const linjer = o.linjer.map(l => linjetekst(rad, l)).filter(t => t !== null);
      html += `<${rad.lenke ? `a href="${trygLenke(rad.lenke)}" target="_blank" rel="noopener"` : "div"} class="frad ${rad.klasse}${rad.ny ? " blink" : ""}" data-key="${esc(rad.key)}">`;
      linjer.forEach((tekst, i) => {
        const id = rad.key + "#" + i, forrige = gamle.get(id);
        html += `<div class="flinje" data-id="${esc(id)}" data-tekst="${esc(tekst)}" data-nr="${linjenr++}">` +
          [...tekst].map((c, k) => {
            const lik = c === " " || (forrige !== undefined && forrige[k] === c);   // tomme felt vipper ikke
            const rullet = forrige !== undefined && forrige[k] === null;            // var midt i en vipp
            return `<span class="fl${c === " " ? " blank" : ""}"${lik ? "" : ` data-maal="${esc(c)}"${rullet ? " data-rull" : ""}`}>${lik ? esc(c) : " "}</span>`;
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
      c._land = forsteGang ? 260 + linje * 130 + kol * 26 : 120 + kol * 14;
    });
    tavleTimer = setInterval(() => {
      const naa = performance.now() - start;
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
    back: ["✓", "TILBAKE", "gronn"], reminder: ["◷", "FRIST", "gul"], check: ["·", "SJEKK", "graa"]
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
  document.addEventListener("click", e => {
    if (e.target.closest("#tvknapp")) { e.preventDefault(); tvPaa(true); }
    else if (tv && !e.target.closest("a")) tvAv();
    else {
      const b = e.target.closest("[data-blip]");
      const kort = b && document.querySelector(`#board [data-maal="${CSS.escape(b.dataset.blip)}"]`);
      if (kort && !tv) kort.scrollIntoView({ behavior: rolig ? "auto" : "smooth", block: "center" });
    }
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
