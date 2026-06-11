const DEFAULT_URL = "https://all-gravy.typeform.com/to/pUNVlfuq?typeform-source=7-eleven.career.emply.com";

const STEPS = ["profile", "addresses", "run"];

const WHY_TEMPLATES = {
  "Универсальный":
    "1) Jeg elsker kundeservice og kontakt med mennesker.\n" +
    "2) 7-Eleven er en dynamisk arbejdsplads med gode udviklingsmuligheder.\n" +
    "3) Jeg vil gerne være en del af et stærkt brand med fleksible arbejdstider.",
  "Энергичный":
    "1) Jeg er en energisk og positiv person — perfekt til 7-Elevens travle miljø.\n" +
    "2) Jeg vil lære at arbejde under pres og udvikle mine kommunikationsevner.\n" +
    "3) 7-Eleven har en stærk virksomhedskultur, som jeg gerne vil være en del af.",
  "С опытом":
    "1) Jeg har erfaring med kundeservice og kan håndtere en travl hverdag.\n" +
    "2) Jeg er pålidelig, mødestabil og altid klar til at give en hånd.\n" +
    "3) Jeg ser 7-Eleven som et sted, hvor jeg kan vokse fagligt og personligt.",
  "Студент":
    "1) Jeg er studerende og søger et fleksibelt job, der passer til mit skema.\n" +
    "2) Jeg er ansvarlig, lærenem og glad for at arbejde med mennesker.\n" +
    "3) 7-Eleven er kendt for at investere i sine medarbejdere — det tiltrækker mig.",
};

const SELF_TEMPLATES = {
  "Стандартный": "Energisk, serviceminded og pålidelig teamplayer med smil på læben.",
  "Краткий": "Positiv, hurtig og altid klar til at hjælpe kunderne.",
  "Опытный": "Erfaren i kundeservice, struktureret og altid mødestabil.",
  "Молодой": "Ung, engageret og lærenem — klar til nye udfordringer.",
};

let state = null;
let addresses = {};
let addressLookup = {};
let selectedAddresses = new Set();
let activeStep = "profile";
let activeLogFilter = "all";
let newAddresses = new Set();
let lastAddrJson = "";
let homeTimer = null;

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (m) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[m]));
}

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 250); }, 3200);
}

function setValue(id, value) {
  const el = $(id);
  if (el && document.activeElement !== el) el.value = value ?? "";
}

/* ---------- home location & routing ---------- */

let home = { addr: "", coords: null };
const geoCache = JSON.parse(localStorage.getItem("geoCache7e") || "{}");
const geoQueue = [];
let geoBusy = false;

function loadHome() {
  try { home = JSON.parse(localStorage.getItem("home7e")) || { addr: "", coords: null }; }
  catch (e) { home = { addr: "", coords: null }; }
}
function saveHome() { localStorage.setItem("home7e", JSON.stringify(home)); }
function homeStatusText() {
  if (home.coords) return "📍 по геолокации";
  if (home.addr) return home.addr;
  return "не задано";
}

