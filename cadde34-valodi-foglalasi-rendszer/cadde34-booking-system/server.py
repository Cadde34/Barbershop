#!/usr/bin/env python3
"""CADDE 34 booking server.

Dependency-free Python HTTP server with:
- SQLite-backed bookings
- real-time availability
- atomic overlap protection
- password-protected admin page
- optional Twilio SMS notification to the barbershop

Run locally:
    python server.py
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
DEFAULT_DB_PATH = BASE_DIR / "data" / "bookings.db"
TZ = ZoneInfo("Europe/Budapest")
ACTIVE_STATUSES = ("pending", "confirmed")
VALID_STATUSES = {"pending", "confirmed", "completed", "cancelled", "no-show"}
PHONE_RE = re.compile(r"^[+0-9 ()/-]{7,24}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
APPROVED_SERVICE_IDS = ("haircut", "style-cut", "beard", "hair-beard")

# The 30-minute values are the minimum booking blocks requested by the owner.
# They remain editable in the admin panel, so the exact real-world durations can
# be confirmed before launch without changing the code.
SERVICE_SEED = (
    ("haircut", "Hajvágás", "Haircut", 7000, 30),
    ("style-cut", "Stílusvágás", "Style Cut", 8000, 30),
    ("beard", "Szakáll", "Beard Trim", 4000, 30),
    ("hair-beard", "Haj + szakáll", "Haircut + Beard", 10000, 30),
)


def load_env_file(path: Path) -> None:
    """Load a simple KEY=VALUE .env file without third-party packages."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(ENV_PATH)


def parse_hhmm(value: str) -> dt_time:
    parsed = datetime.strptime(value, "%H:%M")
    return dt_time(parsed.hour, parsed.minute)


def load_config() -> dict:
    config = {
        "host": "0.0.0.0",
        "port": 8000,
        "admin_username": "admin",
        "admin_password": "CHANGE_ME_CADDE34",
        "opening_time": "09:00",
        "closing_time": "19:00",
        "opening_weekdays": [0, 1, 2, 3, 4, 5],  # Monday-Saturday
        "slot_minutes": 30,
        "minimum_notice_minutes": 0,
        "max_booking_days": 60,
        "business_phone": "+36705910745",
    }
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            config.update(loaded)

    env_map = {
        "CADDE34_HOST": ("host", str),
        "PORT": ("port", int),
        "CADDE34_ADMIN_USER": ("admin_username", str),
        "CADDE34_ADMIN_PASSWORD": ("admin_password", str),
        "CADDE34_OPENING_TIME": ("opening_time", str),
        "CADDE34_CLOSING_TIME": ("closing_time", str),
        "CADDE34_SLOT_MINUTES": ("slot_minutes", int),
        "CADDE34_MINIMUM_NOTICE_MINUTES": ("minimum_notice_minutes", int),
        "CADDE34_MAX_BOOKING_DAYS": ("max_booking_days", int),
        "CADDE34_BUSINESS_PHONE": ("business_phone", str),
    }
    for env_name, (key, caster) in env_map.items():
        value = os.getenv(env_name)
        if value is not None:
            config[key] = caster(value)

    config["port"] = int(config["port"])
    config["slot_minutes"] = int(config["slot_minutes"])
    config["minimum_notice_minutes"] = int(config["minimum_notice_minutes"])
    config["max_booking_days"] = int(config["max_booking_days"])
    config["opening_weekdays"] = [int(item) for item in config.get("opening_weekdays", [])]

    if config["slot_minutes"] < 30 or 60 % config["slot_minutes"] != 0:
        raise ValueError("slot_minutes must be 30 or 60")
    if config["minimum_notice_minutes"] < 0:
        raise ValueError("minimum_notice_minutes cannot be negative")
    if config["max_booking_days"] < 1:
        raise ValueError("max_booking_days must be positive")
    if not config["opening_weekdays"] or any(day < 0 or day > 6 for day in config["opening_weekdays"]):
        raise ValueError("opening_weekdays must contain values from 0 to 6")

    opening = parse_hhmm(str(config["opening_time"]))
    closing = parse_hhmm(str(config["closing_time"]))
    if (closing.hour, closing.minute) <= (opening.hour, opening.minute):
        raise ValueError("closing_time must be later than opening_time")
    return config


