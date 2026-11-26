import { initializeApp } from "https://www.gstatic.com/firebasejs/11.10.0/firebase-app.js";
import { getAuth, GoogleAuthProvider, onAuthStateChanged, signInWithPopup, signOut }
  from "https://www.gstatic.com/firebasejs/11.10.0/firebase-auth.js";
import { getFirestore, doc, getDoc, setDoc, updateDoc, deleteDoc, collection, query, orderBy, limit, onSnapshot, where }
  from "https://www.gstatic.com/firebasejs/11.10.0/firebase-firestore.js";
import { DEFAULTS } from "./defaults.js";

const REPO = "Peper0007/olx-ua-free-finder";
const WORKFLOW = "finder.yml";
const SOURCE_NAMES = { olx: "OLX.ua", olx_cheap: "OLX.ua дешёвые" };
const STATUS = { new: "новое", interesting: "интересно", taken: "забрал", gone: "уже забрали", spam: "спам", low_profit: "невыгодно", skip: "мимо" };
// statuses that move a find out of «Находки» into its own tab
const FOLDERS = {
  taken: { tab: "taken", title: "Забранные", badge: "✅ Забрал", empty: "Пока ничего не забрано. Нажми «забрал» у находки — она переедет сюда." },
  gone: { tab: "gone", title: "Забранные другими", badge: "🏃 Уже забрал кто-то другой", empty: "Пусто. Нажми «уже забрали», если вещь успел забрать другой человек." },
  spam: { tab: "spam", title: "Спам", badge: "🚫 Спам — ненастоящее объявление", empty: "Пусто. Нажми «спам» у ненастоящего объявления — оно переедет сюда." },
};

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Firebase Hosting serves the project's web config at this reserved URL.
const config = await (await fetch("/__/firebase/init.json")).json();
const app = initializeApp(config);
const auth = getAuth(app);
const db = getFirestore(app);

let settings = structuredClone(DEFAULTS);
let finds = [];
let unsubFinds = null, unsubRuns = null, unsubLimits = null, unsubTaken = null, unsubHits = null;
let taken = [];                       // all finds whose status is a folder (taken / gone / spam)
let latest = [], hitsAll = [];        // latest-N window + every find at/above the threshold
let todayOnly = false;
let limitRuns = [];
let lastRun = null;

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 4000);
}

// ---------- auth ----------
$("#btn-login").onclick = async () => {
  try { await signInWithPopup(auth, new GoogleAuthProvider()); }
  catch (e) { $("#login-error").textContent = e.message; }
};
$("#btn-logout").onclick = () => signOut(auth);

onAuthStateChanged(auth, async (user) => {
  if (!user) {
    $("#login").classList.remove("hidden"); $("#app").classList.add("hidden");
    unsubFinds?.(); unsubRuns?.(); unsubLimits?.(); unsubTaken?.(); unsubHits?.();
    return;
  }
  $("#user-email").textContent = user.email;
  try {
    await loadSettings();
  } catch (e) {
    if (e?.code === "permission-denied") {
      $("#login-error").textContent = "Нет доступа для " + user.email + ". Дашборд открыт только владельцу.";
      await signOut(auth);
      return;
    }
    console.error(e);
    toast("Ошибка загрузки настроек: " + (e?.message || e) + " — обнови страницу (Ctrl+F5).", true);
  }
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  subscribeFinds(); subscribeRuns(); subscribeLimits(); loadGhToken();
});

// ---------- tabs ----------
document.querySelectorAll(".tabs button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + b.dataset.tab));
  };
});

// ---------- settings ----------
function deepMerge(base, over) {
  const out = structuredClone(base);
  for (const [k, v] of Object.entries(over || {})) {
    if (v && typeof v === "object" && !Array.isArray(v) && typeof out[k] === "object" && !Array.isArray(out[k])) out[k] = deepMerge(out[k], v);
    else if (v !== null && v !== undefined) out[k] = v;
  }
  return out;
}

async function loadSettings() {
  const snap = await getDoc(doc(db, "config", "settings"));
  settings = deepMerge(DEFAULTS, snap.exists() ? snap.data() : {});
  fillForms();
}

function lines(text) { return String(text ?? "").split("\n").map((s) => s.trim()).filter(Boolean); }
function setVal(sel, v) { const el = $(sel); if (el) el.value = v ?? ""; }
function setChk(sel, v) { const el = $(sel); if (el) el.checked = !!v; }
function getVal(sel) { return $(sel)?.value ?? ""; }

