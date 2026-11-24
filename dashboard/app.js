import { initializeApp } from "https://www.gstatic.com/firebasejs/11.10.0/firebase-app.js";
import { getAuth, GoogleAuthProvider, onAuthStateChanged, signInWithPopup, signOut }
  from "https://www.gstatic.com/firebasejs/11.10.0/firebase-auth.js";
import { getFirestore, doc, getDoc, setDoc, updateDoc, deleteDoc, collection, query, orderBy, limit, onSnapshot, where }
  from "https://www.gstatic.com/firebasejs/11.10.0/firebase-firestore.js";
import { DEFAULTS } from "./defaults.js";

const REPO = "Peper0007/olx-ua-free-finder";
const WORKFLOW = "finder.yml";
const SOURCE_NAMES = { olx: "OLX.ua" };
const STATUS = { new: "новое", interesting: "интересно", taken: "забрал", gone: "уже забрали", spam: "спам", skip: "мимо" };
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
  setVal("#s-loud", s.loud_score ?? 8);
  setVal("#s-model", s.model);
  setChk("#s-olx", s.sources.olx.enabled); setVal("#s-olx-pages", s.sources.olx.pages);
  setVal("#w-skip", (s.skip_words || []).join("\n"));
  setVal("#w-keep", (s.keep_words || []).join("\n"));
  setVal("#w-url", (s.skip_url_parts || []).join("\n"));
  setVal("#c-text", s.criteria);
  setVal("#l-rpd", s.gemini_rpd ?? 500); setVal("#l-ghmin", s.gh_minutes ?? 2000);
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
      max_ai_per_run: Math.max(1, Math.min(200, +$("#s-maxai").value || 100)),
      learn_from_marks: $("#s-learn").checked,
      loud_score: Math.max(0, Math.min(11, +$("#s-loud").value || 8)),
      model: $("#s-model").value.trim() || DEFAULTS.model,
      sources: {
        olx: { enabled: $("#s-olx").checked, pages: Math.max(1, Math.min(25, +$("#s-olx-pages").value || 25)) },
        bazar: { enabled: false, pages: 0 },
        alo: { enabled: false, queries: [] },
      },
    };
  } else if (part === "limits") {
    patch = { gemini_rpd: Math.max(1, +$("#l-rpd").value || 500), gh_minutes: Math.max(1, +$("#l-ghmin").value || 2000) };
  } else if (part === "filters") {
    patch = { skip_words: lines(getVal("#w-skip")), keep_words: lines(getVal("#w-keep")), skip_url_parts: lines(getVal("#w-url")) };
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
["#f-score", "#f-source", "#f-status", "#f-search", "#f-platform"].forEach((s) => ($(s).oninput = () => {
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

function renderFinds() {
  const minS = +$("#f-score").value, src = $("#f-source").value, st = $("#f-status").value, plat = $("#f-platform").value;
  const q = $("#f-search").value.trim().toLowerCase();
  const list = finds.filter((f) => !(f.status in FOLDERS) &&
    (f.score ?? 0) >= minS && (!todayOnly || f.analyzed_at?.toDate?.().toDateString() === new Date().toDateString()) && (!src || f.source === src) && (!plat || (plat === "etsy" ? f.etsy_ok === true : f.etsy_ok === false)) && (!st || (f.status || "new") === st) &&
    (!q || `${f.title} ${f.location} ${f.items}`.toLowerCase().includes(q)));

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
    <article class="find ${f.status === "skip" ? "dim" : ""} ${f.status in FOLDERS ? "folder-" + f.status : ""}">
      <a class="ph" href="${esc(f.url)}" target="_blank" rel="noopener">
        ${f.photos?.[0] ? `<img loading="lazy" referrerpolicy="no-referrer" src="${esc(f.photos[0])}" alt="">` : `<div class="nophoto">нет фото</div>`}
        <span class="score ${scoreClass(f.score)}">${f.score}/10</span>
        ${f.sent ? `<span class="tg" title="Отправлено в Telegram">✈</span>` : ""}
      </a>
      <div class="body">
        ${f.status in FOLDERS ? `<p class="folder-badge ${f.status}">${FOLDERS[f.status].badge}${f.status_at ? " · " + fmtDate(f.status_at) : ""}</p>` : ""}
        <a class="title" href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.title)}</a>
        ${f.items ? `<p>🔎 ${esc(f.items)}</p>` : ""}
        ${f.why ? `<p class="muted">💡 ${esc(f.why)}</p>` : ""}
        <p class="price">💰 ${f.price_etsy || f.price_ebay ? `Etsy <b>${esc(f.price_etsy || "—")}</b> · eBay <b>${esc(f.price_ebay || "—")}</b>` : esc(f.price || "—")}</p>
        ${"etsy_ok" in f ? `<p class="plat ${f.etsy_ok ? "etsy" : "ebay"}">🏷 ${f.etsy_ok ? "Etsy ✅ винтаж/handmade" : "только eBay"}${f.era ? ` · ${esc(f.era)}` : ""}</p>` : ""}
        <p class="checks">🔍 ${priceLinks(f.query_en || f.title)}${f.query_en ? ` <span class="muted small">«${esc(f.query_en)}»</span>` : ""}</p>
        <p class="meta">🚚 ${esc(f.pickup || "—")}</p>
        <p class="meta">📍 ${esc(f.location || "?")} · ${SOURCE_NAMES[f.source] || f.source}</p>
        <p class="meta">🕐 опубликовано ${f.posted_at ? ago(f.posted_at * 1000) : esc(f.posted || "?")} · оценено ${fmtDate(f.analyzed_at)}</p>
        <button class="btn small copy" data-copy="${esc(f.id)}">✉️ Скопировать текст продавцу</button>
        <div class="status">
          ${Object.entries(STATUS).map(([k, v]) => `<button data-id="${esc(f.id)}" data-st="${k}" class="${(f.status || "new") === k ? "on" : ""}">${v}</button>`).join("")}
        </div>
      </div>
    </article>`;
}

function renderTaken() {
  for (const [st, cfg] of Object.entries(FOLDERS)) {
    const list = taken.filter((f) => f.status === st);
    const btn = document.querySelector(`.tabs button[data-tab="${cfg.tab}"]`);
    if (btn) btn.textContent = `${cfg.title}${list.length ? ` (${list.length})` : ""}`;
    $(`#${cfg.tab}-empty`).classList.toggle("hidden", list.length > 0);
    $(`#${cfg.tab}`).innerHTML = list.map(cardHTML).join("");
  }
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
const CRON_UTC_HOURS = [6, 10, 14, 18], CRON_MIN = 7; // must match .github/workflows/finder.yml
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
    for (const h of CRON_UTC_HOURS) {
      const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), h, CRON_MIN);
      if (t > +from && t < +to) n++;
    }
  return n;
}
const fmtTime = (d) => d.toLocaleString("ru-RU", { weekday: "short", hour: "2-digit", minute: "2-digit" });
const runMinutes = (r) => r.finished_at && r.started_at ? Math.max(1, Math.ceil((ms(r.finished_at) - ms(r.started_at)) / 60000)) : 0;
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
  const finished = limitRuns.filter((r) => r.finished_at);
  const last = limitRuns[0];

  // ---- Gemini ----
  const rpd = +settings.gemini_rpd || 500;
  const reset = lastPacificMidnight(now), nextReset = new Date(+reset + 86400000);
  const recent = finished.slice(0, 5);
  const avgCalls = recent.length ? Math.round(recent.reduce((a, r) => a + (r.ai_calls ?? r.analyzed ?? 0), 0) / recent.length) : maxAi;
  const backlogOn = (last?.backlog ?? 1) > 0;
  const perRun = Math.max(1, backlogOn ? maxAi : Math.min(maxAi, avgCalls || 1));
  const today = limitRuns.filter((r) => ms(r.started_at) >= +reset);
  const stale = (r) => r.status === "running" && ms(r.started_at) < Date.now() - 35 * 60000;
  const used = today.reduce((a, r) => a + (stale(r) ? (r.progress?.done ?? perRun) : r.status === "running" ? perRun : (r.ai_calls ?? r.analyzed ?? 0)), 0);
  // If Gemini itself said the daily quota is gone, trust that over our own count
  // (requests made outside the dashboard's log — older runs, AI Studio — also count).
  // only the latest finished run counts: a new API key (or a fresh quota) clears the warning
  const lastDone = today.find((r) => r.status !== "running");
  const exhausted = !!lastDone && (lastDone.errors || []).some((x) => /лимит Gemini/i.test(x));
  const left = exhausted ? 0 : Math.max(0, rpd - used);
  const sched = scheduledBetween(now, nextReset);
  const reserve = sched * perRun;
  const manual = Math.max(0, Math.floor((left - reserve) / perRun));
  const manualNoSched = Math.floor(left / perRun);
  $("#lim-gemini").innerHTML = `
    <div class="lim-big">${exhausted ? `исчерпан до ${fmtTime(nextReset)}` : `${used} / ${rpd}`}</div>${bar(exhausted ? rpd : used, rpd)}
    <div class="lim-answer">Можно запустить вручную ещё <b>${manual}</b> раз(а) до сброса
      ${manual === 0 && manualNoSched > 0 ? `<br><span class="small muted">или ${manualNoSched}, если не оставлять запас на запуски по расписанию — тогда они упрутся в лимит</span>` : ""}</div>
    <div class="lim-rows">
      <span>Осталось запросов</span><span>${left}</span>
      <span>Сброс лимита</span><span>${fmtTime(nextReset)}</span>
      <span>Запусков по расписанию до сброса</span><span>${sched}${settings.paused ? " (пауза)" : ""}</span>
      <span>Один запуск тратит ~</span><span>${perRun} ${backlogOn ? "(есть очередь — берёт максимум)" : "(очередь пуста)"}</span>
      <span>Зарезервировано под расписание</span><span>${reserve}</span>
      <span>Запусков с начала суток</span><span>${today.length}</span>
    </div>`;

  // ---- GitHub Actions minutes ----
  const ghLimit = +settings.gh_minutes || 2000;
  const monthStart = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
  const monthEnd = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
  const month = finished.filter((r) => ms(r.started_at) >= +monthStart);
  const usedMin = month.reduce((a, r) => a + runMinutes(r), 0);
  const durs = finished.slice(0, 5).map(runMinutes).filter(Boolean);
  const avgMin = durs.length ? Math.ceil(durs.reduce((a, b) => a + b, 0) / durs.length) : 10;
  const schedMonth = scheduledBetween(now, monthEnd);
  const projected = schedMonth * avgMin;
  const ghLeft = ghLimit - usedMin;
  const ghManual = Math.max(0, Math.floor((ghLeft - projected) / avgMin));
  const over = usedMin + projected - ghLimit;
  $("#lim-gh").innerHTML = `
    <div class="lim-big">${usedMin} / ${ghLimit} мин</div>${bar(usedMin, ghLimit)}
    <div class="lim-answer">Можно запустить вручную ещё <b>${ghManual}</b> раз(а) до конца месяца
      ${over > 0 ? `<br><span class="small err">При текущей длительности запусков расписание до конца месяца не уложится: перерасход ~${over} мин. Запуски станут короче, когда разберётся очередь; иначе уменьши «объявлений за запуск» или страницы OLX.</span>` : ""}</div>
    <div class="lim-rows">
      <span>Осталось минут</span><span>${Math.max(0, ghLeft)}</span>
      <span>Средний запуск (последние 5)</span><span>${avgMin} мин</span>
      <span>Запусков по расписанию до конца месяца</span><span>${schedMonth}</span>
      <span>Они займут ~</span><span>${projected} мин</span>
      <span>Сброс</span><span>${monthEnd.toLocaleDateString("ru-RU")}</span>
    </div>
    <p class="small muted">Не учтены запуски до подключения дашборда, деплои дашборда (~1 мин каждый) и тестовые режимы.</p>`;
  const pc = (a, b) => (a / b > 0.9 ? "bad" : a / b > 0.7 ? "mid" : "");
  const strip = $("#lim-strip");
  if (strip) strip.innerHTML = `
    <span class="${exhausted ? "bad" : pc(used, rpd)}">ИИ (Gemini): <b>${exhausted ? "лимит исчерпан" : `${used} / ${rpd}`}</b> за сутки, осталось ${left}, сброс ${nextReset.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}</span>
    <span class="${pc(usedMin, ghLimit)}">GitHub: <b>${usedMin} / ${ghLimit}</b> мин за месяц</span>
    <span>Ручных запусков можно: <b>${Math.min(manual, ghManual)}</b> сегодня</span>
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
    for (const h of CRON_UTC_HOURS) {
      const t = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + d, h, CRON_MIN));
      if (t > now) return t;
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