CONFIG = load_config()
DB_PATH = Path(os.getenv("CADDE34_DB_PATH", str(DEFAULT_DB_PATH))).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT: dict[str, list[float]] = {}


def open_time() -> dt_time:
    return parse_hhmm(str(CONFIG["opening_time"]))


def close_time() -> dt_time:
    return parse_hhmm(str(CONFIG["closing_time"]))


def opening_weekdays() -> set[int]:
    return set(int(day) for day in CONFIG["opening_weekdays"])


def notification_destination() -> str:
    return os.getenv("CADDE34_NOTIFICATION_TO", str(CONFIG["business_phone"])).strip()


def sms_is_configured() -> bool:
    return bool(
        os.getenv("TWILIO_ACCOUNT_SID")
        and os.getenv("TWILIO_AUTH_TOKEN")
        and (os.getenv("TWILIO_FROM_NUMBER") or os.getenv("TWILIO_MESSAGING_SERVICE_SID"))
    )


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_database() -> None:
    with db_connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
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
                start_epoch INTEGER NOT NULL,
                end_epoch INTEGER NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'hu' CHECK(language IN ('hu', 'en')),
                status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('pending', 'confirmed', 'completed', 'cancelled', 'no-show')),
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

            CREATE INDEX IF NOT EXISTS idx_bookings_active_range
            ON bookings(status, start_epoch, end_epoch);

            CREATE INDEX IF NOT EXISTS idx_bookings_date
            ON bookings(booking_date, start_time);
            """
        )

        # Upgrade databases created by an earlier version of this project.
        ensure_column(connection, "bookings", "notification_status", "TEXT NOT NULL DEFAULT 'not_configured'")
        ensure_column(connection, "bookings", "notification_provider", "TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "bookings", "notification_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "bookings", "notification_error", "TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "bookings", "notification_attempts", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(connection, "bookings", "notification_updated_at", "TEXT")

        for service_id, name_hu, name_en, price_huf, default_duration in SERVICE_SEED:
            connection.execute(
                """
                INSERT INTO services(id, name_hu, name_en, price_huf, duration_minutes, active)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    name_hu = excluded.name_hu,
                    name_en = excluded.name_en,
                    price_huf = excluded.price_huf,
                    active = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (service_id, name_hu, name_en, price_huf, default_duration),
            )

        # Do not expose any old sample service for new bookings. Historical
        # bookings retain their copied service name and price.
        placeholders = ",".join("?" for _ in APPROVED_SERVICE_IDS)
        connection.execute(
            f"UPDATE services SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id NOT IN ({placeholders})",
            APPROVED_SERVICE_IDS,
        )


def get_service(connection: sqlite3.Connection, service_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM services WHERE id = ? AND active = 1", (service_id,)
    ).fetchone()


def local_datetime(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, parse_hhmm(hhmm), TZ)


def epoch_seconds(value: datetime) -> int:
    return int(value.timestamp())


def format_price_hu(price: int) -> str:
    return f"{price:,}".replace(",", " ") + " Ft"


def format_price_en(price: int) -> str:
    return "HUF " + f"{price:,}"


def sanitize_text(value: object, max_length: int, required: bool = False) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split()) if "\n" not in text else text
    if required and not text:
        raise ValueError("missing_required")
    if len(text) > max_length:
        raise ValueError("too_long")
    return text


