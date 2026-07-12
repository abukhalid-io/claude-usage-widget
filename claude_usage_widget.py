"""
Claude Usage Widget
====================
Widget desktop minimalis (frameless, always-on-top, semi-transparan) untuk
memantau penggunaan token Claude, dari DUA sumber data:

  A. LOKAL, real-time tiap 2 detik: log Claude Code sendiri
     (`~/.claude/projects/**/*.jsonl`) -> context window % & token per
     model (ring gauge + badge Opus/Sonnet/Haiku/Fable).
  B. RESMI dari claude.ai/settings/usage (plan, sesi 5 jam, mingguan 7
     hari) -- dibaca lewat `webview_usage_source.py`, proses WebView2
     terpisah (BUKAN Playwright/automation). Login manual sekali di
     jendela yang muncul, sesudah itu berjalan sendiri di background.

CATATAN SOAL DATA LOKAL (A):
Tiap kali Claude Code membalas, ia menambahkan satu baris JSON ke file
sesi (`*.jsonl`) berisi `message.usage` (input/output/cache tokens) dan
`message.model`. Widget ini:
  1. Cari file sesi yang paling baru dimodifikasi (= sesi yang lagi aktif).
  2. Baca entry `usage` PALING TERAKHIR di situ untuk hitung "context
     terpakai" (input + cache_creation + cache_read) dibanding batas
     context window model YANG SEDANG DIPAKAI (`MODEL_CONTEXT_WINDOWS`,
     angka resmi per model dari platform.claude.com/docs) -> ini yang
     jadi angka di ring gauge. Ini metrik yang AKURAT (bukan estimasi),
     dan relevan langsung dengan saran "pindah chat baru kalau history
     kepanjangan".
  3. Sesekali (throttled, lihat `_FULL_SCAN_INTERVAL_SECONDS`) scan penuh
     file sesi itu untuk hitung total pesan & total token di sesi ini.

CATATAN SOAL DATA RESMI (B) -- riwayat percobaan sebelum ketemu yang jalan:
  1. Scraping lewat Playwright -- claude.ai konsisten menampilkan
     tantangan Cloudflare "Just a moment..." ke Chromium yang dikendalikan
     protokol otomasi (CDP), yang gampang dideteksi (navigator.webdriver,
     dll).
  2. Statusline hook resmi Claude Code (`rate_limits` field) -- ternyata
     tidak terpicu oleh giliran chat di Claude Desktop app.
  3. Endpoint resmi `GET /api/oauth/usage` -- butuh token OAuth di
     `~/.claude/.credentials.json` yang nggak ada di environment yang
     CLI-nya login pakai API key, bukan OAuth akun Claude.ai.
  4. AKHIRNYA JALAN: `pywebview` (WebView2 asli, bukan automation tool)
     -- dites langsung ke claude.ai dan halaman login ASLI muncul normal
     tanpa tantangan bot apapun, karena nggak ada penanda otomasi CDP
     sama sekali. User login manual beneran (ketik password/OTP sendiri)
     persis kayak browser biasa -- BUKAN bypass proteksi bot.
     Catatan: "Continue with Google" tetap diblokir Google sendiri
     (kebijakan anti-phishing "disallowed_useragent" buat semua embedded
     webview) -- pakai "Continue with email" kalau akun cuma bisa Google.

Cara pakai:
    pip install -r requirements.txt
    python claude_usage_widget.py

Kontrol:
    - Klik kiri + tahan (drag) di mana saja pada body widget -> geser widget.
    - Tombol "x" kecil di pojok kanan atas -> minimize ke system tray (klik
      kanan ikon tray buat Show/Refresh/Compact/Quit beneran).
    - Tombol compact -> mode ringkas (cuma ring + tray).
    - Klik badge model (Opus/Sonnet/Haiku/Fable) -> pin ring ke model itu.
    - Widget selalu di atas jendela lain (always on top).
"""

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QLinearGradient,
    QRadialGradient, QConicalGradient, QIcon, QPixmap, QAction,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton,
    QGraphicsDropShadowEffect, QSizePolicy, QSystemTrayIcon, QMenu, QProgressBar,
)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_JSON_PATH = Path.home() / ".claude.json"

BASE_DIR = Path(__file__).resolve().parent
WEBVIEW_SCRIPT = BASE_DIR / "webview_usage_source.py"
WEEKLY_DATA_FILE = BASE_DIR / "usage_data.json"
# Kalau webview source belum update lebih dari ini (mis. lagi nunggu
# login ulang manual), anggap datanya basi.
WEEKLY_STALE_AFTER_SECONDS = 15 * 60

# Batas context window ASLI per keluarga model (token) -- dari dokumentasi
# resmi https://platform.claude.com/docs/en/about-claude/models/overview.
# Beda-beda per model, jadi JANGAN dipukul rata jadi satu angka.
MODEL_CONTEXT_WINDOWS = {
    "opus": 1_000_000,
    "sonnet": 1_000_000,
    "fable": 1_000_000,
    "haiku": 200_000,
}
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000  # fallback kalau model tidak dikenali

# Ambang warna ring, berdasarkan persentase TERPAKAI (bukan sisa)
WARN_USED_PCT = 75      # >= ini -> kuning/oranye (waspada)
CRITICAL_USED_PCT = 85  # >= ini -> merah (kritis)
# Sesi dianggap "panjang" kalau jumlah balasan Claude sudah sebanyak ini
SESSION_MESSAGE_ALARM = 40