function fillForms() {
  const s = settings;
  setChk("#s-paused", s.paused);
  setChk("#s-telegram", s.telegram_enabled !== false);
  setVal("#s-min", s.min_score); if ($("#s-min-val")) $("#s-min-val").textContent = s.min_score;
  setVal("#s-maxai", s.max_ai_per_run);
  setChk("#s-learn", s.learn_from_marks !== false);
  setVal("#s-loud", s.loud_score ?? 6);
  setVal("#s-model", s.model);
  setVal("#s-fallback", (s.fallback_models || []).join(", "));
  setVal("#s-batch", s.ai_batch ?? 4);
  setVal("#s-paid-min", s.paid_min_score ?? 6);
  setChk("#s-alive", s.check_alive !== false);
  setChk("#s-night", s.night?.enabled !== false); setVal("#s-night-from", s.night?.from ?? 1); setVal("#s-night-to", s.night?.to ?? 6);
  setChk("#s-digest", s.digest?.enabled !== false); setVal("#s-digest-hour", s.digest?.hour ?? 21);
  const ch = s.sources.olx_cheap || {};
  setChk("#s-cheap", ch.enabled); setVal("#s-cheap-max", ch.max_uah ?? 200); setVal("#s-cheap-pages", ch.pages ?? 3);
  setVal("#s-cheap-cats", (ch.categories || []).join("\n"));
  setVal("#s-home-city", s.home?.city || ""); setVal("#s-home-near", (s.home?.nearby || []).join("\n"));
  setChk("#s-home-only", s.home?.only_near);
  setChk("#s-olx", s.sources.olx.enabled); setVal("#s-olx-pages", s.sources.olx.pages);
  setVal("#w-skip", (s.skip_words || []).join("\n"));
  setVal("#w-keep", (s.keep_words || []).join("\n"));
  setVal("#w-hunt", (s.hunt_words || []).join("\n"));
  setVal("#w-url", (s.skip_url_parts || []).join("\n"));
  setVal("#c-text", s.criteria);
  setVal("#l-rpd", s.gemini_rpd ?? 500);
  setVal("#f-score", s.min_score); if ($("#f-score-val")) $("#f-score-val").textContent = s.min_score;
  renderBotState();
}
$("#s-min").oninput = (e) => ($("#s-min-val").textContent = e.target.value);

function renderBotState() {
  const p = $("#bot-state");
  p.textContent = settings.paused ? "⏸ на паузе" : "● работает · порог " + settings.min_score;
  p.className = "pill " + (settings.paused ? "warn" : "ok");
}

async function save(part) {
  let patch;
  if (part === "settings") {
    patch = {
      paused: $("#s-paused").checked,
      telegram_enabled: $("#s-telegram").checked,
      min_score: +$("#s-min").value,
      max_ai_per_run: Math.max(1, Math.min(400, +$("#s-maxai").value || 100)),
      learn_from_marks: $("#s-learn").checked,
      loud_score: Math.max(0, Math.min(11, +$("#s-loud").value || 6)),
      model: $("#s-model").value.trim() || DEFAULTS.model,
      fallback_models: $("#s-fallback").value.split(",").map((x) => x.trim()).filter(Boolean),
      ai_batch: Math.max(1, Math.min(6, +$("#s-batch").value || 4)),
      paid_min_score: Math.max(0, Math.min(11, +$("#s-paid-min").value || 6)),
      check_alive: $("#s-alive").checked,
      night: { enabled: $("#s-night").checked, from: Math.max(0, Math.min(23, +$("#s-night-from").value || 0)),
               to: Math.max(0, Math.min(23, +$("#s-night-to").value || 0)) },
      digest: { enabled: $("#s-digest").checked, hour: Math.max(0, Math.min(23, +$("#s-digest-hour").value || 21)) },
      home: { city: $("#s-home-city").value.trim(), nearby: lines($("#s-home-near").value), only_near: $("#s-home-only").checked },
      sources: {
        olx: { enabled: $("#s-olx").checked, pages: Math.max(1, Math.min(25, +$("#s-olx-pages").value || 25)) },
        olx_cheap: { enabled: $("#s-cheap").checked, max_uah: Math.max(1, +$("#s-cheap-max").value || 200),
                     pages: Math.max(1, Math.min(25, +$("#s-cheap-pages").value || 3)), categories: lines($("#s-cheap-cats").value) },
        bazar: { enabled: false, pages: 0 },
        alo: { enabled: false, queries: [] },
      },
    };
  } else if (part === "limits") {
    patch = { gemini_rpd: Math.max(1, +$("#l-rpd").value || 500) };
  } else if (part === "filters") {
    patch = { skip_words: lines(getVal("#w-skip")), keep_words: lines(getVal("#w-keep")), skip_url_parts: lines(getVal("#w-url")),
              hunt_words: lines(getVal("#w-hunt")) };
  } else {
    patch = { criteria: $("#c-text").value.trim() || DEFAULTS.criteria };
  }
  try {
    settings = deepMerge(settings, patch);
    await setDoc(doc(db, "config", "settings"), { ...settings, updated_at: new Date() });
    renderBotState(); renderFinds(); renderLimits();
    toast("Сохранено. Бот применит при следующем запуске.");
  } catch (e) { toast("Ошибка сохранения: " + e.message, true); }
}
document.querySelectorAll("[data-save]").forEach((b) => (b.onclick = () => save(b.dataset.save)));
$("#c-reset").onclick = () => { $("#c-text").value = DEFAULTS.criteria; toast("Текст по умолчанию вставлен — нажми «Сохранить»."); };

// ---------- word-filter tester (Python-like \b for Cyrillic) ----------
const W = "[\\p{L}\\p{N}_]";
function pyRegex(src) {
  const conv = src.replace(/\\b/g, `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`);
  try { return new RegExp(conv, "iu"); } catch { return new RegExp(src.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "iu"); }
}
function testTitle(title) {
  const skip = lines($("#w-skip").value).find((w) => pyRegex(w).test(title));
  const keep = lines($("#w-keep").value).find((w) => pyRegex(w).test(title));
  if (!title) return "";
  if (skip && !keep) return `<span class="bad">✖ мусор</span> — сработало «${esc(skip)}»`;
  if (skip && keep) return `<span class="good">✔ к ИИ</span> — «${esc(skip)}», но спасло исключение «${esc(keep)}»`;
  return `<span class="good">✔ к ИИ</span> — ни одно мусорное слово не сработало`;
}
$("#w-test").oninput = () => ($("#w-test-res").innerHTML = testTitle($("#w-test").value.trim()));