// Геокодер через свой сервер (/api/geocode → DAWA + кэш на диске): быстро,
// без CORS и внешних лимитов. localStorage — второй уровень кэша.
function pumpGeo() {
  if (geoBusy || !geoQueue.length) return;
  geoBusy = true;
  const job = geoQueue.shift();
  fetch(`/api/geocode?q=${encodeURIComponent(job.q)}`)
    .then((r) => r.json())
    .then((d) => {
      if (d && d.ok) {
        geoCache[job.key] = { lat: +d.lat, lng: +d.lng };
        localStorage.setItem("geoCache7e", JSON.stringify(geoCache));
        job.cbs.forEach((cb) => cb(geoCache[job.key]));
      }
    })
    .catch(() => {})
    .finally(() => { setTimeout(() => { geoBusy = false; pumpGeo(); }, 120); });
}
function geocode(key, q, cb) {
  if (geoCache[key]) { cb(geoCache[key]); return; }
  const queued = geoQueue.find((j) => j.key === key);
  if (queued) { queued.cbs.push(cb); return; }  // не плодим дубли в очереди
  geoQueue.push({ key, q, cbs: [cb] });
  pumpGeo();
}
function homePoint(cb) {
  if (home.coords) { cb(home.coords); return; }
  if (home.addr) geocode("addr:" + home.addr, home.addr, cb);
}
function haversine(a, b) {
  const R = 6371, rad = (d) => (d * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat), dLng = rad(b.lng - a.lng);
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
}
function travelMode() {
  return localStorage.getItem("travelMode7e") || "transit";
}
function routeUrl(store) {
  const dest = encodeURIComponent(store + ", Denmark");
  let origin = "";
  if (home.coords) origin = `${home.coords.lat},${home.coords.lng}`;
  else if (home.addr) origin = encodeURIComponent(home.addr);
  return `https://www.google.com/maps/dir/?api=1${origin ? `&origin=${origin}` : ""}` +
    `&destination=${dest}&travelmode=${travelMode()}`;
}
function openRoute(store) { window.open(routeUrl(store), "_blank", "noopener"); }

/* ---------- public-transport summary (via local /api/transit proxy) ---------- */

const MODE_ICON = {
  BUS: "bus", COACH: "bus", TRAM: "tram", SUBWAY: "tram", METRO: "tram",
  RAIL: "train", REGIONAL_RAIL: "train", REGIONAL_FAST_RAIL: "train", SUBURBAN: "train",
  HIGHSPEED_RAIL: "train", LONG_DISTANCE: "train", NIGHT_RAIL: "train", FERRY: "ferry",
};
const transitCache = JSON.parse(localStorage.getItem("transitCache7e") || "{}");
const transitQueue = [];
let transitActive = 0;
let scrollTimer = null;
// transitous serializes requests per IP, so high parallelism only hurts — keep low.
const TRANSIT_CONCURRENCY = 2;

function pumpTransit() {
  while (transitActive < TRANSIT_CONCURRENCY && transitQueue.length) {
    const job = transitQueue.shift();
    transitActive++;
    fetch(job.url).then((r) => r.json()).then(job.cb).catch(() => job.cb(null))
      .finally(() => { transitActive--; pumpTransit(); });
  }
}
function requestTransit(url, cb) { transitQueue.push({ url, cb }); pumpTransit(); }

function transitFromParam() {
  if (home.coords) return `${home.coords.lat},${home.coords.lng}`;
  if (home.addr) return home.addr;
  return "";
}
function fmtTransit(s) {
  if (!s || !s.ok) return "";
  const modes = (s.legs || [])
    .map((l) => icon7(MODE_ICON[l.mode] || "train", 12) + (l.route ? " " + escapeHtml(l.route) : ""))
    .join(" › ") || icon7("walk", 12);
  const tr = s.transfers === 0 ? "без пересадок" : `${s.transfers} перес.`;
  return `${modes} · ${tr} · ${s.minutes} мин`;
}

function loadTransitFor(el) {
  const from = transitFromParam();
  if (!from) return;
  const store = el.getAttribute("data-transit");
  const key = `${from}::${store}`;
  if (transitCache[key]) { el.innerHTML = fmtTransit(transitCache[key]); return; }
  if (el.dataset.loading) return;
  el.dataset.loading = "1";
  el.innerHTML = icon7("bus", 12) + " …";
  const url = `/api/transit?from=${encodeURIComponent(from)}&to=${encodeURIComponent(store)}`;
  requestTransit(url, (res) => {
    el.dataset.loading = "";
    if (res) {
      transitCache[key] = res;
      localStorage.setItem("transitCache7e", JSON.stringify(transitCache));
    }
    el.innerHTML = fmtTransit(res);
  });
}

