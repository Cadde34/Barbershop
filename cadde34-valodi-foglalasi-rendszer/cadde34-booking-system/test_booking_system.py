#!/usr/bin/env python3
"""End-to-end smoke test for the CADDE 34 booking server."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_json(url: str, method: str = "GET", payload: dict | None = None, auth: tuple[str, str] | None = None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def next_open_day() -> str:
    candidate = date.today() + timedelta(days=2)
    while candidate.weekday() == 6:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def wait_until_ready(base_url: str) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            status, payload = request_json(f"{base_url}/api/health")
            if status == 200 and payload.get("ok"):
                return
        except (URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.15)
    raise RuntimeError("A tesztszerver nem indult el időben.")


def main() -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="cadde34-test-"))
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(port),
            "CADDE34_HOST": "127.0.0.1",
            "CADDE34_DB_PATH": str(temp_dir / "bookings.db"),
            "CADDE34_ADMIN_PASSWORD": "test-password",
        }
    )

    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        wait_until_ready(base_url)
        booking_day = next_open_day()

        service_status, service_data = request_json(f"{base_url}/api/services")
        assert service_status == 200 and service_data["ok"]
        assert [item["id"] for item in service_data["services"]] == [
            "haircut", "style-cut", "beard", "hair-beard"
        ]
        assert [item["price_huf"] for item in service_data["services"]] == [7000, 8000, 4000, 10000]

        status, availability = request_json(
            f"{base_url}/api/availability?date={booking_day}&service=haircut"
        )
        assert status == 200 and availability["ok"]
        slot = next(item for item in availability["slots"] if item["available"])

        payload = {
            "name": "Automatikus Teszt",
            "phone": "+36 70 123 4567",
            "service": "haircut",
            "date": booking_day,
            "time": slot["time"],
            "message": "teszt",
            "language": "hu",
        }

        first_status, first = request_json(f"{base_url}/api/bookings", "POST", payload)
        assert first_status == 201 and first["ok"], (first_status, first)
        assert first["booking"]["notification_status"] == "not_configured"

        second_status, second = request_json(f"{base_url}/api/bookings", "POST", payload)
        assert second_status == 409 and second["error"] == "slot_taken", (second_status, second)

        status, after = request_json(
            f"{base_url}/api/availability?date={booking_day}&service=haircut"
        )
        assert status == 200 and after["ok"]
        booked_slot = next(item for item in after["slots"] if item["time"] == slot["time"])
        assert booked_slot["available"] is False and booked_slot["reason"] == "booked"

        admin_status, admin = request_json(
            f"{base_url}/api/admin/bookings?from={booking_day}&to={booking_day}",
            auth=("admin", "test-password"),
        )
        assert admin_status == 200 and len(admin["bookings"]) == 1

        duration_status, duration_result = request_json(
            f"{base_url}/api/admin/services/hair-beard",
            "POST",
            {"duration_minutes": 60},
            auth=("admin", "test-password"),
        )
        assert duration_status == 200 and duration_result["duration_minutes"] == 60

        _, long_availability = request_json(
            f"{base_url}/api/availability?date={booking_day}&service=hair-beard"
        )
        long_slot = next(item for item in long_availability["slots"] if item["available"] and item["time"] != slot["time"])
        long_payload = {
            "name": "Hatvan Perces Teszt",
            "phone": "+36 70 765 4321",
            "service": "hair-beard",
            "date": booking_day,
            "time": long_slot["time"],
            "message": "",
            "language": "hu",
        }
        long_status, long_result = request_json(f"{base_url}/api/bookings", "POST", long_payload)
        assert long_status == 201 and long_result["booking"]["duration_minutes"] == 60

        hour, minute = map(int, long_slot["time"].split(":"))
        overlap_total = hour * 60 + minute + 30
        overlap_time = f"{overlap_total // 60:02d}:{overlap_total % 60:02d}"
        overlap_payload = {**payload, "name": "Átfedő Teszt", "time": overlap_time}
        overlap_status, overlap_result = request_json(f"{base_url}/api/bookings", "POST", overlap_payload)
        assert overlap_status == 409 and overlap_result["error"] == "slot_taken"

        cancel_status, _ = request_json(
            f"{base_url}/api/admin/bookings/{first['booking']['id']}/status",
            "POST",
            {"status": "cancelled"},
            auth=("admin", "test-password"),
        )
        assert cancel_status == 200
        _, after_cancel = request_json(
            f"{base_url}/api/availability?date={booking_day}&service=haircut"
        )
        released_slot = next(item for item in after_cancel["slots"] if item["time"] == slot["time"])
        assert released_slot["available"] is True

        replacement_payload = {**payload, "name": "Új Vendég", "phone": "+36 70 111 2233"}
        replacement_status, replacement = request_json(
            f"{base_url}/api/bookings", "POST", replacement_payload
        )
        assert replacement_status == 201 and replacement["ok"]

        restore_status, restore_result = request_json(
            f"{base_url}/api/admin/bookings/{first['booking']['id']}/status",
            "POST",
            {"status": "confirmed"},
            auth=("admin", "test-password"),
        )
        assert restore_status == 409 and restore_result["error"] == "slot_taken"

        _, concurrency_availability = request_json(
            f"{base_url}/api/availability?date={booking_day}&service=haircut"
        )
        concurrency_slot = next(
            item for item in concurrency_availability["slots"]
            if item["available"] and item["time"] not in {slot["time"], long_slot["time"], overlap_time}
        )
        concurrent_payload_a = {
            **payload,
            "name": "Párhuzamos A",
            "phone": "+36 70 444 5501",
            "time": concurrency_slot["time"],
        }
        concurrent_payload_b = {
            **payload,
            "name": "Párhuzamos B",
            "phone": "+36 70 444 5502",
            "time": concurrency_slot["time"],
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda item: request_json(f"{base_url}/api/bookings", "POST", item),
                [concurrent_payload_a, concurrent_payload_b],
            ))
        statuses = sorted(result[0] for result in results)
        assert statuses == [201, 409], results

        print("PASS: csak a négy hivatalos szolgáltatás foglalható;")
        print("      az azonos, átfedő és párhuzamos foglalások közül csak az első sikerült;")
        print("      a 60 perces időtartam teljesen blokkolt, a lemondás felszabadított,")
        print("      a később újrafoglalt időpont régi foglalása pedig nem volt visszaállítható.")
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