// ---------- finds ----------
function mergeFinds() {
  const byId = new Map();
  for (const f of [...hitsAll, ...latest]) byId.set(f.id, f);
  finds = [...byId.values()].sort((x, y) => ms(y.analyzed_at) - ms(x.analyzed_at));
  renderFinds();
}
function subscribeFinds() {
  unsubFinds?.();
  const q = query(collection(db, "finds"), orderBy("analyzed_at", "desc"), limit(+$("#f-limit").value));
  unsubFinds = onSnapshot(q, (snap) => {
    latest = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    mergeFinds();
  }, (e) => toast("Ошибка загрузки находок: " + e.message, true));
  // every real find (score at/above the threshold) is loaded separately, so it never drops out
  // of the "latest N" window when the bot analyzes lots of new ads
  unsubHits?.();
  const minHit = Math.max(1, Math.min(10, +settings.min_score || 6));
  unsubHits = onSnapshot(query(collection(db, "finds"), where("score", ">=", minHit)), (snap) => {
    hitsAll = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    mergeFinds();
  }, (e) => toast("Ошибка загрузки находок: " + e.message, true));
  // folder items (taken / gone / spam) — loaded separately as well
  unsubTaken?.();
  unsubTaken = onSnapshot(query(collection(db, "finds"), where("status", "in", Object.keys(FOLDERS))), (snap) => {
    taken = snap.docs.map((d) => ({ id: d.id, ...d.data() }))
      .sort((a, b) => ms(b.status_at || b.analyzed_at) - ms(a.status_at || a.analyzed_at));
    renderTaken(); renderFinds();
  }, (e) => toast("Ошибка загрузки папок: " + e.message, true));
}
$("#f-limit").onchange = subscribeFinds;
["#f-score", "#f-source", "#f-status", "#f-search", "#f-platform", "#f-paid", "#f-area"].forEach((s) => ($(s).oninput = () => {
  todayOnly = false; $("#f-score-val").textContent = $("#f-score").value; renderFinds();
}));

function fmtDate(ts) {
  const d = ts?.toDate ? ts.toDate() : ts ? new Date(ts) : null;
  return d ? d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
}
function scoreClass(s) { return s >= 7 ? "s-high" : s >= 4 ? "s-mid" : "s-low"; }

function priceLinks(query) {
  const q = encodeURIComponent(query || "").replace(/%20/g, "+");
  const a = (href, t) => `<a href="${href}" target="_blank" rel="noopener">${t}</a>`;
  return [a(`https://www.ebay.de/sch/i.html?_nkw=${q}&LH_Sold=1&LH_Complete=1`, "eBay.de продано"),
          a(`https://www.ebay.com/sch/i.html?_nkw=${q}&LH_Sold=1&LH_Complete=1`, "eBay.com продано"),
          a(`https://www.etsy.com/search?q=${q}`, "Etsy")].join(" · ");
}

function isNear(f) {
  const loc = (f.location || "").toLowerCase();
  return [settings.home?.city, ...(settings.home?.nearby || [])].some((p) => p && loc.includes(p.toLowerCase()));
}
function ships(f) { return /курьер|отправит|почт|доставк/i.test(f.pickup || "") || !!f.delivery; }

function renderFinds() {
  const minS = +$("#f-score").value, src = $("#f-source").value, st = $("#f-status").value, plat = $("#f-platform").value;
  const q = $("#f-search").value.trim().toLowerCase();
  const paid = $("#f-paid").value, area = $("#f-area").value;
  const list = finds.filter((f) => !(f.status in FOLDERS) &&
    (!paid || (paid === "paid" ? (f.price_eur || 0) > 0 : !(f.price_eur > 0))) &&
    (!area || (area === "near" ? isNear(f) : area === "reach" ? isNear(f) || ships(f) : true)) &&
    (f.score ?? 0) >= minS && (!todayOnly || f.analyzed_at?.toDate?.().toDateString() === new Date().toDateString()) && (!src || f.source === src) && (!plat || (plat === "etsy" ? f.etsy_ok === true : f.etsy_ok === false)) && (!st || (f.status || "new") === st) &&
    (!q || `${f.title} ${f.location} ${f.items} ${f.description || ""} ${f.query_en || ""}`.toLowerCase().includes(q)));

  const today = new Date().toDateString();
  const hits = finds.filter((f) => (f.score ?? 0) >= settings.min_score);
  const tile = (key, n, label) => `<div data-f="${key}" class="${activeTile() === key ? "on" : ""}" title="Показать"><b>${n}</b><span>${label}</span></div>`;
  $("#stats").innerHTML =
    tile("all", finds.length, "оценено (последние) — показать все") +
    tile("hits", hits.length, `находок ≥ ${settings.min_score}`) +
    tile("today", finds.filter((f) => f.analyzed_at?.toDate?.().toDateString() === today).length, "оценено сегодня") +
    tile("interesting", finds.filter((f) => f.status === "interesting").length, "отмечено «интересно»") +
    tile("taken", taken.filter((f) => f.status === "taken").length, "забрал → вкладка «Забранные»");

  $("#finds-empty").classList.toggle("hidden", list.length > 0);
  $("#finds").innerHTML = list.map(cardHTML).join("");
}