// Load transit only for rows currently in (or near) the visible area of the
// scrollable list. Robust across browsers and programmatic scrolling.
function loadVisibleTransit() {
  const from = transitFromParam();
  if (!from) return;
  const list = $("addressList");
  const lr = list.getBoundingClientRect();
  document.querySelectorAll(".transit-badge[data-transit]").forEach((el) => {
    const store = el.getAttribute("data-transit");
    const key = `${from}::${store}`;
    if (transitCache[key]) { el.innerHTML = fmtTransit(transitCache[key]); return; }
    if (el.dataset.loading) return;
    const r = el.getBoundingClientRect();
    if (r.bottom > lr.top - 150 && r.top < lr.bottom + 150) loadTransitFor(el);
  });
}

// Compute transit only for what the user actually looks at: cached rows fill
// instantly, selected stores load eagerly, and the rest load as they scroll
// into view. This avoids 41 slow serial router calls up front.
function updateTransitBadges() {
  const from = transitFromParam();
  if (!from) return;
  document.querySelectorAll(".transit-badge[data-transit]").forEach((el) => {
    const store = el.getAttribute("data-transit");
    const key = `${from}::${store}`;
    if (transitCache[key]) el.innerHTML = fmtTransit(transitCache[key]);
    else if (selectedAddresses.has(store)) loadTransitFor(el); // selected → load now
  });
  loadVisibleTransit();
}

function updateDistanceBadges() {
  if (!(home.coords || home.addr)) return; // no home set → no distances (route still works)
  homePoint((hp) => {
    document.querySelectorAll(".dist-badge[data-dist]").forEach((el) => {
      const addr = el.getAttribute("data-dist");
      const pc = (addr.match(/\b(\d{4})\b/) || [])[1];
      if (!pc) return;
      geocode("pc:" + pc, pc + " Danmark", (sp) => {
        const km = haversine(hp, sp);
        el.textContent = `≈ ${km < 10 ? km.toFixed(1) : Math.round(km)} км`;
        el.title = "Примерное расстояние по прямой от твоего адреса";
      });
    });
  });
}

/* ---------- profile <-> form ---------- */

function buildLookup(tree) {
  const map = {};
  for (const [region, cities] of Object.entries(tree)) {
    for (const [city, zones] of Object.entries(cities)) {
      for (const [zone, list] of Object.entries(zones)) {
        for (const addr of list) map[addr] = [region, city, zone];
      }
    }
  }
  return map;
}

function profileFromForm() {
  const selected = [...selectedAddresses];
  const [region, city, zone] = addressLookup[selected[0]] || ["Sjælland", "København", ""];
  return {
    personal: {
      first_name: $("first").value.trim(),
      last_name: $("last").value.trim(),
      email: $("email").value.trim(),
      phone: $("phone").value.replace(/\D/g, "").slice(0, 8),
      phone_country: "DK",
    },
    location: {
      region, city, zone,
      preferred_addresses: selected,
      selected_addresses: selected,
      fallback_strategy: "first_available",
    },
    job_preferences: {
      hours: $("hours").value,
      position: $("position").value,
      start_date: $("startDate").value || new Date(Date.now() + 14 * 864e5).toISOString().slice(0, 10),
    },
    answers: {
      why_7eleven: $("why").value,
      self_description: $("self").value,
      retail_experience: $("retailExp").value,
      work_experience: $("workExp").value.trim(),
    },
    attachments: { cv_path: $("cv").value.trim() },
  };
}

function fillProfile(p) {
  setValue("first", p.personal?.first_name);
  setValue("last", p.personal?.last_name);
  setValue("email", p.personal?.email);
  setValue("phone", p.personal?.phone);
  setValue("hours", p.job_preferences?.hours || "deltid");
  setValue("position", p.job_preferences?.position || "Butiksassistent");
  setValue("startDate", p.job_preferences?.start_date || new Date(Date.now() + 14 * 864e5).toISOString().slice(0, 10));
  setValue("cv", p.attachments?.cv_path);
  setValue("why", p.answers?.why_7eleven);
  setValue("self", p.answers?.self_description);
  setValue("retailExp", p.answers?.retail_experience || "");
  setValue("workExp", p.answers?.work_experience || "");
  selectedAddresses = new Set(p.location?.selected_addresses || p.location?.preferred_addresses || []);
}

