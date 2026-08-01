const TIME_ZONE = "Europe/Budapest";
const OPENING_MINUTES = 9 * 60;
const CLOSING_MINUTES = 19 * 60;
const SLOT_MINUTES = 30;
const MAX_BOOKING_DAYS = 60;
const MINIMUM_NOTICE_MINUTES = 0;
const ACTIVE_STATUSES = new Set(["pending", "confirmed"]);
const VALID_STATUSES = new Set(["pending", "confirmed", "completed", "cancelled", "no-show"]);
const APPROVED_SERVICE_IDS = new Set(["haircut", "style-cut", "beard", "hair-beard"]);
const PHONE_RE = /^[+0-9 ()/\-]{7,24}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TIME_RE = /^\d{2}:\d{2}$/;

const SERVICE_SEED = [
  ["haircut", "Hajvágás", "Haircut", 7000, 30],
  ["style-cut", "Stílusvágás", "Style Cut", 8000, 30],
  ["beard", "Szakáll", "Beard Trim", 4000, 30],
  ["hair-beard", "Haj + szakáll", "Haircut + Beard", 10000, 30],
];

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS services (
  id TEXT PRIMARY KEY,
  name_hu TEXT NOT NULL,
  name_en TEXT NOT NULL,
  price_huf INTEGER NOT NULL CHECK(price_huf >= 0),
  duration_minutes INTEGER NOT NULL DEFAULT 30 CHECK(duration_minutes > 0),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reference TEXT NOT NULL UNIQUE,
  customer_name TEXT NOT NULL,
  phone TEXT NOT NULL,
  service_id TEXT NOT NULL,
  service_name_hu TEXT NOT NULL,
  service_name_en TEXT NOT NULL,
  price_huf INTEGER NOT NULL,
  duration_minutes INTEGER NOT NULL,
  booking_date TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  start_minutes INTEGER NOT NULL,
  end_minutes INTEGER NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT 'hu' CHECK(language IN ('hu','en')),
  status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('pending','confirmed','completed','cancelled','no-show')),
  notification_status TEXT NOT NULL DEFAULT 'not_configured',
  notification_provider TEXT NOT NULL DEFAULT '',
  notification_id TEXT NOT NULL DEFAULT '',
  notification_error TEXT NOT NULL DEFAULT '',
  notification_attempts INTEGER NOT NULL DEFAULT 0,
  notification_updated_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(service_id) REFERENCES services(id)
);
CREATE INDEX IF NOT EXISTS idx_bookings_active_range ON bookings(booking_date,status,start_minutes,end_minutes);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(booking_date,start_time);
CREATE TABLE IF NOT EXISTS booking_rate_limits (
  bucket_key TEXT PRIMARY KEY,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
`;

let schemaReadyPromise = null;

function securityHeaders(headers = new Headers()) {
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "SAMEORIGIN");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Permissions-Policy", "geolocation=(), microphone=(), camera=()");
  headers.set("Content-Security-Policy", "default-src 'self'; img-src 'self' https://media.base44.com data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src https://www.google.com https://maps.google.com; form-action 'self' https://wa.me https://calendar.google.com; base-uri 'self'; frame-ancestors 'self'");
  return headers;
}

function jsonResponse(payload, status = 200, extraHeaders = {}) {
  const headers = securityHeaders(new Headers(extraHeaders));
  headers.set("Content-Type", "application/json; charset=utf-8");
  headers.set("Cache-Control", "no-store");
  return new Response(JSON.stringify(payload), { status, headers });
}

function errorResponse(status, error, message) {
  return jsonResponse({ ok: false, error, message }, status);
}

async function serveAsset(request, env, pathname) {
  if (!env.ASSETS) return errorResponse(500, "assets_missing", "A statikus weboldal nincs összekapcsolva.");
  const url = new URL(request.url);
  url.pathname = pathname;
  url.search = "";
  const original = await env.ASSETS.fetch(new Request(url.toString(), request));
  const response = new Response(original.body, original);
  securityHeaders(response.headers);
  if (pathname.endsWith(".html")) response.headers.set("Cache-Control", "no-cache");
  return response;
}

async function ensureSchema(env) {
  if (!env.DB) throw new Error("D1 binding 'DB' is missing");
  if (!schemaReadyPromise) {
    schemaReadyPromise = (async () => {
      await env.DB.exec(SCHEMA_SQL);
      const statements = SERVICE_SEED.map(([id, nameHu, nameEn, price, duration]) =>
        env.DB.prepare(`
          INSERT INTO services(id,name_hu,name_en,price_huf,duration_minutes,active)
          VALUES(?,?,?,?,?,1)
          ON CONFLICT(id) DO UPDATE SET
            name_hu=excluded.name_hu,
            name_en=excluded.name_en,
            price_huf=excluded.price_huf,
            active=1,
            updated_at=CURRENT_TIMESTAMP
        `).bind(id, nameHu, nameEn, price, duration)
      );
      statements.push(env.DB.prepare("UPDATE services SET active=0, updated_at=CURRENT_TIMESTAMP WHERE id NOT IN ('haircut','style-cut','beard','hair-beard')"));
      await env.DB.batch(statements);
    })().catch((error) => {
      schemaReadyPromise = null;
      throw error;
    });
  }
  return schemaReadyPromise;
}

function formatPriceHu(price) {
  return `${new Intl.NumberFormat("hu-HU").format(Number(price))} Ft`;
}
function formatPriceEn(price) {
  return `HUF ${new Intl.NumberFormat("en-US").format(Number(price))}`;
}
function sanitizeText(value, maxLength, required = false, preserveNewlines = false) {
  let text = String(value ?? "").trim();
  if (!preserveNewlines) text = text.replace(/\s+/g, " ");
  if (required && !text) throw new Error("missing_required");
  if (text.length > maxLength) throw new Error("too_long");
  return text;
}
function parseDateIso(raw) {
  if (!DATE_RE.test(raw)) throw new Error("invalid_date");
  const [year, month, day] = raw.split("-").map(Number);
  const stamp = Date.UTC(year, month - 1, day);
  const check = new Date(stamp);
  if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) throw new Error("invalid_date");
  return { raw, ordinal: Math.floor(stamp / 86400000), weekday: check.getUTCDay() };
}
function parseTimeMinutes(raw) {
  if (!TIME_RE.test(raw)) throw new Error("invalid_time");
  const [hour, minute] = raw.split(":").map(Number);
  if (hour > 23 || minute > 59) throw new Error("invalid_time");
  const total = hour * 60 + minute;
  if ((total - OPENING_MINUTES) % SLOT_MINUTES !== 0) throw new Error("invalid_time_step");
  return total;
}
function formatMinutes(total) {
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}
function budapestNow() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date());
  const map = {};
  for (const part of parts) if (part.type !== "literal") map[part.type] = part.value;
  const isoDate = `${map.year}-${map.month}-${map.day}`;
  return { isoDate, ordinal: parseDateIso(isoDate).ordinal, minutes: Number(map.hour) * 60 + Number(map.minute) };
}
function dateBookability(dateInfo) {
  const now = budapestNow();
  if (dateInfo.ordinal < now.ordinal) return { bookable: false, reason: "past_date" };
  if (dateInfo.ordinal > now.ordinal + MAX_BOOKING_DAYS) return { bookable: false, reason: "too_far" };
  if (dateInfo.weekday === 0) return { bookable: false, reason: "closed" };
  return { bookable: true, reason: null };
}
function isSmsConfigured(env) {
  return Boolean(env.TWILIO_ACCOUNT_SID && env.TWILIO_AUTH_TOKEN && (env.TWILIO_FROM_NUMBER || env.TWILIO_MESSAGING_SERVICE_SID));
}
function destinationPhone(env) {
  return String(env.CADDE34_NOTIFICATION_TO || "+36705910745").trim();
}
function makeReference(rawDate) {
  const random = new Uint8Array(3);
  crypto.getRandomValues(random);
  const suffix = [...random].map((value) => value.toString(16).padStart(2, "0")).join("").toUpperCase();
  return `C34-${rawDate.replace(/-/g, "")}-${suffix}`;
}
function bookingMessage(booking) {
  const lines = [
    "Új időpontfoglalás – CADDE 34",
    "",
    `Foglalási azonosító: ${booking.reference}`,
    `Név: ${booking.customer_name}`,
    `Telefon: ${booking.phone}`,
    `Szolgáltatás: ${booking.service_name_hu}`,
    `Ár: ${formatPriceHu(booking.price_huf)}`,
    `Dátum: ${booking.booking_date}`,
    `Időpont: ${booking.start_time}–${booking.end_time}`,
  ];
  if (booking.message) lines.push(`Megjegyzés: ${booking.message}`);
  return lines.join("\n");
}
function whatsappUrl(env, booking) {
  return `https://wa.me/${destinationPhone(env).replace(/\D/g, "")}?text=${encodeURIComponent(bookingMessage(booking))}`;
}
async function updateNotification(env, bookingId, values = {}) {
  await env.DB.prepare(`
    UPDATE bookings SET
      notification_status=?, notification_provider='twilio', notification_id=?, notification_error=?,
      notification_attempts=notification_attempts+?, notification_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
    WHERE id=?
  `).bind(
    values.status || "not_configured",
    values.notificationId || "",
    String(values.error || "").slice(0, 1000),
    values.incrementAttempt ? 1 : 0,
    bookingId
  ).run();
}
async function sendTwilioSms(env, body) {
  const sid = String(env.TWILIO_ACCOUNT_SID || "").trim();
  const token = String(env.TWILIO_AUTH_TOKEN || "").trim();
  const params = new URLSearchParams({ To: destinationPhone(env), Body: body });
  if (env.TWILIO_MESSAGING_SERVICE_SID) params.set("MessagingServiceSid", String(env.TWILIO_MESSAGING_SERVICE_SID));
  else params.set("From", String(env.TWILIO_FROM_NUMBER || ""));
  const response = await fetch(`https://api.twilio.com/2010-04-01/Accounts/${encodeURIComponent(sid)}/Messages.json`, {
    method: "POST",
    headers: {
      Authorization: `Basic ${btoa(`${sid}:${token}`)}`,
      "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    },
    body: params.toString(),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || `Twilio HTTP ${response.status}`);
  return data;
}
async function sendBookingNotification(env, booking) {
  if (!isSmsConfigured(env)) {
    await updateNotification(env, booking.id, { status: "not_configured" });
    return { status: "not_configured" };
  }
  try {
    const data = await sendTwilioSms(env, bookingMessage(booking));
    await updateNotification(env, booking.id, { status: "sent", notificationId: data.sid || "", incrementAttempt: true });
    return { status: "sent", id: data.sid || "" };
  } catch (error) {
    await updateNotification(env, booking.id, { status: "failed", error: error.message, incrementAttempt: true });
    return { status: "failed", error: error.message };
  }
}

function secureEqual(a, b) {
  const x = String(a);
  const y = String(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i += 1) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}
function adminAuthResponse(message = "Admin bejelentkezés szükséges.") {
  return new Response(message, {
    status: 401,
    headers: securityHeaders(new Headers({
      "Content-Type": "text/plain; charset=utf-8",
      "WWW-Authenticate": 'Basic realm="CADDE 34 admin", charset="UTF-8"',
      "Cache-Control": "no-store",
    })),
  });
}
function adminRequired(request, env) {
  const expectedUser = String(env.ADMIN_USER || "admin");
  const expectedPassword = String(env.ADMIN_PASSWORD || "");
  if (!expectedPassword) return errorResponse(503, "admin_not_configured", "Az ADMIN_PASSWORD környezeti változó nincs beállítva.");
  const header = request.headers.get("Authorization") || "";
  if (!header.startsWith("Basic ")) return adminAuthResponse();
  let decoded;
  try { decoded = atob(header.slice(6)); } catch (_) { return adminAuthResponse(); }
  const separator = decoded.indexOf(":");
  if (separator < 0) return adminAuthResponse();
  const user = decoded.slice(0, separator);
  const password = decoded.slice(separator + 1);
  if (!secureEqual(user, expectedUser) || !secureEqual(password, expectedPassword)) return adminAuthResponse("Hibás felhasználónév vagy jelszó.");
  return null;
}
async function readJson(request) {
  const type = request.headers.get("Content-Type") || "";
  if (!type.toLowerCase().includes("application/json")) throw new Error("invalid_content_type");
  const text = await request.text();
  if (text.length > 20000) throw new Error("payload_too_large");
  return JSON.parse(text || "{}");
}
async function rateLimitBooking(request, env) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const minute = Math.floor(Date.now() / 60000);
  const bucket = `${ip}:${minute}`;
  await env.DB.prepare("DELETE FROM booking_rate_limits WHERE updated_at < datetime('now','-10 minutes')").run();
  await env.DB.prepare(`
    INSERT INTO booking_rate_limits(bucket_key,attempt_count,updated_at) VALUES(?,1,CURRENT_TIMESTAMP)
    ON CONFLICT(bucket_key) DO UPDATE SET attempt_count=attempt_count+1,updated_at=CURRENT_TIMESTAMP
  `).bind(bucket).run();
  const row = await env.DB.prepare("SELECT attempt_count FROM booking_rate_limits WHERE bucket_key=?").bind(bucket).first();
  return Number(row?.attempt_count || 0) <= 8;
}
function bookingToApi(row) {
  return {
    ...row,
    price_hu: formatPriceHu(row.price_huf),
    price_en: formatPriceEn(row.price_huf),
  };
}

async function handleHealth(env) {
  return jsonResponse({
    ok: true,
    service: "cadde34-booking",
    timezone: TIME_ZONE,
    database: "cloudflare-d1",
    sms_configured: isSmsConfigured(env),
    admin_configured: Boolean(env.ADMIN_PASSWORD),
  });
}
async function handleServices(env) {
  const result = await env.DB.prepare("SELECT id,name_hu,name_en,price_huf,duration_minutes FROM services WHERE active=1 ORDER BY rowid").all();
  return jsonResponse({ ok: true, services: (result.results || []).map((row) => ({ ...row, price_hu: formatPriceHu(row.price_huf), price_en: formatPriceEn(row.price_huf) })) });
}
async function handleAvailability(url, env) {
  const rawDate = url.searchParams.get("date") || "";
  const serviceId = url.searchParams.get("service") || "";
  if (!APPROVED_SERVICE_IDS.has(serviceId)) return errorResponse(400, "invalid_service", "Érvénytelen szolgáltatás.");
  let dateInfo;
  try { dateInfo = parseDateIso(rawDate); } catch (_) { return errorResponse(400, "invalid_date", "Érvénytelen dátum."); }
  const service = await env.DB.prepare("SELECT * FROM services WHERE id=? AND active=1").bind(serviceId).first();
  if (!service) return errorResponse(404, "service_not_found", "A szolgáltatás nem található.");
  const bookability = dateBookability(dateInfo);
  if (!bookability.bookable) {
    return jsonResponse({ ok: true, date: rawDate, service: serviceId, duration_minutes: Number(service.duration_minutes), closed: bookability.reason === "closed", reason: bookability.reason, slots: [] });
  }
  const bookings = await env.DB.prepare("SELECT start_minutes,end_minutes FROM bookings WHERE booking_date=? AND status IN ('pending','confirmed')").bind(rawDate).all();
  const active = bookings.results || [];
  const now = budapestNow();
  const duration = Number(service.duration_minutes);
  const slots = [];
  for (let start = OPENING_MINUTES; start + duration <= CLOSING_MINUTES; start += SLOT_MINUTES) {
    const end = start + duration;
    let available = true;
    let reason = null;
    if (rawDate === now.isoDate && start <= now.minutes + MINIMUM_NOTICE_MINUTES) {
      available = false;
      reason = "past";
    } else if (active.some((booking) => Number(booking.start_minutes) < end && Number(booking.end_minutes) > start)) {
      available = false;
      reason = "booked";
    }
    slots.push({ time: formatMinutes(start), end_time: formatMinutes(end), available, reason });
  }
  return jsonResponse({ ok: true, date: rawDate, service: serviceId, duration_minutes: duration, closed: false, reason: null, slots });
}
async function handleCreateBooking(request, env) {
  if (!(await rateLimitBooking(request, env))) return errorResponse(429, "rate_limited", "Túl sok foglalási kísérlet. Kérjük, várjon egy percet.");
  let payload;
  try { payload = await readJson(request); } catch (_) { return errorResponse(400, "invalid_json", "Érvénytelen kérés."); }
  let customerName, phone, serviceId, rawDate, rawTime, message, language;
  try {
    customerName = sanitizeText(payload.name, 120, true);
    phone = sanitizeText(payload.phone, 24, true);
    serviceId = sanitizeText(payload.service, 40, true);
    rawDate = sanitizeText(payload.date, 10, true);
    rawTime = sanitizeText(payload.time, 5, true);
    message = sanitizeText(payload.message, 1000, false, true);
    language = payload.language === "en" ? "en" : "hu";
  } catch (_) { return errorResponse(400, "invalid_fields", "Kérjük, ellenőrizze a megadott adatokat."); }
  if (!PHONE_RE.test(phone)) return errorResponse(400, "invalid_phone", "Érvénytelen telefonszám.");
  if (!APPROVED_SERVICE_IDS.has(serviceId)) return errorResponse(400, "invalid_service", "Érvénytelen szolgáltatás.");
  let dateInfo, startMinutes;
  try { dateInfo = parseDateIso(rawDate); startMinutes = parseTimeMinutes(rawTime); } catch (_) { return errorResponse(400, "invalid_datetime", "Érvénytelen dátum vagy időpont."); }
  const bookability = dateBookability(dateInfo);
  if (!bookability.bookable) return errorResponse(400, bookability.reason, "A kiválasztott nap nem foglalható.");
  const service = await env.DB.prepare("SELECT * FROM services WHERE id=? AND active=1").bind(serviceId).first();
  if (!service) return errorResponse(404, "service_not_found", "A szolgáltatás nem található.");
  const duration = Number(service.duration_minutes);
  const endMinutes = startMinutes + duration;
  const now = budapestNow();
  if (rawDate === now.isoDate && startMinutes <= now.minutes + MINIMUM_NOTICE_MINUTES) return errorResponse(400, "past", "Ez az időpont már nem foglalható.");
  if (startMinutes < OPENING_MINUTES || endMinutes > CLOSING_MINUTES) return errorResponse(400, "outside_hours", "Az időpont a nyitvatartási időn kívül esik.");
  const reference = makeReference(rawDate);
  const endTime = formatMinutes(endMinutes);
  const initialNotificationStatus = isSmsConfigured(env) ? "pending" : "not_configured";
  let result;
  try {
    result = await env.DB.prepare(`
      INSERT INTO bookings(reference,customer_name,phone,service_id,service_name_hu,service_name_en,price_huf,duration_minutes,booking_date,start_time,end_time,start_minutes,end_minutes,message,language,status,notification_status,notification_provider)
      SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'confirmed',?,'twilio'
      WHERE NOT EXISTS (
        SELECT 1 FROM bookings WHERE booking_date=? AND status IN ('pending','confirmed') AND start_minutes < ? AND end_minutes > ?
      )
    `).bind(
      reference, customerName, phone, serviceId, service.name_hu, service.name_en, Number(service.price_huf), duration,
      rawDate, rawTime, endTime, startMinutes, endMinutes, message, language, initialNotificationStatus,
      rawDate, endMinutes, startMinutes
    ).run();
  } catch (_) { return errorResponse(500, "database_error", "A foglalás mentése nem sikerült."); }
  const changes = Number(result?.meta?.changes ?? result?.changes ?? 0);
  if (changes !== 1) return errorResponse(409, "slot_taken", "Ez az időpont időközben foglalttá vált. Kérjük, válasszon másikat.");
  let bookingId = Number(result?.meta?.last_row_id || 0);
  if (!bookingId) {
    const found = await env.DB.prepare("SELECT id FROM bookings WHERE reference=?").bind(reference).first();
    bookingId = Number(found?.id || 0);
  }
  const booking = {
    id: bookingId,
    reference,
    customer_name: customerName,
    phone,
    service_id: serviceId,
    service_name_hu: service.name_hu,
    service_name_en: service.name_en,
    price_huf: Number(service.price_huf),
    price_hu: formatPriceHu(service.price_huf),
    price_en: formatPriceEn(service.price_huf),
    duration_minutes: duration,
    booking_date: rawDate,
    start_time: rawTime,
    end_time: endTime,
    message,
    status: "confirmed",
  };
  const notification = await sendBookingNotification(env, booking);
  booking.notification_status = notification.status;
  booking.notification_id = notification.id || "";
  booking.whatsapp_url = whatsappUrl(env, booking);
  return jsonResponse({ ok: true, booking }, 201);
}
async function handleAdminBookings(url, request, env) {
  const auth = adminRequired(request, env);
  if (auth) return auth;
  const from = url.searchParams.get("from") || "";
  const to = url.searchParams.get("to") || "";
  try { if (from) parseDateIso(from); if (to) parseDateIso(to); } catch (_) { return errorResponse(400, "invalid_date", "Érvénytelen dátumtartomány."); }
  const clauses = [];
  const values = [];
  if (from) { clauses.push("booking_date>=?"); values.push(from); }
  if (to) { clauses.push("booking_date<=?"); values.push(to); }
  const sql = `SELECT * FROM bookings${clauses.length ? ` WHERE ${clauses.join(" AND ")}` : ""} ORDER BY booking_date,start_time,id`;
  const statement = env.DB.prepare(sql);
  const result = values.length ? await statement.bind(...values).all() : await statement.all();
  return jsonResponse({ ok: true, sms_configured: isSmsConfigured(env), bookings: (result.results || []).map(bookingToApi) });
}
async function handleAdminServices(request, env) {
  const auth = adminRequired(request, env);
  if (auth) return auth;
  const result = await env.DB.prepare("SELECT id,name_hu,name_en,price_huf,duration_minutes,active FROM services WHERE active=1 ORDER BY rowid").all();
  return jsonResponse({ ok: true, services: result.results || [] });
}
async function handleAdminStatus(request, env, bookingId) {
  const auth = adminRequired(request, env);
  if (auth) return auth;
  let status;
  try { status = sanitizeText((await readJson(request)).status, 20, true); } catch (_) { return errorResponse(400, "invalid_json", "Érvénytelen kérés."); }
  if (!VALID_STATUSES.has(status)) return errorResponse(400, "invalid_status", "Érvénytelen állapot.");
  const booking = await env.DB.prepare("SELECT id,status,booking_date,start_minutes,end_minutes FROM bookings WHERE id=?").bind(bookingId).first();
  if (!booking) return errorResponse(404, "not_found", "A foglalás nem található.");
  if (ACTIVE_STATUSES.has(status) && !ACTIVE_STATUSES.has(booking.status)) {
    const result = await env.DB.prepare(`
      UPDATE bookings SET status=?,updated_at=CURRENT_TIMESTAMP
      WHERE id=? AND NOT EXISTS (
        SELECT 1 FROM bookings other WHERE other.id<>? AND other.booking_date=? AND other.status IN ('pending','confirmed') AND other.start_minutes < ? AND other.end_minutes > ?
      )
    `).bind(status, bookingId, bookingId, booking.booking_date, Number(booking.end_minutes), Number(booking.start_minutes)).run();
    if (Number(result?.meta?.changes ?? result?.changes ?? 0) !== 1) return errorResponse(409, "slot_taken", "Az időpontot közben más lefoglalta.");
  } else {
    await env.DB.prepare("UPDATE bookings SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?").bind(status, bookingId).run();
  }
  return jsonResponse({ ok: true, id: bookingId, status });
}
async function handleAdminNotify(request, env, bookingId) {
  const auth = adminRequired(request, env);
  if (auth) return auth;
  const row = await env.DB.prepare("SELECT * FROM bookings WHERE id=?").bind(bookingId).first();
  if (!row) return errorResponse(404, "not_found", "A foglalás nem található.");
  const result = await sendBookingNotification(env, bookingToApi(row));
  const ok = result.status === "sent";
  return jsonResponse({ ok, notification_status: result.status, notification_id: result.id || "", message: ok ? "Az SMS-t a szolgáltató elfogadta." : "Az SMS nincs beállítva vagy sikertelen volt." }, ok ? 200 : 503);
}
async function handleAdminServiceUpdate(request, env, serviceId) {
  const auth = adminRequired(request, env);
  if (auth) return auth;
  if (!APPROVED_SERVICE_IDS.has(serviceId)) return errorResponse(400, "invalid_service", "Ez a szolgáltatás nem módosítható.");
  let duration;
  try { duration = Number((await readJson(request)).duration_minutes); } catch (_) { return errorResponse(400, "invalid_duration", "Érvénytelen időtartam."); }
  if (!Number.isInteger(duration) || duration < 30 || duration > 240 || duration % 30 !== 0) return errorResponse(400, "invalid_duration", "Az időtartam 30 és 240 perc között, 30 perces lépésekben adható meg.");
  const result = await env.DB.prepare("UPDATE services SET duration_minutes=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1").bind(duration, serviceId).run();
  if (Number(result?.meta?.changes ?? result?.changes ?? 0) !== 1) return errorResponse(404, "not_found", "A szolgáltatás nem található.");
  return jsonResponse({ ok: true, id: serviceId, duration_minutes: duration });
}

async function handleApi(request, env, url) {
  try { await ensureSchema(env); } catch (_) { return errorResponse(500, "database_not_configured", "A Cloudflare D1 adatbázis nincs DB néven összekapcsolva a projekttel."); }
  const method = request.method.toUpperCase();
  const path = url.pathname;
  if (method === "GET" && path === "/api/health") return handleHealth(env);
  if (method === "GET" && path === "/api/services") return handleServices(env);
  if (method === "GET" && path === "/api/availability") return handleAvailability(url, env);
  if (method === "POST" && path === "/api/bookings") return handleCreateBooking(request, env);
  if (method === "GET" && path === "/api/admin/bookings") return handleAdminBookings(url, request, env);
  if (method === "GET" && path === "/api/admin/services") return handleAdminServices(request, env);
  let match = path.match(/^\/api\/admin\/bookings\/(\d+)\/status$/);
  if (method === "POST" && match) return handleAdminStatus(request, env, Number(match[1]));
  match = path.match(/^\/api\/admin\/bookings\/(\d+)\/notify$/);
  if (method === "POST" && match) return handleAdminNotify(request, env, Number(match[1]));
  match = path.match(/^\/api\/admin\/services\/([a-z0-9-]+)$/);
  if (method === "POST" && match) return handleAdminServiceUpdate(request, env, match[1]);
  return errorResponse(404, "not_found", "Az útvonal nem található.");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: securityHeaders(new Headers({ Allow: "GET, POST, OPTIONS" })) });
    if (url.pathname.startsWith("/api/")) return handleApi(request, env, url);
    if (url.pathname === "/" || url.pathname === "/index.html") return serveAsset(request, env, "/index.html");
    if (url.pathname === "/admin" || url.pathname === "/admin/" || url.pathname === "/admin.html") {
      const auth = adminRequired(request, env);
      if (auth) return auth;
      return serveAsset(request, env, "/admin.html");
    }
    if (url.pathname === "/favicon.ico") return new Response(null, { status: 204, headers: securityHeaders() });
    return errorResponse(404, "not_found", "Az oldal nem található.");
  },
};