function cardHTML(f) {
  return `
    <article class="find ${f.status === "skip" || f.status === "low_profit" ? "dim" : ""} ${f.status in FOLDERS ? "folder-" + f.status : ""}">
      <a class="ph" href="${esc(f.url)}" target="_blank" rel="noopener">
        ${f.photos?.[0] ? `<img loading="lazy" referrerpolicy="no-referrer" src="${esc(f.photos[0])}" alt="">` : `<div class="nophoto">нет фото</div>`}
        <span class="score ${scoreClass(f.score)}">${f.score}/10</span>
        ${f.sent ? `<span class="tg" title="Отправлено в Telegram">✈</span>` : ""}
      </a>
      <div class="body">
        ${f.status in FOLDERS ? `<p class="folder-badge ${f.status}">${f.auto_gone && f.status === "gone" ? "🏃 Объявление снято с сайта (проверил бот)" : FOLDERS[f.status].badge}${f.status_at ? " · " + fmtDate(f.status_at) : ""}</p>` : ""}
        ${f.price_eur > 0 ? `<p class="paid">💶 Платное: <b>${esc(f.price_uah ?? f.price_eur)} грн</b></p>` : ""}
        ${f.hidden_price && !(f.price_eur > 0) ? `<p class="paid">⚠️ Не бесплатно: в описании цена <b>${esc(f.hidden_price)}</b></p>` : ""}
        ${f.lot?.length ? `<details class="lot"><summary>📦 Лот: ${f.lot.length} объявлений одного продавца</summary>${f.lot.map((m) =>
          `<a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.title)}</a>${m.price ? ` — ${esc(m.price)} грн` : ""}`).join("<br>")}</details>` : ""}
        ${f.hunt ? `<p class="hunt">🎯 Охота: «${esc(f.hunt)}»</p>` : ""}
        <a class="title" href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.title)}</a>
        ${f.items ? `<p>🔎 ${esc(f.items)}</p>` : ""}
        ${f.why ? `<p class="muted">💡 ${esc(f.why)}</p>` : ""}
        <p class="price">💰 ${f.price_etsy || f.price_ebay ? `Etsy <b>${esc(f.price_etsy || "—")}</b> · eBay <b>${esc(f.price_ebay || "—")}</b>` : esc(f.price || "—")}${f.price_conf && f.price_conf !== "высокая" ? ` <span class="conf ${f.price_conf === "низкая" ? "low" : ""}" title="Уверенность ИИ в цене">· ${esc(f.price_conf)} уверенность</span>` : ""}</p>
        ${"etsy_ok" in f ? `<p class="plat ${f.etsy_ok ? "etsy" : "ebay"}">🏷 ${f.etsy_ok ? "Etsy ✅ винтаж/handmade" : "только eBay"}${f.era ? ` · ${esc(f.era)}` : ""}</p>` : ""}
        <p class="checks">🔍 ${priceLinks(f.query_en || f.title)}${f.query_en ? ` <span class="muted small">«${esc(f.query_en)}»</span>` : ""}</p>
        <p class="meta">🚚 ${esc(f.pickup || "—")}</p>
        <p class="meta">📍 ${esc(f.location || "?")} · ${SOURCE_NAMES[f.source] || f.source}</p>
        <p class="meta">🕐 опубликовано ${f.posted_at ? ago(f.posted_at * 1000) : esc(f.posted || "?")} · оценено ${fmtDate(f.analyzed_at)}</p>
        <button class="btn small copy" data-copy="${esc(f.id)}">✉️ Скопировать текст продавцу</button>
        ${f.status === "taken" ? salesHTML(f) : ""}
        <div class="status">
          ${Object.entries(STATUS).map(([k, v]) => `<button data-id="${esc(f.id)}" data-st="${k}" class="${(f.status || "new") === k ? "on" : ""}">${v}</button>`).join("")}
        </div>
      </div>
    </article>`;
}