/* ---------- validation / readiness ---------- */

function readiness() {
  const p = profileFromForm();
  const emailOk = p.personal.email.includes("@") && p.personal.email.split("@")[1]?.includes(".");
  const phoneOk = p.personal.phone.length === 8;
  const profileOk = !!(p.personal.first_name && p.personal.last_name && emailOk && phoneOk
    && p.answers.why_7eleven.trim().length >= 10 && p.answers.self_description.trim().length >= 3
    && p.attachments.cv_path);
  const addrOk = selectedAddresses.size > 0;
  return { emailOk, phoneOk, profileOk, addrOk };
}

function updateReadiness() {
  const r = readiness();

  const emailMark = $("emailMark");
  const email = $("email").value.trim();
  emailMark.textContent = email ? (r.emailOk ? "✓" : "✗") : "";
  emailMark.className = "mark" + (email ? (r.emailOk ? " ok" : " bad") : "");

  const phoneMark = $("phoneMark");
  const digits = $("phone").value.replace(/\D/g, "");
  phoneMark.textContent = digits ? `${digits.length}/8` : "";
  phoneMark.className = "mark counter" + (digits ? (r.phoneOk ? " ok" : " bad") : "");

  const whyLen = $("why").value.trim().length;
  $("whyCount").textContent = whyLen ? `${whyLen} симв.${whyLen < 30 ? " — маловато" : ""}` : "";

  $("selectedText").textContent = `Выбрано: ${selectedAddresses.size}`;
  $("metricSelected").textContent = selectedAddresses.size;

  document.querySelector('[data-flag="profile"]').className = "step-flag " + (r.profileOk ? "ok" : "warn");
  document.querySelector('[data-flag="addresses"]').className = "step-flag " + (r.addrOk ? "ok" : "warn");

  const missing = [];
  if (!r.profileOk) missing.push("профиль");
  if (!r.addrOk) missing.push("магазины");
  $("readySummary").innerHTML = missing.length
    ? `Не хватает: <b class="bad">${missing.join(", ")}</b>`
    : `Всё готово — <b class="ok">${selectedAddresses.size}</b> анкет(ы) к запуску`;
}

/* ---------- addresses ---------- */

