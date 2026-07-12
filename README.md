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

---

## Persyaratan

- **Windows 10/11** (widget ini spesifik Windows — pakai WebView2 & system tray Windows)
- **Python 3.9+** — cek dengan `python --version`
- **Claude Code** sudah pernah dipakai minimal sekali (buat data lokal/context ring bisa muncul — kalau belum pernah, ring-nya akan nunjukin "Belum ada sesi ditemukan" sampai kamu mulai chat)
- **WebView2 Runtime** — biasanya sudah otomatis terpasang di Windows 10/11 modern. Kalau belum ada, download dari [developer.microsoft.com/microsoft-edge/webview2](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)
- Akun **Claude.ai** (Free/Pro/Max) buat bagian kuota resmi (opsional — widget tetap jalan tanpa ini, cuma bagian kuota resmi yang kosong)

## Instalasi

```bash
git clone https://github.com/abukhalid-io/claude-usage-widget.git
cd claude-usage-widget
pip install -r requirements.txt
python claude_usage_widget.py
```

## Cara Pakai (Step by Step)

1. **Jalankan widget** — `python claude_usage_widget.py`. Widget muncul di pojok kanan atas layar.
2. **Ring context window** langsung aktif kalau kamu sudah pernah pakai Claude Code — nggak perlu setup apa-apa.
3. **Login buat kuota resmi (sekali saja)** — begitu widget dibuka, sebuah jendela browser kecil ("Login Claude") juga otomatis terbuka di belakang layar. Login ke akun Claude.ai kamu di situ:
   - Kalau akun kamu punya opsi email+password, langsung login seperti biasa.
   - **Kalau akun kamu cuma bisa login lewat Google** — tombol "Continue with Google" **tidak akan berfungsi** di jendela ini (Google sengaja memblokir login dari browser tertanam/embedded, ini kebijakan keamanan Google sendiri, bukan bug widget). Pakai tombol **"Continue with email"** sebagai gantinya, lalu masukkan kode OTP yang dikirim ke email kamu.
4. Setelah login berhasil, jendela itu **otomatis tersembunyi** dan tidak akan muncul lagi selama sesi login masih valid (biasanya bertahan lama). Bar "Sesi (5 jam)" dan "Mingguan" di widget akan langsung terisi data asli dari akun kamu.
5. Widget sekarang jalan sendiri di background, update terus — cukup dibiarkan terbuka (atau minimize ke tray).

### Kalau sesi login habis

Kadang-kadang sesi login expired (biasanya setelah beberapa minggu). Widget akan otomatis mendeteksi ini, menampilkan status "sesi login habis" di bagian kuota resmi, dan jendela browser kecil itu **muncul lagi sendiri** minta kamu login ulang — tidak perlu restart widget.

### Auto-start saat Windows menyala (opsional)

Biar widget otomatis jalan tiap kali login Windows, tanpa jendela console yang muncul:

1. Tekan `Win + R`, ketik `shell:startup`, Enter
2. Buat shortcut baru di folder itu yang mengarah ke `pythonw.exe` (bukan `python.exe`, biar tanpa console) dengan argumen path ke `claude_usage_widget.py`
   - Contoh target shortcut: `"C:\Path\ke\Python\pythonw.exe" "C:\Path\ke\claude-usage-widget\claude_usage_widget.py"`

## Kontrol

| Aksi | Fungsi |
|---|---|
| Klik-tahan body widget | Geser posisi widget |
| Tombol `×` | Minimize ke system tray (bukan quit) |
| Klik kanan ikon tray | Menu Show/Hide, Refresh sekarang, Compact mode, Quit |
| Tombol `▭` di header | Toggle compact mode (ringkas, cuma ring) |
| Klik badge model (Opus/Sonnet/Haiku/Fable) | "Pin" ring ke model itu — klik lagi buat lepas, ring balik auto-ikut model yang lagi aktif |

## Memahami Tampilan

- **Ring besar (context window)** — seberapa penuh percakapan yang SEDANG kamu buka sekarang (context lokal, dari log Claude Code). Ini beda dari kuota di bawahnya!
- **Badge Opus/Sonnet/Haiku/Fable** — total token yang dipakai tiap model di sesi/chat yang sedang aktif.
- **Bar "Sesi (5 jam)" & "Mingguan"** — kuota resmi dari akun Claude.ai kamu (rate limit langganan Pro/Max), independen dari chat mana yang lagi dibuka.
- **Toast peringatan kuning** — muncul otomatis saat context window hampir penuh atau saat Opus mendominasi pemakaian token sesi.

## Troubleshooting

| Masalah | Solusi |
|---|---|
| Ring nunjukin "Belum ada sesi ditemukan" | Belum pernah pakai Claude Code di komputer ini — buka Claude Code, chat sekali, lalu cek lagi |
| "Continue with Google" error / gagal login | Pakai "Continue with email" di jendela login (lihat bagian Cara Pakai) |
| Bar kuota resmi kosong terus / "tidak ketemu" | Jendela login mungkin tertutup sebelum selesai — jalankan ulang widget, biarkan jendela login terbuka sampai selesai |
| Widget hilang setelah diklik `×` | Itu memang minimize ke tray, bukan tertutup — cek ikon di system tray (area jam Windows) |
| Widget dobel/nggak sinkron | Jangan jalankan `python claude_usage_widget.py` dua kali bersamaan |

## Privasi

Sesi login (`webview_profile/`), data usage (`usage_data.json`), dan log debug tidak pernah di-commit ke repo ini (lihat `.gitignore`) — semuanya cuma tersimpan lokal di komputer kamu sendiri. Widget hanya membaca data lokal milikmu dan halaman usage resmi Claude.ai — tidak ada data yang dikirim ke server pihak ketiga mana pun.
