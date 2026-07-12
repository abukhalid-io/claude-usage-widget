# Claude Usage Widget

Widget desktop minimalis (Windows) untuk memantau penggunaan token Claude secara real-time, dari dua sumber data:

- **Lokal** — context window & token per model (Opus/Sonnet/Haiku/Fable), dibaca langsung dari log Claude Code (`~/.claude/projects/**/*.jsonl`), update tiap 2 detik.
- **Resmi** — kuota sesi 5 jam & mingguan (7 hari), dibaca dari `claude.ai/settings/usage` lewat jendela WebView2 asli (bukan Playwright/automation, jadi tidak diblokir Cloudflare), login manual sekali lalu berjalan sendiri di background.

## Fitur

- Ring gauge context window (warna berubah di ambang 75%/85%)
- Badge per model, bisa diklik buat "pin" ring ke model tertentu
- Bar kuota sesi & mingguan (termasuk per-model kalau ada, mis. Fable)
- Notifikasi/nudge: saran `/compact` atau chat baru saat context penuh, saran turun model kalau Opus mendominasi token sesi
- System tray (minimize, refresh, compact mode)
- Auto-start opsional lewat Windows Startup folder

## Instalasi

```bash
pip install -r requirements.txt
python claude_usage_widget.py
```

Saat pertama kali dijalankan, sebuah jendela browser kecil akan terbuka untuk login manual ke akun Claude.ai kamu (khusus untuk kuota resmi). Login sekali, sesi tersimpan lokal di `webview_profile/` untuk run berikutnya.

> Catatan: kalau akun kamu hanya bisa login lewat Google, gunakan opsi **"Continue with email"** di jendela login itu — Google memblokir OAuth login dari embedded webview manapun (kebijakan keamanan mereka sendiri, bukan batasan widget ini).

## Kontrol

- Klik-tahan body widget → geser posisi
- Tombol `×` → minimize ke system tray (klik kanan ikon tray untuk Show/Refresh/Compact/Quit)
- Tombol `▭` → compact mode (ringkas, cuma ring)
- Klik badge model → pin ring ke model itu, klik lagi untuk lepas

## Privasi

Sesi login (`webview_profile/`), data usage (`usage_data.json`), dan log debug tidak pernah di-commit ke repo ini (lihat `.gitignore`). Widget hanya membaca data lokal milikmu sendiri dan halaman usage resmi Claude.ai — tidak ada data yang dikirim ke server pihak ketiga.