function populateFilters() {
  const regions = Object.keys(addresses);
  const rSel = $("filterRegion");
  const prevR = rSel.value;
  rSel.innerHTML = `<option value="">Все регионы</option>` +
    regions.map((r) => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`).join("");
  if (prevR && regions.includes(prevR)) rSel.value = prevR;

  const cSel = $("filterCity");
  const prevC = cSel.value;
  const cities = [];
  for (const [region, cs] of Object.entries(addresses)) {
    if (rSel.value && region !== rSel.value) continue;
    for (const city of Object.keys(cs)) if (!cities.includes(city)) cities.push(city);
  }
  cities.sort((a, b) => a.localeCompare(b, "da"));
  cSel.innerHTML = `<option value="">Все города</option>` +
    cities.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  if (prevC && cities.includes(prevC)) cSel.value = prevC;
}

// расстояние по прямой до магазина, если индекс уже геокодирован (для сортировки)
function distKmSync(addr, hp) {
  const pc = (addr.match(/\b(\d{4})\b/) || [])[1];
  if (!pc || !hp) return null;
  const sp = geoCache["pc:" + pc];
  return sp ? haversine(hp, sp) : null;
}

let rerenderTimer = null;
function queueRerender() {
  clearTimeout(rerenderTimer);
  rerenderTimer = setTimeout(renderAddresses, 700);
}

function renderAddresses() {
  const query = $("addressSearch").value.trim().toLowerCase();
  const fRegion = $("filterRegion").value;
  const fCity = $("filterCity").value;
  const sort = $("sortAddr").value;
  const hp = home.coords || geoCache["addr:" + home.addr] || null;

  // плоский отфильтрованный список
  const rows = [];
  let total = 0;
  for (const [region, cities] of Object.entries(addresses)) {
    for (const [city, zones] of Object.entries(cities)) {
      for (const [zone, list] of Object.entries(zones)) {
        total += list.length;
        if (fRegion && region !== fRegion) continue;
        if (fCity && city !== fCity) continue;
        for (const addr of list) {
          if (query && !`${region} ${city} ${zone} ${addr}`.toLowerCase().includes(query)) continue;
          rows.push({ region, city, zone, addr, km: distKmSync(addr, hp) });
        }
      }
    }
  }
  $("addrTotal").textContent = `показано ${rows.length} из ${total} магазинов`;

  if (sort === "near") {
    rows.sort((a, b) => (a.km ?? 1e9) - (b.km ?? 1e9));
    if (!hp && (home.coords || home.addr)) {
      homePoint(queueRerender);  // сначала геокодируем домашний адрес, потом пересортируем
    }
    // догеокодировать недостающие индексы и пересортировать, когда подтянутся
    if (hp) {
      for (const r of rows) {
        if (r.km !== null) continue;
        const pc = (r.addr.match(/\b(\d{4})\b/) || [])[1];
        if (pc) geocode("pc:" + pc, pc + " Danmark", queueRerender);
      }
    }
  } else if (sort === "selected") {
    rows.sort((a, b) => (selectedAddresses.has(b.addr) - selectedAddresses.has(a.addr)) ||
      a.city.localeCompare(b.city, "da"));
  }

  let html = "";
  let lastGroup = null;
  for (const r of rows) {
    // в режиме «по городам» группируем заголовками; в остальных — сплошным списком
    if (sort === "city") {
      const group = `${r.zone}__${r.city}`;
      if (group !== lastGroup) {
        lastGroup = group;
        const crumb = r.zone === r.city ? r.region : `${r.city} · ${r.region}`;
        const count = rows.filter((x) => x.zone === r.zone && x.city === r.city).length;
        html += `<div class="zone-row"><span class="zone-name">${escapeHtml(r.zone)}` +
          `<em class="zone-crumb">${escapeHtml(crumb)} · ${count}</em></span>` +
          `<button class="zone-pick" data-zone="${escapeHtml(r.zone)}">выбрать всё</button></div>`;
      }
    }
    const isNew = newAddresses.has(r.addr);
    const checked = selectedAddresses.has(r.addr);
    html += `<div class="addr${isNew ? " new" : ""}${checked ? " picked" : ""}">` +
      `<label class="addr-main">` +
      `<input type="checkbox" value="${escapeHtml(r.addr)}" ${checked ? "checked" : ""}>` +
      `<span class="cbox" aria-hidden="true">${icon7("check", 12)}</span>` +
      `<span class="addr-text">` +
      `<span class="addr-name">${escapeHtml(r.addr)}${isNew ? ' <em class="tag-new">новый</em>' : ""}` +
      (sort !== "city" ? ` <em class="addr-crumb">${escapeHtml(r.zone === r.city ? r.region : r.city)}</em>` : "") +
      `</span>` +
      `<span class="transit-badge" data-transit="${escapeHtml(r.addr)}"></span>` +
      `</span></label>` +
      `<div class="addr-meta">` +
      `<span class="dist-badge" data-dist="${escapeHtml(r.addr)}">${r.km !== null ? `≈ ${r.km < 10 ? r.km.toFixed(1) : Math.round(r.km)} км` : ""}</span>` +
      `<button class="route-btn" data-route="${escapeHtml(r.addr)}" title="Открыть маршрут на карте">${icon7("route", 13)} Маршрут</button>` +
      `</div></div>`;
  }

  $("addressList").innerHTML = rows.length ? html : `<div class="empty">Ничего не найдено. Поменяй фильтры или нажми «Обновить базу с сайта».</div>`;

  $("addressList").querySelectorAll(".addr-main input").forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) selectedAddresses.add(input.value);
      else selectedAddresses.delete(input.value);
      input.closest(".addr").classList.toggle("picked", input.checked);
      updateReadiness();
    });
  });
  $("addressList").querySelectorAll(".zone-pick").forEach((btn) => {
    btn.addEventListener("click", () => toggleZone(btn.dataset.zone));
  });
  $("addressList").querySelectorAll(".route-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => { e.preventDefault(); openRoute(btn.dataset.route); });
  });
  updateReadiness();
  updateDistanceBadges();
  updateTransitBadges();
}

function zoneAddresses(zoneName) {
  const out = [];
  for (const cities of Object.values(addresses))
    for (const zones of Object.values(cities))
      for (const [zone, list] of Object.entries(zones))
        if (zone === zoneName) out.push(...list);
  return out;
}

function toggleZone(zoneName) {
  const items = zoneAddresses(zoneName);
  const allSelected = items.every((a) => selectedAddresses.has(a));
  for (const a of items) {
    if (allSelected) selectedAddresses.delete(a);
    else selectedAddresses.add(a);
  }
  renderAddresses();
}

/* ---------- queue / logs / report ---------- */

function statusLabel(s) {
  return { queued: "в очереди", running: "идёт", ok: "OK", error: "ошибка" }[s] || s;
}

function renderQueue(items) {
  $("metricQueued").textContent = items.length;
  $("metricOk").textContent = items.filter((i) => i.status === "ok").length;
  $("metricCheck").textContent = items.filter((i) => i.status === "error").length;
  if (!items.length) {
    $("queue").innerHTML = `<div class="empty">Очередь появится после запуска.</div>`;
    return;
  }
  $("queue").innerHTML = items.map((i) => `
    <div class="queue-row">
      <div class="queue-index">#${i.index}</div>
      <div>
        <div class="queue-address">${escapeHtml(i.address)}</div>
        <div class="queue-sub">${i.pid ? `PID ${i.pid}` : "ожидает"}</div>
      </div>
      <span class="chip ${i.status}">${escapeHtml(statusLabel(i.status))}</span>
      <div class="queue-sub">${escapeHtml(i.progress || "")}</div>
    </div>`).join("");
}

function renderLogs(logs) {
  const filtered = activeLogFilter === "all" ? logs : logs.filter((l) => l.level === activeLogFilter);
  $("logs").innerHTML = filtered.map((l) => `
    <div class="log-line ${l.level}">
      <span>${escapeHtml(l.time)}</span>
      <span class="log-level">${escapeHtml(l.level)}</span>
      <span class="log-message">${l.item ? `#${l.item} ` : ""}${escapeHtml(l.message)}</span>
    </div>`).join("");
  $("logs").scrollTop = $("logs").scrollHeight;
}

function renderReport(path) {
  if (!path) {
    $("reportBox").innerHTML = `<div class="empty">Отчёт появится после первого прогона.</div>`;
    return;
  }
  $("reportBox").innerHTML = `
    <div class="report-item">
      <strong>Последний отчёт готов</strong>
      <div class="report-path">${escapeHtml(path)}</div>
    </div>
    <button class="btn primary" id="openReportInline">Открыть отчёт</button>`;
  $("openReportInline").addEventListener("click", () => openReport(path));
}

function renderScanBanner(scan) {
  const banner = $("scanBanner");
  if (!scan) { banner.hidden = true; return; }
  banner.hidden = false;
  const added = scan.added?.length || 0;
  const removed = scan.removed?.length || 0;
  banner.className = "scan-banner" + (removed ? " warn" : "");
  banner.textContent = `Сканирование: всего ${scan.total} · новых ${added} · пропало ${removed}`;
}

function renderDbAge(days) {
  const el = $("dbAge");
  if (days === null || days === undefined) {
    el.hidden = false;
    el.className = "pill warn";
    el.textContent = "База магазинов: нет — нажми «Обновить с сайта»";
    return;
  }
  el.hidden = false;
  const d = Math.floor(days);
  const label = d === 0 ? "сегодня" : d === 1 ? "вчера" : `${d} дн. назад`;
  const stale = days >= 7;
  el.className = "pill " + (stale ? "warn" : "soft");
  el.textContent = `База магазинов: ${label}` + (stale ? " ⚠" : " ✓");
}

/* ---------- steps ---------- */

function goToStep(step) {
  activeStep = step;
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.dataset.view !== step; });
  document.querySelectorAll(".step").forEach((s) => s.classList.toggle("active", s.dataset.step === step));
  const idx = STEPS.indexOf(step);
  $("backBtn").hidden = idx === 0;
  $("nextBtn").hidden = idx === STEPS.length - 1;
  $("nextBtn").textContent = idx === 0 ? "Далее: магазины →" : "Далее: запуск →";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ---------- server sync ---------- */

async function refresh() {
  let next;
  try {
    next = await (await fetch("/api/state")).json();
  } catch (e) { return; }
  const first = !state;
  state = next;
  addresses = next.addresses || {};
  addressLookup = buildLookup(addresses);
  newAddresses = new Set(next.last_scan?.added || []);

  if (first) {
    $("url").value = DEFAULT_URL;
    fillProfile(next.profile);
  }

  const addrJson = JSON.stringify(addresses);
  if (first || addrJson !== lastAddrJson) {
    lastAddrJson = addrJson;
    populateFilters();
    renderAddresses();
  }

  $("runPill").textContent = next.running ? (next.current_run || "Выполняется") : "Готов";
  $("runPill").className = `run-pill ${next.running ? "running" : ""}`;
  renderDbAge(next.addresses_age_days);
  for (const id of ["dryRun", "realRun", "scanAddresses"]) $(id).disabled = next.running;
  $("stopRun").disabled = !next.running;

  renderQueue(next.items || []);
  renderLogs(next.logs || []);
  renderReport(next.last_report);
  renderScanBanner(next.last_scan);
  updateReadiness();
}

async function saveProfile(silent) {
  await fetch("/api/profile", { method: "POST", body: JSON.stringify(profileFromForm()) });
  if (!silent) toast("Профиль сохранён", "info");
}

async function scanAddresses() {
  if (!confirm("Обновить базу магазинов с сайта 7-Eleven? Занимает несколько секунд, анкеты не отправляются.")) return;
  await fetch("/api/discover", { method: "POST", body: JSON.stringify({ region: "" }) });
  toast("Обновление базы запущено", "info");
  await refresh();
}

async function run(dryRun) {
  const r = readiness();
  if (!r.addrOk) { toast("Отметь хотя бы один магазин", "warn"); goToStep("addresses"); return; }
  if (!r.profileOk) { toast("Профиль заполнен не полностью", "warn"); goToStep("profile"); return; }
  if (!dryRun && !confirm(`Отправить реальные анкеты по ${selectedAddresses.size} адресам?`)) return;
  await saveProfile(true);
  await fetch("/api/run", {
    method: "POST",
    body: JSON.stringify({ profile: profileFromForm(), dry_run: dryRun, url: $("url").value || DEFAULT_URL }),
  });
  toast(dryRun ? "Тестовый прогон запущен" : "Реальная отправка запущена", dryRun ? "info" : "warn");
  await refresh();
}

async function stopRun() {
  await fetch("/api/stop", { method: "POST" });
  toast("Остановлено", "warn");
  await refresh();
}

async function openReport(path) {
  await fetch(`/api/open?path=${encodeURIComponent(path)}`);
}

async function copyLogs() {
  const logs = state?.logs || [];
  const shown = activeLogFilter === "all" ? logs : logs.filter((l) => l.level === activeLogFilter);
  const text = shown.map((l) => `${l.time} [${l.level}]${l.item ? ` #${l.item}` : ""} ${l.message}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast(`Скопировано строк: ${shown.length}`, "info");
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast(`Скопировано строк: ${shown.length}`, "info"); }
    catch (e2) { toast("Не удалось скопировать", "error"); }
    document.body.removeChild(ta);
  }
}