// ---------- sales tracking (on «Забранные» cards) ----------
const SOLD_WHERE = { "": "не продано", etsy: "Etsy", ebay: "eBay", local: "OLX / лично", other: "другое" };
function salesHTML(f) {
  const v = (x) => (x === undefined || x === null ? "" : esc(x));
  return `<div class="sales" data-sales="${esc(f.id)}">
    <label>Где продано <select name="sold_where">${Object.entries(SOLD_WHERE).map(([k, t]) => `<option value="${k}" ${(f.sold_where || "") === k ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label>Продано за, € <input name="sold_price" type="number" min="0" step="0.01" value="${v(f.sold_price)}"></label>
    <label>Расходы, € <input name="costs" type="number" min="0" step="0.01" value="${v(f.costs)}" title="доставка, комиссии, покупка"></label>
    <label>Часов ушло <input name="hours" type="number" min="0" step="0.25" value="${v(f.hours)}" title="забрать, почистить, сфотографировать, отправить"></label>
    <button class="btn small" data-save-sale="${esc(f.id)}">Сохранить</button>
  </div>`;
}
function renderSales() {
  const el = $("#sales-summary");
  if (!el) return;
  const mine = taken.filter((f) => f.status === "taken");
  const monthStart = new Date(); monthStart.setDate(1); monthStart.setHours(0, 0, 0, 0);
  const sum = (list) => {
    const sold = list.filter((f) => f.sold_where && +f.sold_price > 0);
    const rev = sold.reduce((a, f) => a + +f.sold_price, 0);
    const cost = list.reduce((a, f) => a + (+f.costs || 0) + (+f.price_eur || 0), 0);
    const hours = list.reduce((a, f) => a + (+f.hours || 0), 0);
    return { n: list.length, sold: sold.length, rev, cost, profit: rev - cost, hours, perHour: hours ? (rev - cost) / hours : null,
             perItem: sold.length ? (rev - cost) / sold.length : null };
  };
  const eur = (x) => x === null ? "—" : (Math.round(x * 100) / 100).toLocaleString("ru-RU") + " €";
  const row = (title, s) => `<tr><td>${title}</td><td>${s.n}</td><td>${s.sold}</td><td>${eur(s.rev)}</td><td>${eur(s.cost)}</td>
    <td><b>${eur(s.profit)}</b></td><td>${s.hours || "—"}</td><td><b>${eur(s.perHour)}</b></td><td>${eur(s.perItem)}</td></tr>`;
  const month = mine.filter((f) => ms(f.status_at || f.analyzed_at) >= +monthStart);
  const by = (key, names) => Object.entries(names).map(([k, t]) => [t, mine.filter((f) => key(f) === k)]).filter(([, l]) => l.length);
  const rows = [["Этот месяц", month], ["Всё время", mine],
    ...by((f) => f.sold_where || "", Object.fromEntries(Object.entries(SOLD_WHERE).filter(([k]) => k))).map(([t, l]) => ["продано: " + t, l]),
    ...by((f) => f.etsy_ok === true ? "etsy" : f.etsy_ok === false ? "ebay" : "", { etsy: "Etsy-винтаж", ebay: "только eBay" }).map(([t, l]) => ["тип: " + t, l]),
    ...by((f) => f.source || "", SOURCE_NAMES).map(([t, l]) => ["сайт: " + t, l])];
  el.innerHTML = mine.length ? `<table class="runs sales-table"><thead><tr><th></th><th>Забрано</th><th>Продано</th><th>Выручка</th>
    <th>Расходы</th><th>Прибыль</th><th>Часов</th><th>€ в час</th><th>€ на вещь</th></tr></thead>
    <tbody>${rows.map(([t, l]) => row(t, sum(l))).join("")}</tbody></table>
    <p class="muted small">Расходы = то, что вы вписали (доставка, комиссии) + цена покупки у платных объявлений (в € по курсу из настроек, по умолчанию 48 грн/€). «€ в час» считается по вещам, где указаны часы.</p>` : "";
}
$("#taken").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-save-sale]");
  if (!b) return;
  const box = b.closest("[data-sales]");
  const val = (n) => box.querySelector(`[name="${n}"]`).value;
  const num = (n) => (val(n) === "" ? null : Math.max(0, +val(n)));
  try {
    await updateDoc(doc(db, "finds", b.dataset.saveSale), { sold_where: val("sold_where"), sold_price: num("sold_price"),
      costs: num("costs"), hours: num("hours"), sale_at: new Date() });
    toast("Сохранено.");
  } catch (err) { toast("Не удалось сохранить: " + err.message, true); }
});

// ---------- route for «интересно» ----------
$("#btn-route").onclick = () => {
  const list = finds.filter((f) => f.status === "interesting" && f.location);
  if (!list.length) { toast("Нет находок со статусом «интересно» — отметь те, за которыми поедешь.", true); return; }
  const near = list.filter(isNear), use = near.length ? near : list;
  const groups = {};
  for (const f of use) { const k = f.location.split(",").map((x) => x.trim()).slice(-1)[0] || f.location; (groups[k] ||= []).push(f); }
  const stops = Object.keys(groups).slice(0, 10);
  const url = "https://www.google.com/maps/dir/" + stops.map((x) => encodeURIComponent(x)).join("/");
  window.open(url, "_blank", "noopener");
  toast(`Маршрут: ${stops.map((k) => `${k} (${groups[k].length})`).join(" → ")}` + (near.length && near.length < list.length ? ` · ${list.length - near.length} далёких не включены` : ""));
};

function renderTaken() {
  for (const [st, cfg] of Object.entries(FOLDERS)) {
    const list = taken.filter((f) => f.status === st);
    const btn = document.querySelector(`.tabs button[data-tab="${cfg.tab}"]`);
    if (btn) btn.textContent = `${cfg.title}${list.length ? ` (${list.length})` : ""}`;
    $(`#${cfg.tab}-empty`).classList.toggle("hidden", list.length > 0);
    $(`#${cfg.tab}`).innerHTML = list.map(cardHTML).join("");
  }
  renderSales();
}
// stat tiles act as quick filters
function activeTile() {
  const sc = +$("#f-score").value, st = $("#f-status").value;
  if ($("#f-source").value || $("#f-search").value.trim()) return "";
  if (todayOnly) return sc === 0 && !st ? "today" : "";
  if (sc === 0 && !st) return "all";
  if (sc === settings.min_score && !st) return "hits";
  if (sc === 0 && st === "interesting") return "interesting";
  return "";
}
$("#stats").addEventListener("click", (e) => {
  const t = e.target.closest("[data-f]");
  if (!t) return;
  const f = t.dataset.f;
  if (f === "taken") { document.querySelector('.tabs button[data-tab="taken"]').click(); return; }
  todayOnly = f === "today";
  $("#f-score").value = f === "hits" ? settings.min_score : 0;
  $("#f-score-val").textContent = $("#f-score").value;
  $("#f-status").value = f === "interesting" || f === "taken" ? f : "";
  $("#f-source").value = ""; $("#f-search").value = "";
  renderFinds();
});
async function onCardClick(e) {
  const c = e.target.closest("button[data-copy]");
  if (c) {
    const f = finds.find((x) => x.id === c.dataset.copy) || taken.find((x) => x.id === c.dataset.copy);
    const text = `Добрий день! Ще актуально „${(f?.title || "").slice(0, 80)}“? Можу забрати Новою поштою — доставку оплачу сам. Дякую!`;
    try { await navigator.clipboard.writeText(text); toast("Текст скопирован — вставь его в чат с продавцом."); }
    catch { prompt("Скопируй текст:", text); }
    return;
  }
  const b = e.target.closest("button[data-st]");
  if (!b) return;
  try {
    await updateDoc(doc(db, "finds", b.dataset.id), { status: b.dataset.st, status_at: new Date() });
    if (FOLDERS[b.dataset.st]) toast(`Перенесено во вкладку «${FOLDERS[b.dataset.st].title}».`);
    else if (b.closest(".folder-grid")) toast("Вернул в «Находки».");
  }
  catch (err) { toast("Не удалось сменить статус: " + err.message, true); }
}
$("#finds").addEventListener("click", onCardClick);
for (const cfg of Object.values(FOLDERS)) $(`#${cfg.tab}`).addEventListener("click", onCardClick);

