"use strict";
/* JOBBRADAR, versjon 4.
   Leser status.json og jobs.json som radaren skriver, og legger ditt eget lag
   oppå: søknadsstatus, notater og stempler. Det laget ligger bare i nettleseren. */

/* ---------- trygg lagring: localStorage der det finnes, minne ellers ---------- */
const store = (() => {
  let ok = true;
  const mem = {};
  try { localStorage.setItem("__t", "1"); localStorage.removeItem("__t"); }
  catch (e) { ok = false; }
  return {
    ok,
    get(k, f) {
      try { if (ok) { const v = localStorage.getItem(k); return v === null ? f : JSON.parse(v); } } catch (e) {}
      return (k in mem) ? mem[k] : f;
    },
    set(k, v) {
      try { if (ok) { localStorage.setItem(k, JSON.stringify(v)); return; } } catch (e) {}
      mem[k] = v;
    },
    keys(prefix) {
      const out = [];
      try {
        if (ok) { for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (k && k.startsWith(prefix)) out.push(k); } return out; }
      } catch (e) {}
      return Object.keys(mem).filter(k => k.startsWith(prefix));
    }
  };
})();

const $ = id => document.getElementById(id);
if (!store.ok) $("lagringsvarsel").style.display = "block";

/* alt som kommer fra radaren er skrapt fra eksterne sider: aldri rå HTML inn i siden */
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const trygLenke = u => /^https?:\/\//i.test(u) ? esc(u) : "#";

/* ---------- tid ---------- */
function relTid(iso) {
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (isNaN(m)) return "ukjent tid";
  if (m < 1) return "nå nettopp";
  if (m < 60) return m + " min siden";
  const t = Math.round(m / 60);
  if (t < 48) return t + " t siden";
  return Math.round(t / 24) + " d siden";
}
function kortDato(iso) {
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso || "") ? iso + "T12:00:00" : iso);
  if (isNaN(d)) return "?";
  return d.toLocaleDateString("nb-NO", { day: "numeric", month: "short" }).replace(/\./g, "").toUpperCase();
}
/* hele dager til en frist (YYYY-MM-DD), regnet i din tidssone. null = ingen dato */
function dagerTil(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  if (!m) return null;
  const idag = new Date(); idag.setHours(0, 0, 0, 0);
  return Math.round((new Date(+m[1], +m[2] - 1, +m[3]) - idag) / 86400000);
}
function fristInfo(iso) {
  const d = dagerTil(iso);
  if (d === null) return { tekst: "FRIST UKJENT", klasse: "ukjent", haster: false };
  const dato = " · " + kortDato(iso);
  if (d < 0) return { tekst: "FRIST UTE" + dato, klasse: "ute", haster: false };
  if (d === 0) return { tekst: "FRIST I DAG" + dato, klasse: "haster", haster: true };
  if (d === 1) return { tekst: "FRIST I MORGEN" + dato, klasse: "haster", haster: true };
  return { tekst: "FRIST OM " + d + " DAGER" + dato, klasse: d <= 3 ? "haster" : "", haster: d <= 3 };
}
function varighet(min) {
  if (min < 90) return Math.round(min) + " MIN";
  const t = min / 60;
  return (t < 10 ? t.toFixed(1).replace(".", ",") : Math.round(t)) + " T";
}