/* ---------- templates ---------- */

function setupTemplates(selectId, targetId, templates) {
  const sel = $(selectId);
  for (const name of Object.keys(templates)) {
    const opt = document.createElement("option");
    opt.value = name; opt.textContent = name;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => {
    if (!sel.value) return;
    $(targetId).value = templates[sel.value];
    sel.value = "";
    updateReadiness();
    saveProfile(true);
  });
}

/* ---------- wiring ---------- */

function bindEvents() {
  $("resetUrl").addEventListener("click", () => { $("url").value = DEFAULT_URL; });
  $("saveProfile").addEventListener("click", () => saveProfile(false));
  $("dryRun").addEventListener("click", () => run(true));
  $("realRun").addEventListener("click", () => run(false));
  $("stopRun").addEventListener("click", stopRun);
  $("scanAddresses").addEventListener("click", scanAddresses);
  $("clearAddrs").addEventListener("click", () => { selectedAddresses.clear(); renderAddresses(); });

  $("homeAddr").addEventListener("input", () => {
    home.addr = $("homeAddr").value.trim();
    home.coords = null; // typed address overrides geolocation
    saveHome();
    $("homeStatus").textContent = homeStatusText();
    clearTimeout(homeTimer);
    homeTimer = setTimeout(() => { updateDistanceBadges(); updateTransitBadges(); }, 800);
  });
  $("travelMode").value = travelMode();
  $("travelMode").addEventListener("change", () => {
    localStorage.setItem("travelMode7e", $("travelMode").value);
  });
  $("useGeo").addEventListener("click", () => {
    if (!navigator.geolocation) { toast("Геолокация недоступна в этом браузере", "warn"); return; }
    $("homeStatus").textContent = "определяю…";
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        home.coords = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        saveHome();
        $("homeStatus").textContent = homeStatusText();
        updateDistanceBadges();
        updateTransitBadges();
        toast("Местоположение определено", "info");
      },
      () => { $("homeStatus").textContent = "не удалось"; toast("Не удалось получить геолокацию — нужно разрешение браузера", "warn"); },
      { enableHighAccuracy: false, timeout: 10000 },
    );
  });
  $("openReport").addEventListener("click", () => state?.last_report && openReport(state.last_report));
  $("copyLogs").addEventListener("click", copyLogs);
  $("addressSearch").addEventListener("input", renderAddresses);
  $("filterRegion").addEventListener("change", () => { populateFilters(); renderAddresses(); });
  $("filterCity").addEventListener("change", renderAddresses);
  $("sortAddr").addEventListener("change", renderAddresses);
  $("addressList").addEventListener("scroll", () => {
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(loadVisibleTransit, 120);
  });

  $("backBtn").addEventListener("click", () => goToStep(STEPS[Math.max(0, STEPS.indexOf(activeStep) - 1)]));
  $("nextBtn").addEventListener("click", () => goToStep(STEPS[Math.min(STEPS.length - 1, STEPS.indexOf(activeStep) + 1)]));
  document.querySelectorAll(".step").forEach((s) => s.addEventListener("click", () => goToStep(s.dataset.step)));

  document.querySelectorAll(".chip-btn[data-filter]").forEach((btn) => btn.addEventListener("click", () => {
    document.querySelectorAll(".chip-btn[data-filter]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeLogFilter = btn.dataset.filter;
    renderLogs(state?.logs || []);
  }));

  // live validation + debounced autosave on profile edits
  let saveTimer = null;
  document.querySelectorAll("#first,#last,#email,#phone,#hours,#position,#startDate,#cv,#why,#self")
    .forEach((el) => el.addEventListener("input", () => {
      updateReadiness();
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => saveProfile(true), 1200);
    }));

  setupTemplates("whyTpl", "why", WHY_TEMPLATES);
  setupTemplates("selfTpl", "self", SELF_TEMPLATES);
}

loadHome();
$("homeAddr").value = home.addr || "";
$("homeStatus").textContent = homeStatusText();
bindEvents();
goToStep("profile");
refresh();
setInterval(refresh, 1000);
