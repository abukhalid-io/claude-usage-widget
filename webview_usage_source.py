"""
Webview Usage Source untuk Claude Usage Widget
================================================
Dijalankan sebagai subprocess terpisah oleh widget. Buka jendela
`pywebview` (native WebView2 di Windows -- BUKAN Playwright/automation)
ke https://claude.ai/settings/usage, biarkan user login manual SEKALI,
lalu baca "Plan usage limits" (nama plan, kuota sesi 5 jam & mingguan 7
hari) secara berkala dan tulis ke `usage_data.json`.

Kenapa pywebview, bukan Playwright:
    Playwright (2 percobaan sebelumnya) selalu kena tantangan Cloudflare
    "Just a moment..." karena Chromium yang dikendalikan lewat protokol
    otomasi (CDP) punya penanda yang gampang dideteksi (navigator.webdriver,
    dll). pywebview pakai WebView2 (engine Edge asli) tanpa protokol
    otomasi itu sama sekali -- sudah dites langsung ke claude.ai dan
    HALAMAN LOGIN ASLI muncul normal, tanpa tantangan bot apapun.

CATATAN soal "Continue with Google":
    Google SENGAJA memblokir login OAuth dari embedded webview apapun
    (kebijakan "disallowed_useragent" anti-phishing mereka) -- ini bukan
    hal yang bisa/boleh diakali. Kalau akun Claude kamu cuma bisa login
    lewat Google, coba "Continue with email" (kode verifikasi/OTP lewat
    email biasanya tetap tersedia sebagai opsi meski akun awalnya dibuat
    via Google).

Sesi login disimpan permanen di folder `webview_profile/` (lewat
`private_mode=False, storage_path=...` pywebview) -- run berikutnya
nggak perlu login ulang, jendelanya langsung disembunyikan dari awal.

Jalankan manual (opsional, buat testing):
    python webview_usage_source.py
"""

import json
import msvcrt
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import webview

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "webview_profile"
LOGGED_IN_FLAG = PROFILE_DIR / ".logged_in"
DATA_FILE = BASE_DIR / "usage_data.json"
LOCK_FILE = BASE_DIR / "webview_usage_source.lock"
DATA_FILE_TMP = BASE_DIR / "usage_data.json.tmp"

USAGE_URL = "https://claude.ai/settings/usage"
REFRESH_INTERVAL_SECONDS = 180          # kuota nggak berubah tiap detik, 3 menit cukup
LOGIN_POLL_INTERVAL_SECONDS = 3
LOGIN_TIMEOUT_SECONDS = 900              # 15 menit buat login manual pertama kali

window = None  # diisi setelah create_window


def parse_usage_text(body_text: str) -> dict:
    """Parse `document.body.innerText` halaman "Plan usage limits" jadi:
        {
            "plan_name": str,
            "session": {"pct_used": int, "reset_text": str},
            "weekly": [{"name": str, "pct_used": int, "reset_text": str}, ...],
        }
    """
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]

    plan_name = ""
    section = None
    pending_name = None
    pending_reset = None
    session = {}
    weekly = []

    for i, line in enumerate(lines):
        if line.startswith("Plan usage limits"):
            remainder = line[len("Plan usage limits"):].strip()
            if not remainder and i + 1 < len(lines):
                remainder = lines[i + 1]
            plan_name = remainder
            continue
        if line.startswith("Current session"):
            section = "session"
            pending_name = None
            pending_reset = None
            continue
        if line == "Weekly limits":
            section = "weekly"
            pending_name = None
            pending_reset = None
            continue
        if line.startswith("Learn more about usage limits"):
            continue
        if line.startswith("Last updated"):
            break

        if line.startswith("Resets"):
            pending_reset = line
            continue

        m = re.match(r"^(\d+)%\s*used$", line)
        if m:
            pct = int(m.group(1))
            if section == "session":
                session = {"pct_used": pct, "reset_text": pending_reset or ""}
            elif section == "weekly" and pending_name:
                weekly.append({"name": pending_name, "pct_used": pct, "reset_text": pending_reset or ""})
            pending_reset = None
            pending_name = None
            continue

        if section == "weekly":
            pending_name = line

    return {"plan_name": plan_name, "session": session, "weekly": weekly}


def write_data(payload: dict) -> None:
    payload["last_updated_iso"] = datetime.now(timezone.utc).isoformat()
    DATA_FILE_TMP.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(DATA_FILE_TMP, DATA_FILE)


def write_status(status: str, detail: str = "") -> None:
    write_data({"status": status, "detail": detail, "plan_name": "", "session": {}, "weekly": []})


def get_body_text() -> str:
    try:
        return window.evaluate_js('document.body ? document.body.innerText : ""') or ""
    except Exception:
        return ""


LOGGED_OUT_MARKERS = ("Continue with Google", "Continue with email", "Continue with SSO")


def page_state(body_text: str) -> str:
    """"on_settings" (data siap dibaca) / "logged_out" (perlu login) /
    "logged_in_wrong_page" (udah login tapi SPA-nya nyasar ke /new, bukan
    render halaman settings -- hash-route claude.ai kadang nggak otomatis
    buka overlay-nya, perlu di-reload paksa)."""
    if "Plan usage limits" in body_text:
        return "on_settings"
    if any(marker in body_text for marker in LOGGED_OUT_MARKERS):
        return "logged_out"
    return "logged_in_wrong_page"