/* ---------- klokke ---------- */
function tikk() { $("klokke").textContent = new Date().toLocaleTimeString("nb-NO", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
tikk(); setInterval(tikk, 1000);

/* ---------- data ---------- */
let STATUS = null;   // status.json
let JOBS = [];       // jobs.json: alle ønskede stillinger radaren har sett

const STATUSER = ["NY", "SKAL SØKE", "SØKT", "INTERVJU", "TILBUD", "AVSLAG", "IKKE AKTUELL"];
const I_GANG = ["SKAL SØKE", "SØKT", "INTERVJU", "TILBUD"];
const FERDIG = ["AVSLAG", "IKKE AKTUELL"];
const FLAP_FARGE = { "SKAL SØKE": "gul", "SØKT": "gronn", "INTERVJU": "gronn", "TILBUD": "gronn", "AVSLAG": "graa", "IKKE AKTUELL": "graa" };
const JOBB = "radar.jobb.";

function jobbLagret(key) { const d = store.get(JOBB + key, null); return d && typeof d === "object" ? d : null; }
function jobbStatus(key) { const d = jobbLagret(key); return d && STATUSER.includes(d.status) ? d.status : "NY"; }
/* lagrer også et øyeblikksbilde av stillingen, så søknaden din står igjen
   selv etter at radaren har glemt utlysningen */
function lagreJobb(job, endring) {
  const d = Object.assign({ status: "NY", notat: "" }, jobbLagret(job.key) || {}, endring, {
    endret: new Date().toISOString(),
    tittel: job.title || job.text || "",
    maal: job.target || "",
    lenke: job.link || "",
    frist: job.deadline || null
  });
  store.set(JOBB + job.key, d);
}
function maalNavn(navn) {
  const t = ((STATUS && STATUS.targets) || []).find(x => x.name === navn);
  return (t && t.label) || navn || "";
}
/* alle stillinger du har rørt, også de som har falt ut av jobs.json */
function alleSporede() {
  const kjente = new Map(JOBS.map(j => [j.key, j]));
  for (const k of store.keys(JOBB)) {
    const key = k.slice(JOBB.length);
    if (kjente.has(key)) continue;
    const d = jobbLagret(key);
    if (d) kjente.set(key, { key, title: d.tittel, target: d.maal, link: d.lenke, deadline: d.frist, state: "closed", glemt: true });
  }
  return [...kjente.values()];
}

function statusVelger(key, verdi) {
  const farge = FLAP_FARGE[verdi] || "";
  return `<select class="flap ${farge}" data-jstatus="${esc(key)}" aria-label="Søknadsstatus">` +
    STATUSER.map(s => `<option${s === verdi ? " selected" : ""}>${s}</option>`).join("") + `</select>`;
}

/* ---------- NÅ: åpne stillinger ---------- */
function visJobber() {
  const aapne = JOBS.filter(j => j.state === "open");
  const antall = { ALLE: aapne.length };
  for (const j of aapne) { const s = jobbStatus(j.key); antall[s] = (antall[s] || 0) + 1; }

  let valgt = store.get("radar.filter", "ALLE");
  if (valgt !== "ALLE" && !antall[valgt]) valgt = "ALLE";
  const brikker = ["ALLE", ...STATUSER.filter(s => antall[s])];
  $("filter").innerHTML = aapne.length
    ? brikker.map(s => `<button class="chip${s === valgt ? " on" : ""}" data-filter="${s}">${s}<b>${antall[s]}</b></button>`).join("")
    : "";

  const rang = j => (FERDIG.includes(jobbStatus(j.key)) ? 1 : 0);
  const liste = aapne
    .filter(j => valgt === "ALLE" || jobbStatus(j.key) === valgt)
    .sort((a, b) => rang(a) - rang(b)
      || (a.deadline || "9999").localeCompare(b.deadline || "9999")
      || String(b.first_seen || "").localeCompare(String(a.first_seen || "")));

  $("jobber").innerHTML = liste.length ? liste.map(j => {
    const s = jobbStatus(j.key), f = fristInfo(j.deadline), d = jobbLagret(j.key);
    const dempet = FERDIG.includes(s);
    return `<div class="jobb${f.haster && !dempet && s !== "SØKT" ? " haster" : ""}${dempet ? " dempet" : ""}">
      <div class="jtop"><span class="frist ${f.klasse}">${esc(f.tekst)}</span>${statusVelger(j.key, s)}</div>
      <a class="jtittel" href="${trygLenke(j.link)}" target="_blank" rel="noopener">${esc(j.title || j.text)}</a>
      <div class="jmeta"><b>${esc(maalNavn(j.target))}</b> · SETT FØRSTE GANG ${esc(kortDato(j.first_seen))} (${esc(relTid(j.first_seen).toUpperCase())})</div>
      <textarea class="notat" data-jnotat="${esc(j.key)}" placeholder="Notat. F.eks: søkt 14.11, ringte leder, venter svar …">${esc(d ? d.notat : "")}</textarea>
    </div>`;
  }).join("") : `<div class="tom">${aapne.length ? "INGEN STILLINGER MED DENNE STATUSEN." : "INGEN STILLINGER DU VIL HA ER UTE NÅ. STILLE ER OGSÅ ET SVAR."}</div>`;

  const nye = antall.NY || 0;
  $("lediglinje").hidden = !aapne.length;
  $("ledigtotal").textContent = (aapne.length === 1 ? "1 STILLING DU VIL HA ER LEDIG NÅ" : aapne.length + " STILLINGER DU VIL HA ER LEDIG NÅ")
    + (nye ? " · " + nye + (nye === 1 ? " NY" : " NYE") : "");
  try { if (navigator.setAppBadge) { nye ? navigator.setAppBadge(nye) : navigator.clearAppBadge(); } } catch (e) {}
}

/* ---------- SØKNADER ---------- */
function visSoknader() {
  const alle = alleSporede();
  const tall = {};
  for (const j of alle) { const s = jobbStatus(j.key); if (s !== "NY") tall[s] = (tall[s] || 0) + 1; }
  $("teller").innerHTML = ["SKAL SØKE", "SØKT", "INTERVJU", "TILBUD"].map(s =>
    `<div class="tall${tall[s] ? " har" : ""}"><div class="n">${tall[s] || 0}</div><div class="l">${s}</div></div>`).join("");

  /* lengst framme i løpet øverst, avslag nederst */
  const REKKE = ["TILBUD", "INTERVJU", "SØKT", "SKAL SØKE", "AVSLAG"];
  const mine = alle.filter(j => REKKE.includes(jobbStatus(j.key)))
    .sort((a, b) => REKKE.indexOf(jobbStatus(a.key)) - REKKE.indexOf(jobbStatus(b.key))
      || (a.deadline || "9999").localeCompare(b.deadline || "9999"));
  $("mine").innerHTML = mine.length ? `<div class="rader">` + mine.map(j => {
    const lukket = j.state !== "open";
    const info = lukket ? "UTLYSNING LUKKET" : fristInfo(j.deadline).tekst;
    return `<div class="rad"><div class="rn"><a href="${trygLenke(j.link)}" target="_blank" rel="noopener">${esc(j.title || j.text)}</a>
      <small>${esc(maalNavn(j.target))} · ${esc(info)}</small></div>
      <div class="rd">${statusVelger(j.key, jobbStatus(j.key))}</div></div>`;
  }).join("") + `</div>` : `<div class="tom">INGEN SØKNADER I GANG. SETT STATUS PÅ EN STILLING OVER, SÅ HAVNER DEN HER.</div>`;
}

/* ---------- TAVLE: arbeidsgiverne ---------- */
const GAMLE_ID = [[/vy/i, "vy"], [/iss/i, "iss"], [/sas/i, "sas"], [/norwegian/i, "norwegian"], [/wider/i, "wideroe"]];
function maalId(t) {
  if (t.id) return t.id;
  const g = GAMLE_ID.find(([rx]) => rx.test(t.name || ""));   // status.json fra før versjon 4
  return g ? g[1] : String(t.name || "x").toLowerCase().replace(/[^a-z0-9]+/g, "-");
}
const MAAL_FARGE = { "ÅPEN": "gul", "MULIG": "gronn", "LUKKET": "graa" };

function visTavle() {
  const maal = (STATUS && STATUS.targets) || [];
  $("board").innerHTML = maal.length ? maal.map(t => {
    const id = maalId(t);
    const d = store.get("radar.maal." + id, { status: "UKJENT", notat: "", sist: "aldri" });
    const aapne = JOBS.filter(j => j.state === "open" && j.target === t.name);
    const radar = t.ok ? `<b>OK</b>`
      : `<b class="warn">FEIL x${esc(t.consecutive_failures || 1)}${t.error ? " · " + esc(String(t.error).slice(0, 60)) : ""}</b>`;
    const ledig = aapne.length ? `<div class="ledig"><div class="lh"><span class="dot gul"></span><span>LEDIG NÅ · ${aapne.length === 1 ? "1 STILLING" : aapne.length + " STILLINGER"}${t.ok ? "" : " · SIST KJENT"}</span></div>`
      + aapne.map(o => `<a href="${trygLenke(o.link)}" target="_blank" rel="noopener">${esc(o.text || o.title)}</a>`).join("") + `</div>` : "";
    return `<div class="brow${aapne.length ? " lit" : ""}" data-maal="${esc(id)}">
      <div class="btop">
        <div class="dest">${esc(t.label || t.name)}${t.sub ? `<small>${esc(t.sub)}</small>` : ""}</div>
        <select class="flap ${MAAL_FARGE[d.status] || ""}" data-mstatus aria-label="Status ${esc(t.label || t.name)}">
          ${["UKJENT", "ÅPEN", "MULIG", "LUKKET"].map(s => `<option${s === d.status ? " selected" : ""}>${s}</option>`).join("")}
        </select>
      </div>
      <div class="bmeta">
        <span>RADAR: ${radar}</span>
        <span>NYE TREFF: <b${(t.new_hits || 0) > 0 ? ' class="nytt"' : ""}>${esc(t.new_hits || 0)}</b></span>
        <span>ANNONSER NÅ: <b>${esc(t.listings ?? "–")}</b></span>
        <span>SIST STEMPLET: <b data-sist>${esc(d.sist)}</b></span>
      </div>
      ${ledig}
      <textarea class="notat" data-mnotat placeholder="Notat om arbeidsgiveren. F.eks: åpen søknad sendt, kontaktperson …">${esc(d.notat)}</textarea>
      <div class="bact">
        <button class="btn smal gul" data-stemple>Stemple sjekket nå</button>
        <a class="btn smal" href="${trygLenke(t.url)}" target="_blank" rel="noopener">Åpne karrieresiden</a>
      </div>
    </div>`;
  }).join("") : `<div class="tom" style="padding:12px">INGEN ARBEIDSGIVERE I STATUS.</div>`;
}

/* ---------- HISTORIKK ---------- */
function visHistorikk() {
  const lukket = JOBS.filter(j => j.state === "closed")
    .sort((a, b) => String(b.closed_at || "").localeCompare(String(a.closed_at || ""))).slice(0, 40);
  $("historikk").innerHTML = lukket.length ? `<div class="rader">` + lukket.map(j => {
    const dager = Math.max(0, Math.round((Date.parse(j.closed_at) - Date.parse(j.first_seen)) / 86400000));
    return `<div class="rad"><div class="rn">${esc(j.title || j.text)}<small>${esc(maalNavn(j.target))} · UTE ${esc(kortDato(j.first_seen))} – ${esc(kortDato(j.closed_at))}${isNaN(dager) ? "" : " · " + dager + " D"}</small></div>
      <div class="rd graa">${esc(jobbStatus(j.key) === "NY" ? "" : jobbStatus(j.key))}</div></div>`;
  }).join("") + `</div>`
    : `<div class="tom">INGEN LUKKEDE UTLYSNINGER ENNÅ. LISTA FYLLES ETTER HVERT SOM RADAREN SER STILLINGER KOMME OG GÅ.</div>`;
}

/* ---------- RADAR: helse ---------- */
function visHelse() {
  const rader = [];
  const kj = ((STATUS && STATUS.runs) || []).map(Date.parse).filter(n => !isNaN(n)).sort((a, b) => a - b);
  const naa = Date.now();
  if (kj.length) {
    const doegn = kj.filter(t => naa - t <= 86400000).length;
    rader.push(["Sjekker siste døgn", "så ofte GitHub faktisk slapp radaren til", String(doegn)]);
    const uke = kj.filter(t => naa - t <= 7 * 86400000);
    if (uke.length >= 2) {
      let hull = 0;
      for (let i = 1; i < uke.length; i++) hull = Math.max(hull, uke[i] - uke[i - 1]);
      rader.push(["Lengste pause mellom to sjekker", "siste 7 dager, av det loggen dekker", varighet(hull / 60000)]);
    }
  } else {
    rader.push(["Sjekker siste døgn", "loggen starter ved første kjøring av versjon 4", "–"]);
  }
  const ch = STATUS && STATUS.channels;
  if (ch) rader.push(["Varsling", ch.email || ch.push ? "slik når radaren deg" : "ingen kanal er aktiv", "EPOST " + (ch.email ? "PÅ" : "AV") + " · PUSH " + (ch.push ? "PÅ" : "AV")]);
  const feil = ((STATUS && STATUS.targets) || []).filter(t => !t.ok);
  rader.push(["Kilder", feil.length ? "feiler: " + feil.map(t => t.label || t.name).join(", ") : "alle svarer", feil.length ? feil.length + " FEILER" : "OK"]);
  if (STATUS && STATUS.calendar) {
    const url = new URL("frister.ics", location.href);
    const lenke = location.protocol === "https:" ? "webcal://" + url.host + url.pathname : url.href;
    rader.push(["Frister i kalenderen", "abonner én gang, så dukker fristene opp i telefonens kalender", `<a href="${esc(lenke)}">ABONNER</a>`, true]);
  }
  $("helse").innerHTML = rader.map(([navn, under, verdi, html]) =>
    `<div class="rad"><div class="rn">${esc(navn)}<small>${esc(under)}</small></div><div class="rd">${html ? verdi : esc(verdi)}</div></div>`).join("");

  const al = ((STATUS && STATUS.recent_alerts) || []).slice(0, 8);
  $("alerts").innerHTML = al.length
    ? al.map(a => `<a class="alert" href="${trygLenke(a.link)}" target="_blank" rel="noopener"><span class="at">${esc(maalNavn(a.target).toUpperCase())} · ${esc(relTid(a.time).toUpperCase())}</span>${esc(a.text)}</a>`).join("")
    : `<div class="tom">INGEN TREFF I RADARLOGGEN.</div>`;
}

function visAlt() { visJobber(); visSoknader(); visTavle(); visHistorikk(); visHelse(); }

/* ---------- hent status.json og jobs.json ---------- */
async function hent(fil) {
  const r = await fetch("./" + fil, { cache: "no-store" });
  if (!r.ok) throw new Error(fil);
  return r.json();
}
/* status.json fra før versjon 4 har ingen jobs.json ved siden av seg */
function jobberFraStatus(st) {
  const ut = [];
  for (const t of (st && st.targets) || []) for (const o of t.open || []) {
    ut.push({ key: o.key || t.name + "|" + o.link, target: t.name, text: o.text, title: o.title || o.text, link: o.link,
      deadline: o.deadline || null, first_seen: st.generated, state: "open" });
  }
  return ut;
}

let laster = false;
async function lastStatus(stille) {
  if (laster) return;
  /* ikke tegn om mens du skriver i et notat eller har en velger åpen */
  const a = document.activeElement;
  if (stille === true && a && /^(TEXTAREA|SELECT|INPUT)$/.test(a.tagName)) return;
  laster = true;
  const rdot = $("rdot"), rtekst = $("rtekst");
  if (stille !== true) rtekst.textContent = "RADAR: SJEKKER…";
  try {
    STATUS = await hent("status.json");
    try { JOBS = (await hent("jobs.json")).jobs || []; }
    catch (e) { JOBS = jobberFraStatus(STATUS); }
    const min = Math.round((Date.now() - Date.parse(STATUS.generated)) / 60000);
    /* GitHub's free schedule skips most slots, hours can pass between runs,
       so only warn when a check is clearly overdue (12 h), not after 45 min */
    if (min <= 720) { rdot.className = "dot live"; rtekst.textContent = "RADAR: SIST SJEKKET " + relTid(STATUS.generated).toUpperCase(); }
    else { rdot.className = "dot stale"; rtekst.textContent = "RADAR: IKKE SJEKKET PÅ " + relTid(STATUS.generated).replace(" siden", "").toUpperCase() + " · TRYKK KJØR SJEKK NÅ"; }
  } catch (e) {
    STATUS = null; JOBS = [];
    rdot.className = "dot off";
    rtekst.textContent = "RADAR: FRAKOBLET · SIDEN VIRKER, MEN UTEN SANNTID";
  } finally { laster = false; }
  visAlt();
  if (!STATUS) $("jobber").innerHTML = `<div class="tom">FANT IKKE status.json. KJØR SIDEN PÅ GITHUB PAGES SAMMEN MED RADAREN FOR SANNTID.</div>`;
}
$("oppdater").addEventListener("click", () => lastStatus());
lastStatus();
/* hent ny status hvert 5. minutt, og med en gang du kommer tilbake til fanen/appen */
setInterval(() => { if (!document.hidden) lastStatus(true); }, 5 * 60 * 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) lastStatus(true); });