// ---------- runs ----------
const STATUS_RUN = { running: "⏳ идёт", ok: "✔ ок", failed: "✖ ошибка" };
function subscribeRuns() {
  unsubRuns?.();
  const q = query(collection(db, "runs"), orderBy("started_at", "desc"), limit(40));
  unsubRuns = onSnapshot(q, (snap) => {
    $("#runs").innerHTML = snap.docs.map((d) => {
      const r = d.data();
      const dur = r.finished_at && r.started_at ? Math.round((r.finished_at.toMillis() - r.started_at.toMillis()) / 60000 * 10) / 10 + " мин" : "";
      const counts = r.counts ? Object.entries(r.counts).map(([k, v]) => `${SOURCE_NAMES[k] || k}: ${v}`).join("<br>") : "";
      return `<tr class="${r.status}">
        <td>${fmtDate(r.started_at)}</td><td>${r.trigger === "manual" ? "вручную" : r.trigger === "schedule" ? "расписание" : esc(r.trigger)}</td>
        <td>${STATUS_RUN[r.status] || esc(r.status)}</td><td>${counts}</td><td>${r.new ?? ""}</td><td>${r.junk ?? ""}${r.duplicates ? ` <span class="muted small">+${r.duplicates} дубл.</span>` : ""}</td>
        <td>${r.candidates ?? ""}</td><td>${r.analyzed ?? ""}</td><td><b>${r.sent ?? ""}</b></td><td>${r.backlog ?? ""}</td>
        <td>${dur}</td><td class="err small">${esc((r.errors || []).join("; "))}${r.failed_sources?.length ? " нет ответа: " + esc(r.failed_sources.join(", ")) : ""}</td></tr>`;
    }).join("") || `<tr><td colspan="12" class="muted">Запусков пока нет — данные появятся после первого запуска бота с Firestore.</td></tr>`;
    const last = snap.docs[0]?.data();
    lastRun = last || null; renderLive();
    if (last) $("#btn-run").title = "Последний запуск: " + fmtDate(last.started_at) + " — " + (STATUS_RUN[last.status] || last.status);
  }, (e) => toast("Ошибка загрузки запусков: " + e.message, true));
}

// ---------- run now (GitHub workflow_dispatch) ----------
// The token lives in Firestore (secrets/github), so every allowed dashboard user shares it.
// Firestore rules limit reads to the two allowed accounts.
const TOKEN_KEY = "gh_token"; // legacy per-browser storage, migrated on first load
let ghToken = "", ghMeta = null;
const tokenDoc = () => doc(db, "secrets", "github");
function getToken() { return ghToken; }
async function loadGhToken() {
  try {
    const snap = await getDoc(tokenDoc());
    if (snap.exists()) { ghToken = snap.data().token || ""; ghMeta = snap.data(); }
    let legacy = ""; try { legacy = localStorage.getItem(TOKEN_KEY) || ""; } catch {}
    if (!ghToken && legacy) await saveGhToken(legacy, true);
    if (legacy) try { localStorage.removeItem(TOKEN_KEY); } catch {}
  } catch (e) { toast("Не удалось загрузить токен GitHub: " + e.message, true); }
  renderGhState();
}
async function saveGhToken(v, quiet = false) {
  const meta = { token: v, saved_by: auth.currentUser?.email || "", saved_at: new Date() };
  await setDoc(tokenDoc(), meta);
  ghToken = v; ghMeta = meta;
  if (!quiet) toast("Токен сохранён — кнопка работает у обоих пользователей.");
}
function renderGhState() {
  const when = ghMeta?.saved_at ? fmtDate(ghMeta.saved_at) : "";
  $("#gh-state").textContent = ghToken ? `токен задан${ghMeta?.saved_by ? " (" + ghMeta.saved_by + (when ? ", " + when : "") + ")" : ""}` : "токен не задан";
}
$("#gh-save").onclick = async () => {
  const v = $("#gh-token").value.trim();
  if (!v) return;
  try { await saveGhToken(v); $("#gh-token").value = ""; renderGhState(); }
  catch (e) { toast("Не удалось сохранить токен: " + e.message, true); }
};
$("#gh-clear").onclick = async () => {
  try { await deleteDoc(tokenDoc()); ghToken = ""; ghMeta = null; renderGhState(); toast("Токен удалён."); }
  catch (e) { toast("Не удалось удалить токен: " + e.message, true); }
};