# Nudge "kebanyakan pakai Opus": trigger kalau porsi token Opus di sesi ini
# >= ini % dari total token semua model, DAN sudah lewat ambang absolut
# (biar nggak berisik di sesi pendek yang baru mulai).
OPUS_SHARE_WARN_PCT = 40
OPUS_MIN_TOKENS_FOR_NUDGE = 5_000

# Seberapa sering scan penuh file sesi (mahal untuk file besar) diulang
_FULL_SCAN_INTERVAL_SECONDS = 20.0
# Berapa byte terakhir file yang dibaca untuk cari entry usage paling baru
_TAIL_CHUNK_BYTES = 400_000

# Warna khas per keluarga model -- satu keluarga palet hangat (terracotta/
# cokelat/emas) senada tema brand Claude, BUKAN rainbow ungu/biru/hijau
# yang nggak nyambung sama estetika Claude.
MODEL_FAMILY_COLORS = {
    "opus":   "#B45309",   # rust/amber tua
    "sonnet": "#D97757",   # terracotta -- warna brand utama Claude
    "haiku":  "#E8A34D",   # emas hangat
    "fable":  "#8B5E34",   # cokelat sienna
}

# Warna penanda badge yang lagi dipin/ditampilkan di ring -- emas terang,
# sengaja beda dari ke-4 warna model di atas (termasuk sonnet yang sekarang
# pakai terracotta persis) biar highlight-nya tetap kelihatan jelas.
PIN_HIGHLIGHT_COLOR = "#FBBF24"


def family_key_for_model(model_id: str) -> str | None:
    """"claude-sonnet-5" -> "sonnet". None kalau tidak dikenali."""
    low = (model_id or "").lower()
    for family in MODEL_FAMILY_COLORS:
        if family in low:
            return family
    return None


def style_for_model(model_id: str) -> dict:
    """
    Ubah id model mentah (mis. "claude-sonnet-5", "claude-opus-4-8",
    "claude-haiku-4-5-20251001") jadi label yang sama persis dengan yang
    dipakai Claude Code sendiri ("Sonnet 5", "Opus 4.8", "Haiku 4.5") +
    warna indikator khas per keluarga model.
    """
    low = (model_id or "").lower()
    family = family_key_for_model(model_id)
    if family is None:
        return {"color": "#a1a1aa", "label": model_id or "?"}
    idx = low.find(family)
    version_raw = low[idx + len(family):].strip("-")
    parts = [p for p in version_raw.split("-") if p]
    # buang suffix tanggal build (angka 8 digit, mis. 20251001)
    if parts and re.fullmatch(r"\d{8}", parts[-1]):
        parts = parts[:-1]
    version = ".".join(parts)
    label = f"{family.capitalize()} {version}".strip()
    return {"color": MODEL_FAMILY_COLORS[family], "label": label}


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


@dataclass
class UsageState:
    status: str = "loading"        # "loading" / "ok" / "no_session" / "error"
    detail: str = ""
    model: str = ""
    context_tokens: int = 0
    context_window: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    session_messages: int = 0
    session_total_tokens: int = 0
    project_name: str = ""
    last_activity_text: str = ""
    model_usage: dict = None       # {"sonnet": tokens, "opus": tokens, ...} -- total token per keluarga model di sesi ini
    context_by_family: dict = None     # {"sonnet": context_tokens_terakhir, ...} -- context snapshot terakhir per model
    model_id_by_family: dict = None    # {"sonnet": "claude-sonnet-5", ...} -- id model terakhir per keluarga