DEBUG_FILE = BASE_DIR / "webview_debug.txt"


def log_debug(msg: str):
    try:
        with DEBUG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except Exception:
        pass


def wait_for_settings_page(timeout_seconds: float) -> bool:
    """Poll sampai halaman "Plan usage limits" kebaca, sambil paksa reload
    kalau ternyata sudah login tapi SPA nyasar ke halaman lain. Return True
    kalau berhasil, False kalau timeout (asumsikan perlu login manual)."""
    deadline = time.time() + timeout_seconds
    last_forced_reload = 0.0
    while time.time() < deadline:
        body_text = get_body_text()
        state = page_state(body_text)
        log_debug(f"wait_for_settings_page state={state} body_len={len(body_text)} snip={body_text[:80]!r}")
        if state == "on_settings":
            return True
        if state == "logged_in_wrong_page" and time.time() - last_forced_reload > 5:
            # Sudah login tapi kepentok /new#settings/usage yang nggak
            # auto-render overlay-nya -- reload paksa ke URL settings.
            try:
                window.load_url(USAGE_URL)
            except Exception:
                pass
            last_forced_reload = time.time()
        time.sleep(LOGIN_POLL_INTERVAL_SECONDS)
    return False


def worker():
    first_run = not LOGGED_IN_FLAG.exists()

    if first_run:
        write_status(
            "waiting_login",
            'Menunggu login manual di jendela browser (pakai "Continue with email" kalau akun cuma bisa Google)...',
        )
        if not wait_for_settings_page(LOGIN_TIMEOUT_SECONDS):
            write_status("error", "Timeout menunggu login manual")
            try:
                window.destroy()
            except Exception:
                pass
            return

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        LOGGED_IN_FLAG.write_text("ok", encoding="utf-8")
        try:
            window.hide()
        except Exception:
            pass

    while True:
        try:
            try:
                window.load_url(USAGE_URL)
            except Exception:
                pass
            time.sleep(3)
            body_text = get_body_text()
            state = page_state(body_text)
            log_debug(f"main_loop state={state} body_len={len(body_text)} snip={body_text[:80]!r}")

            if state != "on_settings":
                # Sering ini cuma hiccup routing SPA sesaat (claude.ai
                # kadang nyasar ke /new#settings/usage tanpa auto-render
                # overlay-nya) -- coba pulihkan CEPAT & DIAM-DIAM dulu,
                # jangan langsung declare sesi habis & munculin window.
                if wait_for_settings_page(30):
                    body_text = get_body_text()
                    state = "on_settings"
                    log_debug("main_loop: pulih cepat dari hiccup, nggak perlu login ulang")
                else:
                    if LOGGED_IN_FLAG.exists():
                        LOGGED_IN_FLAG.unlink()
                    write_status("session_expired", "Sesi login sudah tidak valid, perlu login ulang")
                    try:
                        window.show()
                    except Exception:
                        pass
                    if wait_for_settings_page(LOGIN_TIMEOUT_SECONDS):
                        body_text = get_body_text()
                        state = "on_settings"
                        LOGGED_IN_FLAG.write_text("ok", encoding="utf-8")
                        try:
                            window.hide()
                        except Exception:
                            pass
                    else:
                        write_status("error", "Timeout menunggu login ulang")
                        time.sleep(REFRESH_INTERVAL_SECONDS)
                        continue

            if state == "on_settings":
                parsed = parse_usage_text(body_text)
                parsed["status"] = "ok"
                parsed["detail"] = ""
                write_data(parsed)
                try:
                    window.hide()
                except Exception:
                    pass
        except Exception as exc:
            write_status("error", f"Gagal ambil data: {exc}")

        time.sleep(REFRESH_INTERVAL_SECONDS)


def run():
    global window
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # SELALU dibuat visible dulu -- window yang dibuat hidden=True dari
    # awal ternyata WebView2-nya nggak sepenuhnya render/load kontennya
    # (evaluate_js balikin body kosong), bikin sesi valid kedeteksi salah
    # sebagai "logged out". Kalau sesi masih valid, `worker()` bakal
    # nyembunyiin window ini lagi dalam beberapa detik (lihat window.hide()
    # di kedua tempat: abis first-login sukses, dan abis parse sukses).
    window = webview.create_window(
        "Login Claude (biarkan terbuka)",
        USAGE_URL,
        width=900,
        height=750,
        hidden=False,
    )
    webview.start(
        worker,
        gui="edgechromium",
        private_mode=False,
        storage_path=str(PROFILE_DIR),
    )


def acquire_singleton_lock():
    """Lock eksklusif Windows (msvcrt) di `webview_usage_source.lock` --
    WebView2/pywebview TIDAK otomatis mengunci `storage_path` seperti
    Chromium+Playwright dulu, jadi tanpa ini 2 instance bisa jalan
    bareng dan rebutan profile (bikin deteksi login jadi flaky/salah).
    Return file handle yang HARUS ditahan tetap terbuka selama proses
    hidup (jangan di-garbage-collect), atau None kalau instance lain
    sudah pegang lock-nya."""
    try:
        f = open(LOCK_FILE, "w")
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        return None


if __name__ == "__main__":
    _lock_handle = acquire_singleton_lock()
    if _lock_handle is None:
        print("Instance lain sudah jalan (lock terpegang) -- keluar.")
        sys.exit(0)
    try:
        run()
    finally:
        try:
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            _lock_handle.close()
        except Exception:
            pass