$("#btn-run").onclick = async () => {
  const token = getToken();
  if (!token) {
    document.querySelector('.tabs button[data-tab="runs"]').click();
    toast("Сначала вставь токен GitHub в разделе «Запуски».", true);
    return;
  }
  $("#btn-run").disabled = true;
  try {
    const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" },
      body: JSON.stringify({ ref: "main", inputs: { mode: "run" } }),
    });
    if (r.status === 401) toast("Токен GitHub недействителен или истёк — создай новый и вставь в «Запуски».", true);
    else if (r.status === 204) toast("Проверка запущена. Через ~1 минуту она появится в «Запусках», находки — по мере оценки.");
    else toast(`GitHub ответил ${r.status}: ${(await r.text()).slice(0, 200)}`, true);
  } catch (e) { toast("Не удалось запустить: " + e.message, true); }
  finally { setTimeout(() => ($("#btn-run").disabled = false), 5000); }
};

// ---------- limits ----------
const PT = "America/Los_Angeles";              // Gemini daily quotas reset at midnight Pacific time
const CRON_UTC_HOURS = [...Array(24).keys()], CRON_MINS = [13, 33, 53]; // every 20 min — must match .github/workflows/finder.yml
const LOCAL_TZ = "Europe/Kyiv";                    // night pause hours are local — must match LOCAL_TZ in main.py
function isNight(t) {
  const n = settings.night || {};
  if (n.enabled === false) return false;
  const h = +new Intl.DateTimeFormat("en-US", { timeZone: LOCAL_TZ, hour: "2-digit", hourCycle: "h23" }).format(t);
  const a = n.from ?? 1, b = n.to ?? 6;
  return a === b ? false : a < b ? h >= a && h < b : h >= a || h < b;
}
const ms = (ts) => ts?.toMillis ? ts.toMillis() : ts ? +new Date(ts) : 0;

function tzOffsetMs(date, tz) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric",
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .formatToParts(date).map((x) => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(date / 1000) * 1000;
}
function lastPacificMidnight(now) {
  const local = new Date(+now + tzOffsetMs(now, PT));          // wall clock in PT, expressed as UTC fields
  const midnightAsUtc = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate());
  return new Date(midnightAsUtc - tzOffsetMs(new Date(midnightAsUtc), PT));
}
function scheduledBetween(from, to) {
  if (settings.paused) return 0;
  let n = 0;
  const d = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate()));
  for (; +d < +to; d.setUTCDate(d.getUTCDate() + 1))
    for (const h of CRON_UTC_HOURS) for (const mi of CRON_MINS) {
      const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), h, mi);
      if (t > +from && t < +to && !isNight(new Date(t))) n++;
    }
  return n;
}
const fmtTime = (d) => d.toLocaleString("ru-RU", { weekday: "short", hour: "2-digit", minute: "2-digit" });
function bar(used, total) {
  const p = Math.min(100, Math.round(used / total * 100));
  return `<div class="lim-bar"><i class="${p > 90 ? "bad" : p > 70 ? "mid" : ""}" style="width:${p}%"></i></div>`;
}

function subscribeLimits() {
  unsubLimits?.();
  const now = new Date();
  const from = new Date(Math.min(+new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)), +lastPacificMidnight(now)));
  const q = query(collection(db, "runs"), where("started_at", ">=", from), orderBy("started_at", "desc"));
  unsubLimits = onSnapshot(q, (snap) => { limitRuns = snap.docs.map((d) => d.data()); renderLimits(); },
    (e) => toast("Ошибка загрузки лимитов: " + e.message, true));
  clearInterval(subscribeLimits._t);
  subscribeLimits._t = setInterval(renderLimits, 60000);
}

function renderLimits() {
  if (!$("#lim-gemini")) return;
  if (!$("#lim-strip").dataset.bound) {
    $("#lim-strip").dataset.bound = 1;
    $("#lim-strip").addEventListener("click", (e) => {
      if (!e.target.closest("[data-goto]")) return;
      e.preventDefault(); document.querySelector('.tabs button[data-tab="limits"]').click();
    });
  }
  const now = new Date();
  const maxAi = +settings.max_ai_per_run || 100;
  const last = limitRuns[0];

  // ---- Gemini ----
  const rpd = +settings.gemini_rpd || 500;
  const reset = lastPacificMidnight(now), nextReset = new Date(+reset + 86400000);
  const today = limitRuns.filter((r) => ms(r.started_at) >= +reset);
  // a running run reports its Gemini requests in progress.calls; old runs without it count as their done ads
  const used = today.reduce((a, r) => a + (r.status === "running" ? (r.progress?.calls ?? r.progress?.done ?? 0) : (r.ai_calls ?? r.analyzed ?? 0)), 0);
  // If Gemini itself said the daily quota is gone, trust that over our own count
  // (requests made outside the dashboard's log — older runs, AI Studio — also count).
  // only the latest finished run counts: a new API key (or a fresh quota) clears the warning
  const lastDone = today.find((r) => r.status !== "running");
  const exhausted = !!lastDone && (lastDone.errors || []).some((x) => /лимит Gemini/i.test(x));
  const left = exhausted ? 0 : Math.max(0, rpd - used);
  // The bot spreads what's left of the day's quota evenly over the remaining scheduled runs (every 20 min)
  // (a manual run takes the same share), so the evening still has quota for fresh ads.
  const sched = scheduledBetween(now, nextReset);
  const batch = +settings.ai_batch || 4;
  const share = exhausted ? 0 : Math.max(left ? 3 : 0, Math.floor(left / (sched + 1)));
  const shareAds = Math.min(maxAi, share * batch);
  $("#lim-gemini").innerHTML = `
    <div class="lim-big">${exhausted ? `исчерпан до ${fmtTime(nextReset)}` : `${used} / ${rpd}`}</div>${bar(exhausted ? rpd : used, rpd)}
    <div class="lim-answer">Следующая проверка (по расписанию или вручную) отдаст ИИ до <b>${shareAds}</b> объявлений
      <br><span class="small muted">${share} запрос(ов) × ${batch} объявления в запросе. Бот сам делит остаток лимита поровну на запуски до сброса, поэтому ручные проверки лимит не «съедают» — просто каждая следующая получает чуть меньше.</span></div>
    <div class="lim-rows">
      <span>Осталось запросов</span><span>${left}</span>
      <span>Сброс лимита</span><span>${fmtTime(nextReset)}</span>
      <span>Запусков по расписанию до сброса</span><span>${sched}${settings.paused ? " (пауза)" : ""}</span>
      <span>Объявлений в одном запросе</span><span>${batch}</span>
      <span>Запусков с начала суток</span><span>${today.length}</span>
      <span>В очереди после последней проверки</span><span>${last?.backlog ?? "—"}</span>
    </div>`;

  const pc = (a, b) => (a / b > 0.9 ? "bad" : a / b > 0.7 ? "mid" : "");
  const strip = $("#lim-strip");
  if (strip) strip.innerHTML = `
    <span class="${exhausted ? "bad" : pc(used, rpd)}">ИИ (Gemini): <b>${exhausted ? "лимит исчерпан" : `${used} / ${rpd}`}</b> за сутки, осталось ${left}, сброс ${nextReset.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}</span>
    <span>Следующая проверка: до <b>${shareAds}</b> объявлений к ИИ</span>
    <a href="#" data-goto="limits">подробнее</a>`;
}