def validate_date(raw: str) -> date:
    if not DATE_RE.match(raw):
        raise ValueError("invalid_date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("invalid_date") from exc


def validate_time(raw: str) -> str:
    if not TIME_RE.match(raw):
        raise ValueError("invalid_time")
    parsed = parse_hhmm(raw)
    minute_step = int(CONFIG["slot_minutes"])
    opening = open_time()
    opening_minutes = opening.hour * 60 + opening.minute
    current_minutes = parsed.hour * 60 + parsed.minute
    if (current_minutes - opening_minutes) % minute_step != 0:
        raise ValueError("invalid_time_step")
    return raw


def date_is_bookable(day: date) -> tuple[bool, str | None]:
    today = datetime.now(TZ).date()
    if day < today:
        return False, "past_date"
    if day > today + timedelta(days=int(CONFIG["max_booking_days"])):
        return False, "too_far"
    if day.weekday() not in opening_weekdays():
        return False, "closed"
    return True, None


def get_conflicts(
    connection: sqlite3.Connection,
    start_epoch: int,
    end_epoch: int,
    *,
    exclude_booking_id: int | None = None,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    exclude_sql = ""
    params: list[object] = [*ACTIVE_STATUSES, end_epoch, start_epoch]
    if exclude_booking_id is not None:
        exclude_sql = " AND id <> ?"
        params.append(exclude_booking_id)
    return connection.execute(
        f"""
        SELECT id, reference, start_epoch, end_epoch
        FROM bookings
        WHERE status IN ({placeholders})
          AND start_epoch < ?
          AND end_epoch > ?
          {exclude_sql}
        LIMIT 1
        """,
        params,
    ).fetchall()


def list_availability(day: date, service: sqlite3.Row) -> list[dict]:
    is_bookable, _ = date_is_bookable(day)
    if not is_bookable:
        return []

    now = datetime.now(TZ)
    earliest = now + timedelta(minutes=int(CONFIG["minimum_notice_minutes"]))
    duration = int(service["duration_minutes"])
    step = int(CONFIG["slot_minutes"])
    first = datetime.combine(day, open_time(), TZ)
    closing = datetime.combine(day, close_time(), TZ)

    with db_connect() as connection:
        day_start = epoch_seconds(datetime.combine(day, dt_time.min, TZ))
        day_end = epoch_seconds(datetime.combine(day + timedelta(days=1), dt_time.min, TZ))
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = connection.execute(
            f"""
            SELECT start_epoch, end_epoch
            FROM bookings
            WHERE status IN ({placeholders})
              AND start_epoch < ?
              AND end_epoch > ?
            """,
            (*ACTIVE_STATUSES, day_end, day_start),
        ).fetchall()

    slots: list[dict] = []
    current = first
    while current + timedelta(minutes=duration) <= closing:
        end = current + timedelta(minutes=duration)
        start_epoch = epoch_seconds(current)
        end_epoch = epoch_seconds(end)
        reason = None
        available = True

        if current <= earliest:
            available = False
            reason = "past"
        else:
            for row in rows:
                if int(row["start_epoch"]) < end_epoch and int(row["end_epoch"]) > start_epoch:
                    available = False
                    reason = "booked"
                    break

        slots.append(
            {
                "time": current.strftime("%H:%M"),
                "end_time": end.strftime("%H:%M"),
                "available": available,
                "reason": reason,
            }
        )
        current += timedelta(minutes=step)
    return slots


def make_reference(day: date) -> str:
    return f"C34-{day.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"


def booking_message(booking: dict) -> str:
    lines = [
        "Új időpontfoglalás – CADDE 34",
        "",
        f"Foglalási azonosító: {booking['reference']}",
        f"Név: {booking['customer_name']}",
        f"Telefon: {booking['phone']}",
        f"Szolgáltatás: {booking['service_name_hu']}",
        f"Ár: {format_price_hu(int(booking['price_huf']))}",
        f"Dátum: {booking['booking_date']}",
        f"Időpont: {booking['start_time']}–{booking['end_time']}",
    ]
    if booking.get("message"):
        lines.append(f"Megjegyzés: {booking['message']}")
    return "\n".join(lines)


def build_whatsapp_url(booking: dict) -> str:
    destination = re.sub(r"\D", "", notification_destination())
    return f"https://wa.me/{destination}?text=" + quote(booking_message(booking))


def update_notification(
    booking_id: int,
    *,
    status: str,
    provider: str = "twilio",
    notification_id: str = "",
    error: str = "",
    increment_attempt: bool = False,
) -> None:
    with db_connect() as connection:
        connection.execute(
            """
            UPDATE bookings
            SET notification_status = ?,
                notification_provider = ?,
                notification_id = ?,
                notification_error = ?,
                notification_attempts = notification_attempts + ?,
                notification_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                provider,
                notification_id,
                error[:1000],
                1 if increment_attempt else 0,
                booking_id,
            ),
        )


def send_twilio_sms(body: str) -> tuple[str, str]:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    destination = notification_destination()

    form: dict[str, str] = {"To": destination, "Body": body}
    if messaging_service_sid:
        form["MessagingServiceSid"] = messaging_service_sid
    else:
        form["From"] = from_number

    encoded = urlencode(form).encode("utf-8")
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{quote(account_sid, safe='')}/Messages.json",
        data=encoded,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "CADDE34Booking/2.0",
        },
    )

    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            return str(payload.get("sid", "")), str(payload.get("status", "accepted"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Twilio HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Twilio connection error: {exc.reason}") from exc


def send_booking_notification(booking: dict) -> dict:
    booking_id = int(booking["id"])
    if not sms_is_configured():
        update_notification(
            booking_id,
            status="not_configured",
            provider="twilio",
            error="Twilio credentials are not configured.",
        )
        return {"status": "not_configured", "id": "", "error": ""}

    update_notification(
        booking_id,
        status="sending",
        provider="twilio",
        error="",
        increment_attempt=True,
    )
    try:
        notification_id, provider_status = send_twilio_sms(booking_message(booking))
        # A successful Twilio API response means the provider accepted the SMS;
        # it does not falsely claim handset delivery.
        update_notification(
            booking_id,
            status="sent",
            provider="twilio",
            notification_id=notification_id,
            error="",
        )
        return {
            "status": "sent",
            "id": notification_id,
            "provider_status": provider_status,
            "error": "",
        }
    except Exception as exc:  # network/provider failures must not erase the booking
        message = str(exc)[:1000]
        update_notification(
            booking_id,
            status="failed",
            provider="twilio",
            error=message,
        )
        return {"status": "failed", "id": "", "error": message}


def allowed_booking_request(client_ip: str) -> bool:
    now = time.time()
    window = 10 * 60
    max_requests = 8
    with RATE_LIMIT_LOCK:
        recent = [stamp for stamp in RATE_LIMIT.get(client_ip, []) if now - stamp < window]
        if len(recent) >= max_requests:
            RATE_LIMIT[client_ip] = recent
            return False
        recent.append(now)
        RATE_LIMIT[client_ip] = recent
    return True


def booking_to_dict(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["price_hu"] = format_price_hu(int(row["price_huf"]))
    result["price_en"] = format_price_en(int(row["price_huf"]))
    return result


class Cadde34Handler(BaseHTTPRequestHandler):
    server_version = "Cadde34Booking/2.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        super().end_headers()

    def send_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status: int, code: str, message: str) -> None:
        self.send_json({"ok": False, "error": code, "message": message}, status)

    def read_json(self) -> dict:
        length_raw = self.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except ValueError as exc:
            raise ValueError("invalid_content_length") from exc
        if length <= 0 or length > 32768:
            raise ValueError("invalid_body_size")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_json") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid_json")
        return payload

    def is_admin_authenticated(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        expected_user = str(CONFIG["admin_username"])
        expected_password = str(CONFIG["admin_password"])
        return hmac.compare_digest(username, expected_user) and hmac.compare_digest(
            password, expected_password
        )

    def require_admin(self) -> bool:
        if self.is_admin_authenticated():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="CADDE 34 admin", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = b'{"ok":false,"error":"unauthorized"}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def serve_static(self, request_path: str) -> None:
        if request_path in ("", "/"):
            relative = Path("index.html")
        elif request_path in ("/admin", "/admin/"):
            if not self.require_admin():
                return
            relative = Path("admin.html")
        else:
            relative = Path(request_path.lstrip("/"))

        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        mime, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        content_type = mime or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if target.suffix == ".html" else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "service": "cadde34-booking",
                    "timezone": "Europe/Budapest",
                    "database": "sqlite",
                    "sms_configured": sms_is_configured(),
                }
            )
            return

        if path == "/api/services":
            with db_connect() as connection:
                rows = connection.execute(
                    "SELECT id, name_hu, name_en, price_huf, duration_minutes FROM services WHERE active = 1 ORDER BY rowid"
                ).fetchall()
            self.send_json(
                {
                    "ok": True,
                    "services": [
                        {
                            **dict(row),
                            "price_hu": format_price_hu(int(row["price_huf"])),
                            "price_en": format_price_en(int(row["price_huf"])),
                        }
                        for row in rows
                    ],
                }
            )
            return

        if path == "/api/availability":
            raw_date = query.get("date", [""])[0]
            service_id = query.get("service", [""])[0]
            try:
                day = validate_date(raw_date)
            except ValueError:
                self.send_error_json(400, "invalid_date", "Érvénytelen dátum.")
                return
            with db_connect() as connection:
                service = get_service(connection, service_id)
            if service is None:
                self.send_error_json(400, "invalid_service", "Érvénytelen szolgáltatás.")
                return
            bookable, reason = date_is_bookable(day)
            if not bookable:
                self.send_json(
                    {
                        "ok": True,
                        "date": raw_date,
                        "closed": reason == "closed",
                        "reason": reason,
                        "duration_minutes": int(service["duration_minutes"]),
                        "slots": [],
                    }
                )
                return
            self.send_json(
                {
                    "ok": True,
                    "date": raw_date,
                    "closed": False,
                    "duration_minutes": int(service["duration_minutes"]),
                    "slot_minutes": int(CONFIG["slot_minutes"]),
                    "slots": list_availability(day, service),
                }
            )
            return

        if path == "/api/admin/bookings":
            if not self.require_admin():
                return
            start_date = query.get("from", [""])[0]
            end_date = query.get("to", [""])[0]
            clauses = []
            params: list[object] = []
            if start_date:
                try:
                    validate_date(start_date)
                except ValueError:
                    self.send_error_json(400, "invalid_from", "Érvénytelen kezdő dátum.")
                    return
                clauses.append("booking_date >= ?")
                params.append(start_date)
            if end_date:
                try:
                    validate_date(end_date)
                except ValueError:
                    self.send_error_json(400, "invalid_to", "Érvénytelen záró dátum.")
                    return
                clauses.append("booking_date <= ?")
                params.append(end_date)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            with db_connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM bookings" + where + " ORDER BY booking_date ASC, start_time ASC, id ASC",
                    params,
                ).fetchall()
            self.send_json(
                {
                    "ok": True,
                    "sms_configured": sms_is_configured(),
                    "bookings": [booking_to_dict(row) for row in rows],
                }
            )
            return

        if path == "/api/admin/services":
            if not self.require_admin():
                return
            with db_connect() as connection:
                rows = connection.execute(
                    "SELECT id, name_hu, name_en, price_huf, duration_minutes, active FROM services WHERE active = 1 ORDER BY rowid"
                ).fetchall()
            self.send_json({"ok": True, "services": [dict(row) for row in rows]})
            return

        self.serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/bookings":
            client_ip = self.client_address[0]
            if not allowed_booking_request(client_ip):
                self.send_error_json(429, "rate_limited", "Túl sok foglalási kísérlet. Próbálja újra később.")
                return
            try:
                payload = self.read_json()
                customer_name = sanitize_text(payload.get("name"), 120, required=True)
                phone = sanitize_text(payload.get("phone"), 24, required=True)
                service_id = sanitize_text(payload.get("service"), 40, required=True)
                raw_date = sanitize_text(payload.get("date"), 10, required=True)
                raw_time = sanitize_text(payload.get("time"), 5, required=True)
                message = sanitize_text(payload.get("message"), 800)
                language = sanitize_text(payload.get("language"), 2) or "hu"
                if language not in {"hu", "en"}:
                    language = "hu"
                if not PHONE_RE.match(phone):
                    raise ValueError("invalid_phone")
                day = validate_date(raw_date)
                validate_time(raw_time)
            except ValueError as exc:
                self.send_error_json(400, str(exc), "A megadott adatok hibásak vagy hiányosak.")
                return

            bookable, reason = date_is_bookable(day)
            if not bookable:
                self.send_error_json(400, reason or "not_bookable", "Ez a nap nem foglalható.")
                return

            connection: sqlite3.Connection | None = None
            try:
                connection = db_connect()
                connection.execute("BEGIN IMMEDIATE")
                service = get_service(connection, service_id)
                if service is None:
                    connection.execute("ROLLBACK")
                    self.send_error_json(400, "invalid_service", "Érvénytelen szolgáltatás.")
                    return

                start_local = local_datetime(day, raw_time)
                duration = int(service["duration_minutes"])
                end_local = start_local + timedelta(minutes=duration)
                opening_local = datetime.combine(day, open_time(), TZ)
                closing_local = datetime.combine(day, close_time(), TZ)
                earliest = datetime.now(TZ) + timedelta(minutes=int(CONFIG["minimum_notice_minutes"]))

                if start_local <= earliest:
                    connection.execute("ROLLBACK")
                    self.send_error_json(400, "past", "Ez az időpont már nem foglalható.")
                    return
                if start_local < opening_local or end_local > closing_local:
                    connection.execute("ROLLBACK")
                    self.send_error_json(400, "outside_hours", "Az időpont a nyitvatartási időn kívül esik.")
                    return

                start_epoch = epoch_seconds(start_local)
                end_epoch = epoch_seconds(end_local)
                if get_conflicts(connection, start_epoch, end_epoch):
                    connection.execute("ROLLBACK")
                    self.send_error_json(
                        409,
                        "slot_taken",
                        "Ez az időpont időközben foglalttá vált. Kérjük, válasszon másikat.",
                    )
                    return

                reference = make_reference(day)
                initial_notification_status = "pending" if sms_is_configured() else "not_configured"
                cursor = connection.execute(
                    """
                    INSERT INTO bookings(
                        reference, customer_name, phone, service_id,
                        service_name_hu, service_name_en, price_huf,
                        duration_minutes, booking_date, start_time, end_time,
                        start_epoch, end_epoch, message, language, status,
                        notification_status, notification_provider
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, 'twilio')
                    """,
                    (
                        reference,
                        customer_name,
                        phone,
                        service_id,
                        service["name_hu"],
                        service["name_en"],
                        int(service["price_huf"]),
                        duration,
                        raw_date,
                        raw_time,
                        end_local.strftime("%H:%M"),
                        start_epoch,
                        end_epoch,
                        message,
                        language,
                        initial_notification_status,
                    ),
                )
                booking_id = int(cursor.lastrowid)
                connection.execute("COMMIT")
                connection.close()
                connection = None

                booking = {
                    "id": booking_id,
                    "reference": reference,
                    "customer_name": customer_name,
                    "phone": phone,
                    "service_id": service_id,
                    "service_name_hu": service["name_hu"],
                    "service_name_en": service["name_en"],
                    "price_huf": int(service["price_huf"]),
                    "price_hu": format_price_hu(int(service["price_huf"])),
                    "price_en": format_price_en(int(service["price_huf"])),
                    "duration_minutes": duration,
                    "booking_date": raw_date,
                    "start_time": raw_time,
                    "end_time": end_local.strftime("%H:%M"),
                    "message": message,
                    "status": "confirmed",
                }
                notification = send_booking_notification(booking)
                booking["notification_status"] = notification["status"]
                booking["notification_id"] = notification.get("id", "")
                booking["whatsapp_url"] = build_whatsapp_url(booking)
                self.send_json({"ok": True, "booking": booking}, 201)
            except sqlite3.Error as exc:
                if connection is not None:
                    try:
                        connection.execute("ROLLBACK")
                    except Exception:
                        pass
                    connection.close()
                print("Database error:", exc)
                self.send_error_json(500, "database_error", "A foglalás mentése nem sikerült.")
            return

        status_match = re.fullmatch(r"/api/admin/bookings/(\d+)/status", path)
        if status_match:
            if not self.require_admin():
                return
            try:
                payload = self.read_json()
                status = sanitize_text(payload.get("status"), 20, required=True)
            except ValueError:
                self.send_error_json(400, "invalid_json", "Érvénytelen kérés.")
                return
            if status not in VALID_STATUSES:
                self.send_error_json(400, "invalid_status", "Érvénytelen állapot.")
                return
            booking_id = int(status_match.group(1))
            connection: sqlite3.Connection | None = None
            try:
                connection = db_connect()
                connection.execute("BEGIN IMMEDIATE")
                booking_row = connection.execute(
                    "SELECT id, status, start_epoch, end_epoch FROM bookings WHERE id = ?",
                    (booking_id,),
                ).fetchone()
                if booking_row is None:
                    connection.execute("ROLLBACK")
                    self.send_error_json(404, "not_found", "A foglalás nem található.")
                    return

                # Re-activating a cancelled/completed/no-show booking must never
                # create an overlap with an appointment that was booked later.
                if status in ACTIVE_STATUSES and booking_row["status"] not in ACTIVE_STATUSES:
                    conflicts = get_conflicts(
                        connection,
                        int(booking_row["start_epoch"]),
                        int(booking_row["end_epoch"]),
                        exclude_booking_id=booking_id,
                    )
                    if conflicts:
                        connection.execute("ROLLBACK")
                        self.send_error_json(
                            409,
                            "slot_taken",
                            "A foglalás nem állítható vissza, mert az időpontot közben más lefoglalta.",
                        )
                        return

                connection.execute(
                    "UPDATE bookings SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, booking_id),
                )
                connection.execute("COMMIT")
                connection.close()
                connection = None
            except sqlite3.Error as exc:
                if connection is not None:
                    try:
                        connection.execute("ROLLBACK")
                    except Exception:
                        pass
                    connection.close()
                print("Database error while updating booking status:", exc)
                self.send_error_json(500, "database_error", "A foglalás állapota nem módosítható.")
                return

            self.send_json({"ok": True, "id": booking_id, "status": status})
            return

        notify_match = re.fullmatch(r"/api/admin/bookings/(\d+)/notify", path)
        if notify_match:
            if not self.require_admin():
                return
            booking_id = int(notify_match.group(1))
            with db_connect() as connection:
                row = connection.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            if row is None:
                self.send_error_json(404, "not_found", "A foglalás nem található.")
                return
            booking = booking_to_dict(row)
            result = send_booking_notification(booking)
            status_code = 200 if result["status"] == "sent" else 503
            self.send_json(
                {
                    "ok": result["status"] == "sent",
                    "notification_status": result["status"],
                    "notification_id": result.get("id", ""),
                    "message": (
                        "Az SMS-t a szolgáltató elfogadta."
                        if result["status"] == "sent"
                        else "Az SMS-küldés nincs beállítva vagy sikertelen volt."
                    ),
                },
                status_code,
            )
            return

        service_match = re.fullmatch(r"/api/admin/services/([a-z0-9-]+)", path)
        if service_match:
            if not self.require_admin():
                return
            try:
                payload = self.read_json()
                duration = int(payload.get("duration_minutes"))
            except (ValueError, TypeError):
                self.send_error_json(400, "invalid_duration", "Érvénytelen időtartam.")
                return
            step = int(CONFIG["slot_minutes"])
            if duration < step or duration > 240 or duration % step != 0:
                self.send_error_json(
                    400,
                    "invalid_duration",
                    f"Az időtartam {step} perces lépésekben, {step} és 240 perc között adható meg.",
                )
                return
            service_id = service_match.group(1)
            if service_id not in APPROVED_SERVICE_IDS:
                self.send_error_json(400, "invalid_service", "Ez a szolgáltatás nem módosítható.")
                return
            with db_connect() as connection:
                cursor = connection.execute(
                    "UPDATE services SET duration_minutes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND active = 1",
                    (duration, service_id),
                )
            if cursor.rowcount == 0:
                self.send_error_json(404, "not_found", "A szolgáltatás nem található.")
                return
            self.send_json({"ok": True, "id": service_id, "duration_minutes": duration})
            return

        self.send_error_json(404, "not_found", "Az útvonal nem található.")


class Cadde34Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    init_database()
    host = str(CONFIG["host"])
    port = int(CONFIG["port"])
    if str(CONFIG["admin_password"]) == "CHANGE_ME_CADDE34":
        print("FIGYELEM: Az admin jelszó még az alapértelmezett. Internetes közzététel előtt módosítsd a .env fájlban.")
    if not sms_is_configured():
        print("INFO: A Twilio SMS még nincs beállítva. A foglalások ettől függetlenül mentésre kerülnek.")
    server = Cadde34Server((host, port), Cadde34Handler)
    print(f"CADDE 34 weboldal: http://localhost:{port}")
    print(f"Admin foglalási táblázat: http://localhost:{port}/admin")
    print(f"Adatbázis: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLeállítás...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