/* ---------- det du gjør: status, notater, stempler ---------- */
function finnJobb(key) { return alleSporede().find(j => j.key === key) || { key }; }

document.addEventListener("change", e => {
  const el = e.target;
  if (el.dataset.jstatus !== undefined) {
    lagreJobb(finnJobb(el.dataset.jstatus), { status: el.value });
    el.blur();
    visJobber(); visSoknader(); visHistorikk();
  } else if (el.dataset.mstatus !== undefined) {
    lagreMaal(el.closest(".brow"));
    el.className = "flap " + (MAAL_FARGE[el.value] || "");
  }
});
document.addEventListener("input", e => {
  const el = e.target;
  if (el.dataset.jnotat !== undefined) lagreJobb(finnJobb(el.dataset.jnotat), { notat: el.value });
  else if (el.dataset.mnotat !== undefined) lagreMaal(el.closest(".brow"));
});
document.addEventListener("click", e => {
  const el = e.target.closest("[data-filter],[data-stemple]");
  if (!el) return;
  if (el.dataset.filter !== undefined) { store.set("radar.filter", el.dataset.filter); visJobber(); }
  else {
    const rad = el.closest(".brow");
    rad.querySelector("[data-sist]").textContent = new Date().toLocaleString("nb-NO", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    lagreMaal(rad);
  }
});
function lagreMaal(rad) {
  store.set("radar.maal." + rad.dataset.maal, {
    status: rad.querySelector("[data-mstatus]").value,
    notat: rad.querySelector("[data-mnotat]").value,
    sist: rad.querySelector("[data-sist]").textContent
  });
}

/* ---------- vinduer nedtelling ---------- */
document.querySelectorAll("[data-frist]").forEach(el => {
  const d = Math.ceil((Date.parse(el.dataset.frist) - Date.now()) / 86400000);
  el.textContent = d > 1 ? "OM " + d + " DAGER" : (d >= -90 ? "NÅ" : "PASSERT");
});

/* ---------- sikkerhetskopi ---------- */
const melding = t => { $("kopimelding").textContent = t; };
function lagKopi() {
  const data = {};
  for (const k of store.keys("radar.")) data[k] = store.get(k, null);
  return JSON.stringify({ app: "jobbradar", versjon: 1, laget: new Date().toISOString(), data }, null, 1);
}
function hentInn(tekst) {
  let kopi;
  try { kopi = JSON.parse(tekst); } catch (e) { return melding("DETTE ER IKKE EN GYLDIG KOPI (KLARTE IKKE Å LESE TEKSTEN)."); }
  if (!kopi || kopi.app !== "jobbradar" || typeof kopi.data !== "object" || kopi.data === null) return melding("DETTE ER IKKE EN KOPI FRA JOBBRADAR.");
  const felt = Object.entries(kopi.data).filter(([k]) => k.startsWith("radar."));
  if (!felt.length) return melding("KOPIEN ER TOM.");
  if (!confirm("Hente inn " + felt.length + " lagrede felt fra kopien" + (kopi.laget ? " (laget " + new Date(kopi.laget).toLocaleString("nb-NO") + ")" : "") + "? Felt med samme navn her blir overskrevet.")) return;
  for (const [k, v] of felt) store.set(k, v);
  visAlt();
  melding("HENTET INN " + felt.length + " FELT.");
}
$("eksport").addEventListener("click", () => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lagKopi()], { type: "application/json" }));
  a.download = "jobbradar-kopi-" + new Date().toISOString().slice(0, 10) + ".json";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  melding("KOPI LASTET NED (" + store.keys("radar.").length + " FELT).");
});
$("eksporttekst").addEventListener("click", () => {
  const t = $("kopitekst"); t.value = lagKopi(); t.focus(); t.select();
  melding("MARKERT. KOPIER TEKSTEN OG LIM DEN INN PÅ DEN ANDRE ENHETEN.");
});
$("importtekst").addEventListener("click", () => hentInn($("kopitekst").value));
$("importfil").addEventListener("click", () => $("filvelger").click());
$("filvelger").addEventListener("change", e => {
  const fil = e.target.files && e.target.files[0];
  if (!fil) return;
  fil.text().then(hentInn).catch(() => melding("KLARTE IKKE Å LESE FILA."));
  e.target.value = "";
});

/* ---------- app: åpner uten dekning ---------- */
if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