// ---------- live status ----------
const SRC_FULL = { olx: "OLX.ua" };
function ago(ts) {
  const m = Math.round((Date.now() - ms(ts)) / 60000);
  return m < 1 ? "только что" : m < 60 ? `${m} мин назад` : `${Math.floor(m / 60)} ч ${m % 60} мин назад`;
}
function nextScheduled(now = new Date()) {
  if (settings.paused) return null;
  for (let d = 0; d < 3; d++)
    for (const h of CRON_UTC_HOURS) for (const mi of CRON_MINS) {
      const t = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + d, h, mi));
      if (t > now && !isNight(t)) return t;
    }
  return null;
}
const link = (url, text) => url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text);
function renderLive() {
  const el = $("#live");
  if (!el) return;
  const r = lastRun;
  const next = nextScheduled();
  const nextTxt = settings.paused ? "расписание на паузе" : next ? "следующая по расписанию: " + next.toLocaleString("ru-RU", { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
  if (!r) { el.className = "live"; el.innerHTML = `<span class="dot"></span> Проверок ещё не было · ${nextTxt}`; return; }

  if (r.status === "running" && ms(r.started_at) < Date.now() - 35 * 60000) {
    el.className = "live fail";
    el.innerHTML = `<span class="dot"></span> Последняя проверка прервана (GitHub остановил её через 30 мин) · начата ${ago(r.started_at)}${r.progress?.done ? ` · успела оценить ${r.progress.done} из ${r.progress.todo}` : ""} · ${nextTxt}`;
    return;
  }
  if (r.status === "running") {
    const p = r.progress || {};
    // with live progress: silent for 15 min = stuck; without it (older bot code): past the 30-min job timeout
    const stale = p.at ? ms(p.at) < Date.now() - 15 * 60000 : ms(r.started_at) < Date.now() - 35 * 60000;
    let what = p.phase ? "запуск…" : "подробности появятся со следующего запуска", bar = "";
    if (p.phase === "collect") {
      what = `собираю объявления: <b>${SRC_FULL[p.source] || esc(p.source || "")}</b>${p.step ? ` (сайт ${esc(p.step)})` : ""}` +
        (p.page ? ` · страница ${esc(p.page)}` : "") + (p.url ? ` · ${link(p.url, "открыть")}` : "") +
        (p.collected ? ` · собрано ${p.collected}` : "");
    } else if (p.phase === "filter") {
      what = `отбор: собрано ${p.collected}, новых ${p.new}, мусор ${p.junk}, дубли ${p.duplicates} → ИИ проверит <b>${p.todo}</b> из ${p.candidates}`;
    } else if (p.phase === "analyze") {
      const pct = p.todo ? Math.round(p.done / p.todo * 100) : 0;
      what = `ИИ оценивает <b>${p.done + 1} / ${p.todo}</b> · ${SRC_FULL[p.source] || esc(p.source || "")} · ${link(p.url, "«" + (p.title || "") + "»")} · находок: <b>${p.hits ?? 0}</b>`;
      bar = `<div class="lim-bar live-bar"><i style="width:${pct}%"></i></div>`;
    }
    el.className = "live " + (stale ? "stale" : "run");
    el.innerHTML = `<span class="dot"></span> ${stale ? "Проверка давно не отвечает (возможно, прервана)" : "Идёт проверка"}
      · начата ${ago(r.started_at)} · ${what}${bar}`;
    return;
  }
  const ok = r.status === "ok";
  el.className = "live " + (ok ? "" : "fail");
  el.innerHTML = `<span class="dot"></span> Последняя проверка ${ago(r.finished_at || r.started_at)} — ${ok ? "✔" : "✖ ошибка"}
    · собрано ${r.collected ?? "—"}, оценено ИИ ${r.analyzed ?? "—"}, находок ${r.sent ?? 0}${r.backlog ? `, в очереди ${r.backlog}` : ""} · ${nextTxt}`;
}
setInterval(renderLive, 30000);