class ClaudeCodeUsageSource:
    """
    Sumber data ASLI: baca file sesi JSONL Claude Code yang paling baru
    dimodifikasi (= sesi aktif) langsung dari disk. Lihat header modul.
    """

    def __init__(self):
        self._cache_path = None
        self._cache_full_stats = (0, 0, {}, {}, {})
        self._last_full_scan_time = 0.0

    def fetch(self) -> UsageState:
        path = self._find_active_session_file()
        if path is None:
            return UsageState(status="no_session", detail="Belum ada sesi Claude Code ditemukan")

        try:
            model, context_tokens, last_ts = self._read_latest_usage(path)
        except OSError as exc:
            return UsageState(status="error", detail=str(exc))

        if model is None:
            return UsageState(status="no_session", detail="Sesi belum ada data usage")

        now = time.time()
        if path != self._cache_path or (now - self._last_full_scan_time) > _FULL_SCAN_INTERVAL_SECONDS:
            try:
                self._cache_full_stats = self._scan_full(path)
                self._last_full_scan_time = now
                self._cache_path = path
            except OSError:
                pass

        (session_messages, session_total_tokens, model_usage,
         context_by_family, model_id_by_family) = self._cache_full_stats
        project_name = path.parent.name.replace("C--Users-randex-", "").replace("-", " / ")
        family = family_key_for_model(model)
        context_window = MODEL_CONTEXT_WINDOWS.get(family, DEFAULT_CONTEXT_WINDOW_TOKENS)

        # Data tail-read (2 detik sekali) selalu paling akurat buat model yang
        # BARU SAJA dipakai -- overwrite hasil scan penuh (throttled 20 detik)
        # yang mungkin agak basi untuk keluarga model yang lagi aktif ini.
        context_by_family = dict(context_by_family)
        model_id_by_family = dict(model_id_by_family)
        if family:
            context_by_family[family] = context_tokens
            model_id_by_family[family] = model

        return UsageState(
            status="ok",
            model=model,
            context_tokens=context_tokens,
            context_window=context_window,
            session_messages=session_messages,
            session_total_tokens=session_total_tokens,
            project_name=project_name,
            context_by_family=context_by_family,
            model_id_by_family=model_id_by_family,
            last_activity_text=self._format_age(last_ts),
            model_usage=model_usage,
        )

    @staticmethod
    def _find_active_session_file():
        if not CLAUDE_PROJECTS_DIR.exists():
            return None
        candidates = list(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"))
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _read_latest_usage(path: Path):
        """Baca chunk terakhir file (murah, O(1) walau filenya besar) buat
        cari entry assistant paling baru yang punya `usage`."""
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - _TAIL_CHUNK_BYTES))
            data = f.read()

        for line in reversed(data.decode("utf-8", errors="ignore").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            usage = (obj.get("message") or {}).get("usage")
            if not usage:
                continue
            context_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            return (obj.get("message") or {}).get("model", "?"), context_tokens, obj.get("timestamp")
        return None, 0, None

    @staticmethod
    def _scan_full(path: Path):
        """Hitung jumlah balasan Claude, total token input+output, breakdown
        token per keluarga model, DAN context snapshot + id model terakhir
        per keluarga (dipakai kalau user "pin" ring ke model non-aktif).
        Di-throttle di `fetch()` karena file bisa besar."""
        messages = 0
        total_tokens = 0
        model_usage = {family: 0 for family in MODEL_FAMILY_COLORS}
        context_by_family = {}
        model_id_by_family = {}
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"type":"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                messages += 1
                turn_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                total_tokens += turn_tokens
                model_id = msg.get("model", "")
                family = family_key_for_model(model_id)
                if family:
                    model_usage[family] += turn_tokens
                    # File dibaca berurutan dari atas -> nilai ini otomatis
                    # jadi "yang paling terakhir" begitu loop selesai.
                    context_by_family[family] = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    model_id_by_family[family] = model_id
        return messages, total_tokens, model_usage, context_by_family, model_id_by_family

    @staticmethod
    def _format_age(iso_ts) -> str:
        if not iso_ts:
            return ""
        try:
            ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        except ValueError:
            return ""
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < 60:
            return "baru saja"
        if age < 3600:
            return f"{int(age // 60)} menit lalu"
        return f"{int(age // 3600)} jam lalu"


def read_account_info() -> dict:
    """Baca email & nama organisasi akun Claude Code dari `~/.claude.json`
    (lokal, sekali baca -- tidak berubah-ubah tiap detik). Nama plan
    (Pro/Max) ada di `LiveUsageSource` (dari webview_usage_source.py)."""
    try:
        raw = json.loads(CLAUDE_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"email": "", "org": ""}
    oauth = raw.get("oauthAccount") or {}
    return {
        "email": oauth.get("emailAddress", ""),
        "org": oauth.get("organizationName", ""),
    }


@dataclass
class WeeklyLimit:
    name: str
    pct_used: int
    reset_text: str


@dataclass
class LiveUsageState:
    """Kuota RESMI (session 5 jam + weekly 7 hari + plan) yang benar-benar
    dibaca dari claude.ai/settings/usage lewat `webview_usage_source.py`
    (jendela WebView2 asli, login manual sekali -- bukan Playwright/
    automation, jadi nggak kena Cloudflare)."""
    status: str = "loading"    # loading/ok/waiting_login/session_expired/error/stale
    detail: str = ""
    plan_name: str = ""
    session_pct_used: int = None
    session_reset_text: str = ""
    weekly: list = None            # list[WeeklyLimit]
    last_updated_text: str = ""


class LiveUsageSource:
    """Baca `usage_data.json` yang ditulis `webview_usage_source.py`
    (proses WebView2 terpisah, refresh tiap 3 menit)."""

    def fetch(self) -> LiveUsageState:
        if not WEEKLY_DATA_FILE.exists():
            return LiveUsageState(status="loading", detail="Menunggu webview source mulai...")
        try:
            raw = json.loads(WEEKLY_DATA_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return LiveUsageState(status="loading", detail="Membaca data...")

        status = raw.get("status", "ok")
        detail = raw.get("detail", "")
        last_updated_iso = raw.get("last_updated_iso")
        age_seconds = None
        last_updated_text = ""
        if last_updated_iso:
            try:
                ts = datetime.fromisoformat(last_updated_iso)
                age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
                last_updated_text = self._format_age(age_seconds)
            except ValueError:
                pass

        if status == "ok" and age_seconds is not None and age_seconds > WEEKLY_STALE_AFTER_SECONDS:
            status = "stale"
            detail = "Webview source sepertinya berhenti"

        session = raw.get("session") or {}
        weekly = [
            WeeklyLimit(name=w.get("name", "?"), pct_used=int(w.get("pct_used", 0)), reset_text=w.get("reset_text", ""))
            for w in raw.get("weekly", [])
        ]

        return LiveUsageState(
            status=status,
            detail=detail,
            plan_name=raw.get("plan_name", ""),
            session_pct_used=session.get("pct_used"),
            session_reset_text=session.get("reset_text", ""),
            weekly=weekly,
            last_updated_text=last_updated_text,
        )

    @staticmethod
    def _format_age(seconds: float) -> str:
        if seconds < 60:
            return "baru saja"
        minutes = int(seconds // 60)
        if minutes < 60:
            return f"{minutes} menit lalu"
        return f"{int(minutes // 60)} jam lalu"


def ensure_webview_source_running():
    """Spawn webview_usage_source.py sebagai subprocess background kalau
    belum jalan. Aman dipanggil berkali-kali: kalau instance lain sudah
    pegang profile WebView2, yang baru cukup gagal & keluar (tidak dobel)."""
    if not WEBVIEW_SCRIPT.exists():
        return
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen(
        [sys.executable, str(WEBVIEW_SCRIPT)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def build_tray_icon() -> QIcon:
    """Bikin ikon tray sederhana (lingkaran ungu + huruf "C") langsung
    lewat QPainter -- nggak butuh file .ico/.png eksternal."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#D97757"))  # terracotta -- senada tema Claude
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.setPen(QColor("#ffffff"))
    font = QFont("Segoe UI", 30, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "C")
    painter.end()
    return QIcon(pixmap)


def gradient_for_used_pct(used_pct: float) -> tuple[str, str]:
    """
    Kembalikan (warna_awal, warna_akhir) untuk arc gradient gauge,
    berdasarkan persentase TERPAKAI: aman -> waspada (>=75%) -> kritis (>=85%).
    """
    if used_pct >= CRITICAL_USED_PCT:
        return "#f97316", "#dc2626"    # oranye -> merah (kritis)
    if used_pct >= WARN_USED_PCT:
        return "#fbbf24", "#f97316"    # kuning -> oranye (waspada)
    return "#4ade80", "#16a34a"        # hijau muda -> hijau tua (aman)


class RingProgress(QWidget):
    """
    Gauge "coin" bergaya neumorphic: bezel dark timbul, indentasi dalam,
    dan arc progress dengan gradient warna (mirip gauge speedometer di
    referensi desain).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pct = 100.0
        self._grad_start = QColor("#4ade80")
        self._grad_end = QColor("#16a34a")
        self._center_text = "100%"
        self._center_color = QColor("#f5f5f7")
        self.setFixedSize(180, 180)

    def set_value(self, pct: float, color_start_hex: str, color_end_hex: str,
                  center_text: str = None, center_color_hex: str = "#f5f5f7"):
        """`pct` = seberapa penuh arc-nya (0-100, otomatis di-clamp).
        `center_text` override teks di tengah -- kalau None, default ke
        f"{int(pct)}%". Dipakai buat kasus overpakai (>100%) supaya angka
        aslinya tetap kelihatan, bukan cuma mentok "0%"."""
        self._pct = max(0.0, min(100.0, pct))
        self._grad_start = QColor(color_start_hex)
        self._grad_end = QColor(color_end_hex)
        self._center_text = center_text if center_text is not None else f"{int(self._pct)}%"
        self._center_color = QColor(center_color_hex)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        outer_margin = 8
        outer_rect = QRectF(outer_margin, outer_margin, w - outer_margin * 2, h - outer_margin * 2)

        # 1) Bezel luar timbul (neumorphic): radial gradient miring
        #    biar kelihatan cembung/raised seperti "coin".
        bezel_grad = QRadialGradient(w * 0.35, h * 0.3, w * 0.9)
        bezel_grad.setColorAt(0.0, QColor("#46464d"))
        bezel_grad.setColorAt(1.0, QColor("#222226"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bezel_grad))
        painter.drawEllipse(outer_rect)

        # Highlight tipis di rim atas-kiri supaya makin terasa timbul
        highlight_pen = QPen(QColor(255, 255, 255, 25), 2)
        painter.setPen(highlight_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(outer_rect.adjusted(1, 1, -1, -1), 45 * 16, 120 * 16)

        # 2) Groove tempat arc progress berjalan
        groove_margin = 22
        groove_rect = QRectF(groove_margin, groove_margin, w - groove_margin * 2, h - groove_margin * 2)

        track_pen = QPen(QColor(0, 0, 0, 90), 16)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawArc(groove_rect, 0, 360 * 16)

        # 3) Arc progress dengan gradient warna (conical gradient)
        conic = QConicalGradient(w / 2, h / 2, 90)
        conic.setColorAt(0.0, self._grad_start)
        conic.setColorAt(max(0.001, self._pct / 100.0), self._grad_end)
        conic.setColorAt(1.0, self._grad_end)
        grad_pen = QPen(QBrush(conic), 16)
        grad_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(grad_pen)
        span_angle = int(360 * (self._pct / 100.0) * 16)
        painter.drawArc(groove_rect, 90 * 16, -span_angle)

        # 4) Lingkaran dalam (indentasi/concave) tempat teks
        inner_margin = 40
        inner_rect = QRectF(inner_margin, inner_margin, w - inner_margin * 2, h - inner_margin * 2)
        inner_grad = QRadialGradient(w * 0.5, h * 0.42, w * 0.5)
        inner_grad.setColorAt(0.0, QColor("#1c1c1f"))
        inner_grad.setColorAt(1.0, QColor("#0d0d0f"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(inner_grad))
        painter.drawEllipse(inner_rect)

        # 5) Teks di tengah (persentase, atau override kalau overpakai)
        font_size = 26 if len(self._center_text) <= 4 else 20
        painter.setPen(self._center_color)
        painter.setFont(QFont("Segoe UI", font_size, QFont.Weight.Bold))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._center_text)


class ModelBadge(QLabel):
    """Badge model yang bisa diklik (pill) -- klik buat "pin" ring gauge
    ke keluarga model ini, klik lagi buat lepas pin (balik ngikutin model
    yang lagi aktif dipakai Claude)."""

    clicked = pyqtSignal(str)

    def __init__(self, family: str, parent=None):
        super().__init__(parent)
        self.family = family
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Cegah Windows nggambar focus-ring warna aksen sistem (oranye dkk)
        # nimpa border custom kita begitu badge ini diklik.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.family)
        super().mousePressEvent(event)


class ToastLabel(QLabel):
    """Label peringatan kecil yang berkedip (fade in/out)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "color: #fbbf24; font-size: 11px; font-weight: 600; "
            "background: transparent;"
        )
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._opacity_anim = None
        self.hide()

    def show_message(self, text: str):
        self.setText(text)
        self.show()
        effect = self.graphicsEffect()
        # Animasi berkedip sederhana lewat stylesheet toggling opacity text
        self._blink_state = True
        if hasattr(self, "_blink_timer"):
            self._blink_timer.stop()
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start(600)

    def _toggle_blink(self):
        self._blink_state = not self._blink_state
        self.setStyleSheet(
            f"color: {'#fbbf24' if self._blink_state else '#7c5a0a'}; "
            "font-size: 11px; font-weight: 600; background: transparent;"
        )

    def clear_message(self):
        if hasattr(self, "_blink_timer"):
            self._blink_timer.stop()
        self.hide()


class UsageWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.source = ClaudeCodeUsageSource()
        self.weekly_source = LiveUsageSource()
        self.account_info = read_account_info()  # statis, dibaca sekali aja
        self._drag_pos = None
        self._selected_family = None   # None = ring auto-ikut model yang lagi aktif dipakai
        self._compact_mode = False
        self._full_mode_widgets = []   # diisi di _build_ui, disembunyikan saat compact mode

        self._setup_window()
        self._build_ui()
        self._setup_tray()
        self._setup_timer()

    # ------------------------------------------------------------
    # Window setup: frameless, transparan, always on top, draggable
    # ------------------------------------------------------------
    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # tidak muncul di taskbar
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(300, self._target_height())

        # Posisikan di pojok kanan atas layar saat pertama kali dibuka
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.width() - self.width() - 24, 24)

    def _build_ui(self):
        # Root layout di widget utama -- bikin `card` otomatis ikut resize
        # tiap kali window di-resize (toggle grafik/compact mode), jadi
        # nggak ada area transparan sisa yang nembusin jendela di belakang.
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # Container utama yang jadi "card" dengan background semi-transparan
        self.card = QWidget(self)
        self.card.setObjectName("card")
        root_layout.addWidget(self.card)
        self.card.setStyleSheet(
            """
            #card {
                background-color: rgba(24, 24, 27, 210);
                border-radius: 18px;
                border: 1px solid rgba(255,255,255,25);
            }
            """
        )

        # Shadow halus di sekeliling card -- dipasang ke `card` (bukan ke
        # window utama) supaya bayangannya ngikutin bentuk rounded card,
        # bukan kotak penuh window yang bikin sudut bawah kelihatan kotak.
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 160))
        self.card.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self.card)
        outer.setContentsMargins(16, 12, 16, 14)
        outer.setSpacing(8)

        # --- Header row: judul + tombol close ---
        header = QHBoxLayout()
        title = QLabel("Claude Usage")
        title.setStyleSheet(
            "color: #e4e4e7; font-size: 12px; font-weight: 600; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch()

        def _mini_btn(text, tooltip):
            btn = QPushButton(text)
            btn.setFixedSize(20, 20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tooltip)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setStyleSheet(
                """
                QPushButton {
                    color: rgba(228,228,231,120);
                    background: transparent;
                    border: none;
                    font-size: 12px;
                    font-weight: bold;
                }
                QPushButton:hover { color: #93c5fd; }
                """
            )
            return btn

        self.compact_btn = _mini_btn("▭", "Toggle compact mode")
        self.compact_btn.clicked.connect(self._toggle_compact_mode)
        header.addWidget(self.compact_btn)

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("Minimize ke system tray")
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setStyleSheet(
            """
            QPushButton {
                color: rgba(228,228,231,120);
                background: transparent;
                border: none;
                font-size: 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: #ef4444;
            }
            """
        )
        self.close_btn.clicked.connect(self.hide)  # bukan quit -- minimize ke tray
        header.addWidget(self.close_btn)
        outer.addLayout(header)

        # --- Ring: SATU-SATUNYA yang tetap kelihatan di compact mode ---
        body = QVBoxLayout()
        body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.ring = RingProgress(self.card)
        body.addWidget(self.ring, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addLayout(body)

        # --- Container SATU widget buat semua detail (badge, context,
        # kuota, dst). Dibungkus jadi satu widget (bukan di-hide satu-satu)
        # supaya pas compact mode di-toggle, dia collapse total ke 0px --
        # kalau di-hide satu-satu, spacing antar item yang disembunyikan
        # tetap kepakai sama Qt dan nyisain kotak kosong panjang. ---
        self.details_container = QWidget()
        details = QVBoxLayout(self.details_container)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(8)

        # Grid badge model: satu pill per model, semua kelihatan sekaligus
        # biar bisa dipantau bareng-bareng. Yang lagi aktif disorot solid,
        # yang lain outline redup + jumlah token yang sudah dipakai.
        self.model_grid_widget = QWidget()
        model_grid = QGridLayout(self.model_grid_widget)
        model_grid.setContentsMargins(0, 0, 0, 0)
        model_grid.setSpacing(6)

        self.model_badges = {}
        families = list(MODEL_FAMILY_COLORS.keys())  # ["opus", "sonnet", "haiku", "fable"]
        for i, family in enumerate(families):
            badge = ModelBadge(family)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedHeight(22)
            badge.clicked.connect(self._on_model_badge_clicked)
            model_grid.addWidget(badge, i // 2, i % 2)
            self.model_badges[family] = badge
        details.addWidget(self.model_grid_widget)

        self.context_label = QLabel()
        self.context_label.setStyleSheet(
            "color: #a1a1aa; font-size: 10px; background: transparent;"
        )
        self.context_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        details.addWidget(self.context_label)

        self.reset_label = QLabel()
        self.reset_label.setStyleSheet(
            "color: #a1a1aa; font-size: 10px; background: transparent;"
        )
        self.reset_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        details.addWidget(self.reset_label)

        self.project_label = QLabel()
        self.project_label.setStyleSheet("background: transparent;")
        self.project_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.project_label.setWordWrap(True)
        details.addWidget(self.project_label)

        # --- Separator tipis ---
        self.sep = QLabel()
        self.sep.setFixedHeight(1)
        self.sep.setStyleSheet("background-color: rgba(255,255,255,25); margin: 2px 0;")
        details.addWidget(self.sep)

        # --- Kuota RESMI (session 5 jam + weekly 7 hari + plan), dibaca
        # dari webview_usage_source.py (WebView2 asli, bukan scraping). ---
        self.plan_status_label = QLabel()
        self.plan_status_label.setStyleSheet(
            "color: #e4e4e7; font-size: 10px; font-weight: 600; background: transparent;"
        )
        self.plan_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.plan_status_label.setWordWrap(True)
        details.addWidget(self.plan_status_label)

        self.session_quota_text, self.session_quota_bar = self._build_quota_row("Sesi (5 jam)")
        details.addWidget(self.session_quota_text)
        details.addWidget(self.session_quota_bar)

        self.weekly_quota_text, self.weekly_quota_bar = self._build_quota_row("Mingguan (semua model)")
        details.addWidget(self.weekly_quota_text)
        details.addWidget(self.weekly_quota_bar)

        # Slot kuota per-model (mis. "Fable 3%") -- jumlah item ini dinamis
        # tergantung apa yang claude.ai tampilkan, jadi disiapkan beberapa
        # slot QProgressBar sekaligus lalu disembunyikan/dipakai sesuai
        # kebutuhan di `_update_weekly_section` (bukan bikin widget baru
        # tiap tick, biar hemat & nggak bikin layout goyang).
        self.model_quota_rows = []
        for _ in range(2):
            text_label, bar = self._build_quota_row("")
            details.addWidget(text_label)
            details.addWidget(bar)
            text_label.setVisible(False)
            bar.setVisible(False)
            self.model_quota_rows.append((text_label, bar))

        self.updated_label = QLabel()
        self.updated_label.setStyleSheet(
            "color: #71717a; font-size: 9px; background: transparent;"
        )
        self.updated_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        details.addWidget(self.updated_label)

        outer.addWidget(self.details_container)

        # --- Toast peringatan (tetap kelihatan walau compact) ---
        self.toast = ToastLabel(self.card)
        outer.addWidget(self.toast)

        outer.addStretch()

        # Satu-satunya yang disembunyikan saat compact mode -- lihat komentar
        # di atas soal kenapa dibungkus satu container, bukan di-hide 1-per-1.
        self._full_mode_widgets = [self.details_container]

    @staticmethod
    def _build_quota_row(label_text: str):
        """Bikin satu baris kuota: label teks kecil + progress bar tipis di
        bawahnya. Dipakai buat "Sesi (5 jam)" dan "Mingguan (7 hari)"."""
        text_label = QLabel(label_text)
        text_label.setStyleSheet(
            "color: #a1a1aa; font-size: 10px; background: transparent;"
        )
        text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        bar.setStyleSheet(
            """
            QProgressBar { background-color: rgba(255,255,255,20); border-radius: 3px; }
            QProgressBar::chunk { background-color: #4ade80; border-radius: 3px; }
            """
        )
        return text_label, bar

    @staticmethod
    def _style_quota_bar(bar: QProgressBar, color: str):
        bar.setStyleSheet(
            f"""
            QProgressBar {{ background-color: rgba(255,255,255,20); border-radius: 3px; }}
            QProgressBar::chunk {{ background-color: {color}; border-radius: 3px; }}
            """
        )

    def _set_quota_row(self, text_label: QLabel, bar: QProgressBar, name: str, pct_used, reset_text: str):
        if pct_used is None:
            text_label.setText(f"{name} — tidak ada data")
            bar.setValue(0)
            self._style_quota_bar(bar, "#3f3f46")
            return
        text_label.setText(f"{name} · {int(pct_used)}% used" + (f" · {reset_text}" if reset_text else ""))
        bar.setValue(max(0, min(100, int(pct_used))))
        if pct_used >= CRITICAL_USED_PCT:
            color = "#dc2626"
        elif pct_used >= WARN_USED_PCT:
            color = "#f97316"
        else:
            color = "#4ade80"
        self._style_quota_bar(bar, color)

    def _setup_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)  # update tiap 2 detik (ubah sesuai kebutuhan)
        self._tick()  # panggil sekali di awal biar langsung ada data

    # ------------------------------------------------------------
    # System tray: minimize ke tray alih-alih quit beneran
    # ------------------------------------------------------------
    def _setup_tray(self):
        self.tray = QSystemTrayIcon(build_tray_icon(), self)
        self.tray.setToolTip("Claude Usage Widget")

        menu = QMenu()

        self.tray_show_action = QAction("Show/Hide", self)
        self.tray_show_action.triggered.connect(self._toggle_visibility)
        menu.addAction(self.tray_show_action)

        refresh_action = QAction("Refresh sekarang", self)
        refresh_action.triggered.connect(self._tick)
        menu.addAction(refresh_action)

        self.tray_compact_action = QAction("Compact mode", self)
        self.tray_compact_action.setCheckable(True)
        self.tray_compact_action.triggered.connect(self._toggle_compact_mode)
        menu.addAction(self.tray_compact_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit_app)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_visibility()

    def _toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _quit_app(self):
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):
        """Klik X di title bar (kalau ada) -> minimize ke tray, bukan quit."""
        event.ignore()
        self.hide()

    # ------------------------------------------------------------
    # Toggle compact mode
    # ------------------------------------------------------------
    _BASE_HEIGHT_FULL = 535
    _BASE_HEIGHT_COMPACT = 260

    def _target_height(self) -> int:
        return self._BASE_HEIGHT_COMPACT if self._compact_mode else self._BASE_HEIGHT_FULL

    def _toggle_compact_mode(self):
        self._compact_mode = not self._compact_mode
        if hasattr(self, "tray_compact_action"):
            self.tray_compact_action.setChecked(self._compact_mode)

        for w in self._full_mode_widgets:
            w.setVisible(not self._compact_mode)
        self.resize(220 if self._compact_mode else 300, self._target_height())

    # ------------------------------------------------------------
    # Update tampilan tiap tick berdasarkan data dari simulator/sumber asli
    # ------------------------------------------------------------
    def _tick(self):
        self._update_weekly_section()

        state = self.source.fetch()

        if state.status != "ok":
            self._render_non_ok_state(state)
            return

        active_family = family_key_for_model(state.model)
        # display_family = model yang lagi ditampilkan di ring: kalau user
        # nge-klik/pin salah satu badge, ring ikut badge itu terus sampai
        # di-klik lagi (unpin) -> balik auto-ikut model yang lagi dipakai.
        display_family = self._selected_family or active_family

        context_by_family = state.context_by_family or {}
        model_id_by_family = state.model_id_by_family or {}
        if display_family == active_family:
            display_model_id = state.model
            display_context_tokens = state.context_tokens
            display_context_window = state.context_window
        else:
            display_model_id = model_id_by_family.get(display_family, "")
            display_context_tokens = context_by_family.get(display_family, 0)
            display_context_window = MODEL_CONTEXT_WINDOWS.get(display_family, DEFAULT_CONTEXT_WINDOW_TOKENS)

        used_pct = (display_context_tokens / display_context_window) * 100 if display_context_window else 0
        arc_pct = min(100.0, used_pct)  # arc-nya mentok penuh di 100%, tapi angkanya tetap tampil apa adanya
        start_hex, end_hex = gradient_for_used_pct(used_pct)
        center_color = "#fca5a5" if used_pct >= CRITICAL_USED_PCT else "#f5f5f7"
        self.ring.set_value(
            arc_pct, start_hex, end_hex,
            center_text=f"{int(used_pct)}%", center_color_hex=center_color,
        )

        usage_by_family = state.model_usage or {}
        for family, badge in self.model_badges.items():
            color = MODEL_FAMILY_COLORS[family]
            tokens_text = format_tokens(usage_by_family.get(family, 0))
            badge.setText(f"{family.capitalize()} · {tokens_text}")
            is_active = family == active_family
            is_displayed = family == display_family
            border_width = 2 if is_displayed else 1
            highlight_border = PIN_HIGHLIGHT_COLOR if is_displayed else None
            if is_active:
                badge.setStyleSheet(
                    f"""
                    color: #ffffff;
                    background-color: {color};
                    border: {border_width}px solid {highlight_border or color};
                    border-radius: 10px;
                    font-size: 10px;
                    font-weight: 700;
                    padding: 2px 6px;
                    """
                )
            else:
                badge.setStyleSheet(
                    f"""
                    color: #ffffff;
                    background-color: rgba{self._rgba_from_hex(color, 45)};
                    border: {border_width}px solid {highlight_border or f'rgba{self._rgba_from_hex(color, 130)}'};
                    border-radius: 10px;
                    font-size: 10px;
                    font-weight: 500;
                    padding: 2px 6px;
                    """
                )

        style = style_for_model(display_model_id) if display_model_id else {
            "label": display_family.capitalize() if display_family else "?",
        }
        pin_note = " · dipin" if self._selected_family else ""
        over_text = f" (over {int(used_pct)}%)" if used_pct > 100 else ""
        self.context_label.setText(
            f"{style['label']}{pin_note} · {format_tokens(display_context_tokens)}"
            f"/{format_tokens(display_context_window)} token context{over_text}"
        )
        self.reset_label.setText(
            f"{state.session_messages} balasan · {format_tokens(state.session_total_tokens)} token sesi ini"
        )
        self.project_label.setText(
            f'<span style="color:#a1a1aa; font-size:10px;">{state.project_name}</span>'
        )
        self.updated_label.setText(f"Update: {state.last_activity_text}")

        warnings = []
        if used_pct > 100:
            warnings.append(f"Context sudah {int(used_pct)}% dari asumsi {format_tokens(display_context_window)} — segera /clear atau chat baru")
        elif used_pct >= CRITICAL_USED_PCT:
            warnings.append(f"Context sudah {int(used_pct)}% terpakai — /compact atau chat baru disarankan")
        elif used_pct >= WARN_USED_PCT:
            warnings.append(f"Context sudah {int(used_pct)}% terpakai — mulai pertimbangkan /compact")
        if state.session_messages >= SESSION_MESSAGE_ALARM:
            warnings.append(f"Sesi sudah {state.session_messages} balasan — cukup panjang, pindah topik baru = chat baru")

        # Nudge model mix: kalau Opus mendominasi token sesi ini, saranin
        # turun ke Sonnet/Haiku buat tugas yang nggak butuh Opus.
        total_family_tokens = sum(usage_by_family.values())
        opus_tokens = usage_by_family.get("opus", 0)
        if (
            total_family_tokens > 0
            and opus_tokens >= OPUS_MIN_TOKENS_FOR_NUDGE
            and (opus_tokens / total_family_tokens) * 100 >= OPUS_SHARE_WARN_PCT
        ):
            opus_share = int((opus_tokens / total_family_tokens) * 100)
            warnings.append(
                f"Opus sudah {opus_share}% dari token sesi ini — pertimbangkan Sonnet/Haiku buat tugas ringan"
            )

        if warnings:
            self.toast.show_message(" · ".join(warnings))
        else:
            self.toast.clear_message()

    def _on_model_badge_clicked(self, family: str):
        """Klik badge model -> pin ring ke model itu. Klik badge yang sama
        lagi -> unpin, ring balik auto-ikut model yang lagi aktif dipakai."""
        self._selected_family = None if self._selected_family == family else family
        self._tick()  # refresh langsung, nggak nunggu timer 2 detik

    @staticmethod
    def _rgba_from_hex(hex_color: str, alpha: int) -> str:
        """"#3b82f6", 30 -> "(59, 130, 246, 30)" buat dipakai di rgba(...) QSS."""
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"({r}, {g}, {b}, {alpha})"

    def _update_weekly_section(self):
        """Update baris "Sesi (5 jam)" & "Mingguan" dari `usage_data.json`
        (hasil webview_usage_source.py). Independen dari data context lokal
        -- jalan/gagal sendiri-sendiri."""
        w = self.weekly_source.fetch()

        if w.status != "ok":
            messages = {
                "loading": "Kuota resmi: menghubungkan...",
                "waiting_login": "Kuota resmi: login manual dulu di jendela browser yang terbuka",
                "session_expired": "Kuota resmi: sesi login habis, minta login ulang",
                "error": f"Kuota resmi: error ({w.detail})" if w.detail else "Kuota resmi: error",
                "stale": "Kuota resmi: data lama, webview source mungkin berhenti",
            }
            self.plan_status_label.setText(messages.get(w.status, w.status))
            for text_label, bar, name in (
                (self.session_quota_text, self.session_quota_bar, "Sesi (5 jam)"),
                (self.weekly_quota_text, self.weekly_quota_bar, "Mingguan (semua model)"),
            ):
                text_label.setText(name)
                bar.setValue(0)
                self._style_quota_bar(bar, "#3f3f46")
            for text_label, bar in self.model_quota_rows:
                text_label.setVisible(False)
                bar.setVisible(False)
            return

        org = self.account_info.get("org", "")
        plan_bits = [b for b in (w.plan_name, org) if b]
        self.plan_status_label.setText(" · ".join(plan_bits) if plan_bits else "Plan Claude")

        self._set_quota_row(
            self.session_quota_text, self.session_quota_bar,
            "Sesi (5 jam)", w.session_pct_used, w.session_reset_text,
        )

        weekly_all = next((x for x in (w.weekly or []) if x.name.lower().startswith("all")), None)
        if weekly_all:
            self._set_quota_row(
                self.weekly_quota_text, self.weekly_quota_bar,
                weekly_all.name, weekly_all.pct_used, weekly_all.reset_text,
            )
        else:
            self.weekly_quota_text.setText("Mingguan — tidak ada data")
            self.weekly_quota_bar.setValue(0)

        per_model = [x for x in (w.weekly or []) if x is not weekly_all]
        for i, (text_label, bar) in enumerate(self.model_quota_rows):
            if i < len(per_model):
                item = per_model[i]
                text_label.setVisible(True)
                bar.setVisible(True)
                self._set_quota_row(text_label, bar, item.name, item.pct_used, item.reset_text)
            else:
                text_label.setVisible(False)
                bar.setVisible(False)

    def _render_non_ok_state(self, state: UsageState):
        """Tampilkan status non-normal (loading/no_session/error) di ring & label."""
        self.ring.set_value(0.0, "#3f3f46", "#27272a")
        for family, badge in self.model_badges.items():
            badge.setText(family.capitalize())
            badge.setStyleSheet(
                "color: #71717a; background: transparent; border: 1px solid #3f3f46;"
                "border-radius: 10px; font-size: 10px; padding: 2px 6px;"
            )
        self.context_label.setText("")

        messages = {
            "loading": "Membaca log Claude Code...",
            "no_session": state.detail or "Belum ada sesi Claude Code ditemukan",
            "error": f"Error: {state.detail}" if state.detail else "Terjadi error",
        }
        self.reset_label.setText(messages.get(state.status, state.status))
        self.project_label.setText("")
        self.updated_label.setText("")
        self.toast.clear_message()

    # ------------------------------------------------------------
    # Drag window: klik-tahan di body widget lalu geser
    # ------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()


def main():
    ensure_webview_source_running()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # widget hide ke tray, bukan quit
    widget = UsageWidget()
    widget.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()