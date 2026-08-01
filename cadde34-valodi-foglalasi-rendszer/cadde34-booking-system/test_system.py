#!/usr/bin/env python3
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
TEST_DIR = ROOT / "test-data"
DB_PATH = TEST_DIR / "integration.db"
shutil.rmtree(TEST_DIR, ignore_errors=True)
TEST_DIR.mkdir(parents=True, exist_ok=True)
os.environ["CADDE34_DB_PATH"] = str(DB_PATH)
os.environ["CADDE34_ADMIN_PASSWORD"] = "testpass"
os.environ["PORT"] = "0"
for name in (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "TWILIO_MESSAGING_SERVICE_SID",
):
    os.environ.pop(name, None)

spec = importlib.util.spec_from_file_location("cadde34_server", ROOT / "server.py")
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
module.init_database()
server = module.Cadde34Server(("127.0.0.1", 0), module.Cadde34Handler)
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
BASE = f"http://127.0.0.1:{port}"


def request_json(path: str, *, method: str = "GET", payload=None, admin=False):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if admin:
        token = base64.b64encode(b"admin:testpass").decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    req = Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def next_open_date():
    candidate = datetime.now(module.TZ).date() + timedelta(days=2)
    while candidate.weekday() not in module.opening_weekdays():
        candidate += timedelta(days=1)
    return candidate.isoformat()


try:
    status, health = request_json("/api/health")
    assert status == 200 and health["ok"] and health["database"] == "sqlite"
    assert health["sms_configured"] is False

    status, services = request_json("/api/services")
    assert status == 200
    assert [(s["id"], s["price_huf"]) for s in services["services"]] == [
        ("haircut", 7000),
        ("style-cut", 8000),
        ("beard", 4000),
        ("hair-beard", 10000),
    ]

    day = next_open_date()
    status, availability = request_json(f"/api/availability?date={day}&service=haircut")
    assert status == 200 and availability["ok"]
    slot = next(item["time"] for item in availability["slots"] if item["available"])

    payload = {
        "name": "Teszt Vendég",
        "phone": "+36 30 123 4567",
        "service": "haircut",
        "date": day,
        "time": slot,
        "message": "Integrációs teszt",
        "language": "hu",
    }
    results = []
    barrier = threading.Barrier(3)

    def submit():
        barrier.wait()
        results.append(request_json("/api/bookings", method="POST", payload=payload))

    t1 = threading.Thread(target=submit)
    t2 = threading.Thread(target=submit)
    t1.start(); t2.start(); barrier.wait(); t1.join(); t2.join()
    codes = sorted(result[0] for result in results)
    assert codes == [201, 409], codes
    created = next(body["booking"] for code, body in results if code == 201)
    assert created["notification_status"] == "not_configured"

    status, availability = request_json(f"/api/availability?date={day}&service=haircut")
    same_slot = next(item for item in availability["slots"] if item["time"] == slot)
    assert same_slot["available"] is False and same_slot["reason"] == "booked"

    status, admin_bookings = request_json(
        f"/api/admin/bookings?from={day}&to={day}", admin=True
    )
    assert status == 200 and len(admin_bookings["bookings"]) == 1
    booking_id = admin_bookings["bookings"][0]["id"]
    assert admin_bookings["bookings"][0]["notification_status"] == "not_configured"

    status, _ = request_json(
        f"/api/admin/bookings/{booking_id}/status",
        method="POST",
        payload={"status": "cancelled"},
        admin=True,
    )
    assert status == 200

    status, availability = request_json(f"/api/availability?date={day}&service=haircut")
    same_slot = next(item for item in availability["slots"] if item["time"] == slot)
    assert same_slot["available"] is True

    status, _ = request_json(
        "/api/admin/services/hair-beard",
        method="POST",
        payload={"duration_minutes": 60},
        admin=True,
    )
    assert status == 200

    status, availability = request_json(f"/api/availability?date={day}&service=hair-beard")
    long_slot = next(item["time"] for item in availability["slots"] if item["available"])
    long_payload = dict(payload, service="hair-beard", time=long_slot, name="Hosszú Teszt")
    status, long_created = request_json("/api/bookings", method="POST", payload=long_payload)
    assert status == 201
    assert long_created["booking"]["duration_minutes"] == 60

    hour, minute = map(int, long_slot.split(":"))
    overlapping_minutes = hour * 60 + minute + 30
    overlapping_time = f"{overlapping_minutes // 60:02d}:{overlapping_minutes % 60:02d}"
    overlap_payload = dict(payload, service="haircut", time=overlapping_time, name="Átfedés Teszt")
    status, overlap = request_json("/api/bookings", method="POST", payload=overlap_payload)
    assert status == 409 and overlap["error"] == "slot_taken"

    # Exercise the automatic SMS success path without contacting an external
    # provider. Live delivery still requires real Twilio credentials.
    os.environ["TWILIO_ACCOUNT_SID"] = "AC_TEST"
    os.environ["TWILIO_AUTH_TOKEN"] = "TOKEN_TEST"
    os.environ["TWILIO_FROM_NUMBER"] = "+15005550006"
    module.send_twilio_sms = lambda body: ("SM_TEST_NOTIFICATION", "queued")
    status, availability = request_json(f"/api/availability?date={day}&service=beard")
    sms_slot = next(
        item["time"] for item in availability["slots"]
        if item["available"] and item["time"] not in {long_slot, overlapping_time}
    )
    sms_payload = dict(payload, service="beard", time=sms_slot, name="SMS Teszt")
    status, sms_created = request_json("/api/bookings", method="POST", payload=sms_payload)
    assert status == 201
    assert sms_created["booking"]["notification_status"] == "sent"
    assert sms_created["booking"]["notification_id"] == "SM_TEST_NOTIFICATION"

    print("PASS: exact four services and prices")
    print("PASS: SQLite booking persistence")
    print("PASS: concurrent double-booking rejection (201 + 409)")
    print("PASS: duration-based overlap rejection")
    print("PASS: cancellation releases the slot")
    print("PASS: admin API and editable service durations")
    print("PASS: notification state is tracked when SMS is not configured")
    print("PASS: automatic SMS success path (mock provider)")
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    shutil.rmtree(TEST_DIR, ignore_errors=True)
