# Zagent V3 — Panduan Source Code, Pengembangan, Build, dan Rollback

Repository **source-only** untuk Zagent V3 (Zagent Master/User). Dokumentasi ini menjelaskan cara bekerja pada kode sumber, menguji perubahan, menyiapkan bundle, dan mengembalikan perubahan melalui Git tanpa membuat folder backup source berulang.

> **Kebijakan:** Git menyimpan kode, manifest, skrip, tes, dan dokumentasi. File `.exe`, installer, runtime terunduh, cache, log, file lokal rahasia, hasil publish/staging, dan backup **tidak** disimpan di Git. Repository ini bukan paket installer siap pakai.

## 1. Lokasi dan komponen

Source lokal pengembangan saat panduan ini dibuat:

~~~text
D:\3-Ikbalzohr\Agent\appBridge\production\Zagent\v3
~~~

Remote `origin`: https://github.com/ikbalzohr/phyton_zagent_ai_manager.git

Branch utama: `main`.

| Lokasi | Fungsi |
| --- | --- |
| `src/` | .NET Core, Manager, Setup, Bundle, Shared |
| `agents/files/engine/` | Agent Files (Python) |
| `agents/web/engine/` | Agent Web dan Chrome Bridge (Node.js/TypeScript) |
| `agents/tunnel/engine/` | Tunnel router, sesi, identitas, dan akses (Python) |
| `agents/arcmap/engine/` | Agent ArcMap, MCP, dan orkestrator (Python) |
| `agents/arcmap/addin/` | Source ArcMap add-in |
| `schemas/` | Skema dan kontrak agent |
| `tests/` dan `agents/*/engine/tests/` | Pengujian |
| `dev/` | Skrip pengembangan lokal |
| `packaging/scripts/` | Skrip build, publish, staging, dan bundle |
| `docs/` | Arsitektur, keputusan teknis, dan panduan detail |
| `component-manifest.json` | Referensi versi/komponen (bukan kredensial runtime) |

Zagent **Master** ditujukan untuk operator Ikbal, sedangkan **User** untuk operator Harry. Pemisahan lokasi instalasi, identitas, dan alokasi port mengacu pada [panduan Master/User](docs/ZAGENT_V3_MASTER_USER_INSTALLATION_20261008.md).

## 2. Prasyarat lingkungan

- **Windows** dengan PowerShell untuk skrip `.ps1`.
- **.NET SDK 10.0.401**, sesuai `global.json`. Sejumlah skrip pengembang **masih menggunakan path absolut** `D:\3-Ikbalzohr\Agent\appBridge\toolchain\dotnet10\dotnet.exe`; sesuaikan skrip jika workstation berbeda.
- **Node.js** kompatibel dengan `agents/web/engine/package.json` (rentang `>=20.18.0 <27`; lingkungan ini menggunakan Node 24.19.0), beserta npm.
- **Python 3.11** untuk MCP/agent modern. Jangan mengandalkan Python global 2.7. ArcMap Desktop 10.6.1 dan Python 2.7 hanya untuk komponen desktop ArcMap eksternal.
- Toolchain, dependensi npm/Python, browser runtime, SDK, dan payload pendukung **harus disiapkan terpisah** karena bukan bagian dari Git. Periksa [persyaratan toolchain](docs/TOOLCHAIN_REQUIREMENTS.md) dan skrip `packaging/scripts/Build-PortableRuntimes.ps1`.

**Catatan reproduksibilitas:** README dan skrip build tidak menjamin hasil build identik di mesin baru tanpa seluruh versi dependency dan input eksternal yang sesuai. Sebagian skrip menggunakan path developer lokal. Verifikasi prasyarat sebelum melakukan build rilis.

## 3. Mulai bekerja

Buka PowerShell lalu:

~~~powershell
Set-Location 'D:\3-Ikbalzohr\Agent\appBridge\production\Zagent\v3'
git status
git branch --show-current
git log --oneline -5
~~~

Untuk mendapatkan pembaruan pada workstation lain (hanya saat working tree bersih):

~~~powershell
git pull --ff-only origin main
~~~

Jangan menjalankan `git reset --hard`, `git clean -fdx`, atau checkout paksa sebelum memastikan tidak ada pekerjaan lokal yang perlu disimpan. Git hanya melacak file yang tidak dikecualikan `.gitignore`.

## 4. Alur pengembangan yang disarankan

1. Periksa `git status` dan commit aktif sebelum mengubah kode.
2. Kerjakan perubahan **kecil dan terfokus** pada source, tanpa memodifikasi instalasi Production secara langsung.
3. Jalankan build Debug dan pengujian yang relevan. Gunakan DEV untuk pengujian langsung hanya ketika memang diperlukan.
4. Tinjau `git diff`, pastikan tidak ada token/API key atau output build yang ikut.
5. Buat commit setelah validasi lulus; push ke `origin/main` setelah perubahan siap.
6. Jika fitur gagal, rollback melalui commit Git (lihat bagian 7); **jangan membuat direktori `backup` atau salinan source V3 setiap revisi**.

Contoh commit:

~~~powershell
git status
git diff
git add -- 'src/Zagent.Core/Program.cs'  # contoh: ganti sesuai file yang diubah
git diff --cached --check
git diff --cached --stat
git commit -m "fix: ringkasan perubahan dan hasil validasi"
git push origin main
~~~

Pastikan hasil pengujian dicatat dalam commit atau dokumentasi bila relevan. Gunakan nama commit yang menjelaskan bagian yang diperbaiki dan alasan perubahannya.

## 5. Build dan pengujian DEV

Dari root V3, build .NET tanpa komponen Web:

~~~powershell
& .\dev\Build-ZagentDev.ps1 -SkipWeb
~~~

Jika dependensi Web terpasang dan ingin build keseluruhan, jalankan `& .\dev\Build-ZagentDev.ps1`. Pengujian Web dapat dijalankan dari modulnya:

~~~powershell
Push-Location .\agents\web\engine
npm ci
npm run typecheck
npm run regression:test
Pop-Location
~~~

Contoh regression gate source yang disediakan:

~~~powershell
& .\packaging\scripts\Test-KnownBugRegressions.ps1
~~~

Untuk pengujian proses DEV secara **sengaja/terkontrol**, gunakan:

~~~powershell
& .\dev\Start-Zagent-Dev.ps1
& .\dev\Status-Zagent-Dev.ps1
& .\dev\Stop-Zagent-Dev.ps1
~~~

- `Start-Zagent-Dev.ps1 -NoBuild` dapat digunakan hanya jika build sebelumnya sudah ada dan sesuai dengan source aktif.
- Jangan menjalankan/menutup Manager dan ArcMap tanpa tujuan pengujian yang jelas. Periksa kepemilikan sesi dan proses; jangan sampai mematikan Production.
- DEV menggunakan port Tunnel 19010/19011, Files 19020, Web 19030, dan ArcMap 19040 sesuai skrip launcher. Untuk perbedaan antara Master/User dan DEV, lihat panduan instalasi Master/User.
- Aturan plugin: Zagent Production memerlukan approval dan binding operator-perangkat yang cocok; Zagent DEV menggunakan grant dari Production. **Try Plugin digunakan dalam sesi chat terpisah**, tidak digabung dalam sesi Production/DEV. Lihat [panduan otorisasi](dev/GUIDE-AUTH-CREDENTIALS.md).

## 6. Build dan bundle rilis

**Tidak ada installer `.exe` dalam riwayat Git.** Git menyimpan *cara membangunnya* melalui skrip yang tersedia. Hasil build akan dibuat pada direktori yang dikecualikan `.gitignore`.

Alur packaging yang perlu dipenuhi:

1. Pastikan .NET, Python, Node, dependency/cache eksternal, dan browser runtime telah siap.
2. Jalankan regression gate yang relevan serta pengujian sebelum rilis.
3. Siapkan `packaging/runtime/` melalui `packaging/scripts/Build-PortableRuntimes.ps1` dan input eksternal yang disyaratkan skrip; pastikan juga runtime browser Playwright yang diperlukan sudah tersedia.
4. Publish .NET Core, Manager, Setup melalui `Publish-Core.ps1`, `Publish-Manager.ps1`, dan `Publish-Setup.ps1`. **Bundle stub** `packaging/publish/bundle/Zagent.Bundle.exe` juga harus dibangun dari `src/Zagent.Bundle/` (skrip bundle memerlukannya).
5. Jalankan `Build-ProductionPayload.ps1 -OutputRoot <direktori-staging-baru>`. Skrip ini **menolak overwrite** direktori staging lama.
6. Jalankan `Build-SingleExeBundle.ps1 -PayloadRoot <direktori-staging> -OutputFile <file-exe-baru>` setelah manifest payload tervalidasi. Skrip juga **menolak overwrite** output yang sudah ada.
7. Verifikasi bundle pada lingkungan pengujian sebelum promosi ke instalasi Production.

Contoh tahap akhir **setelah seluruh input build sudah lengkap** (pilih nama output baru setiap rilis):

~~~powershell
$release = 'Zagent-0.9.0-local1'
$stage = Join-Path (Get-Location) "packaging\staging\$release"
$installer = Join-Path (Get-Location) "packaging\bundle\$release.exe"

& .\packaging\scripts\Build-ProductionPayload.ps1 -OutputRoot $stage
& .\packaging\scripts\Build-SingleExeBundle.ps1 -PayloadRoot $stage -OutputFile $installer
~~~

Perintah di atas **bukan** bootstrap penuh dari clone baru: tahap publish, Bundle stub, runtime, dan dependency harus diselesaikan lebih dahulu. Tidak boleh mengasumsikan keberhasilan rilis hanya karena perintah bundle selesai.

## 7. Rollback dengan Git (tanpa folder backup source)

Sebelum rollback, pastikan pekerjaan lokal sudah di-commit dan proses DEV yang menggunakan kode tersebut dihentikan secara aman.

**Lihat riwayat dan perbedaan:**

~~~powershell
git log --oneline --decorate -15
$oldCommit = 'ISI_HASH_COMMIT'
git diff "$oldCommit..HEAD"
~~~

**Batalkan satu commit buruk sambil mempertahankan riwayat** (cara yang paling aman pada branch bersama `main`):

~~~powershell
$badCommit = 'ISI_HASH_COMMIT'
git revert $badCommit
git push origin main
~~~

**Periksa versi lama sementara tanpa mengubah `main`:**

~~~powershell
$oldCommit = 'ISI_HASH_COMMIT'
git switch --detach $oldCommit
# Inspeksi / build / test versi tersebut jika perlu.
git switch main
~~~

Mode *detached HEAD* bukan tempat untuk commit perubahan permanen. Jangan memaksakan penghapusan perubahan lokal untuk berpindah commit.

**Kembalikan beberapa file ke versi commit tertentu**, lalu commit sebagai perbaikan baru:

~~~powershell
$oldCommit = 'ISI_HASH_COMMIT'
git restore "--source=$oldCommit" --staged --worktree -- 'src/Zagent.Core/Program.cs'
git diff --cached
git commit -m "revert: pulihkan file dari baseline terverifikasi"
git push origin main
~~~

Penting: rollback Git mengembalikan **source yang dilacak** saja, **bukan** file hasil publish, runtime, installer, data pengguna, atau instalasi Production. Setelah rollback source, lakukan rebuild + tes dan deployment terkontrol jika diperlukan.

## 8. Kebijakan file dan keamanan

- **Masuk Git:** source (`.cs`, `.py`, `.ts`, dll.), `.csproj`, solusi `.slnx`, manifest/lockfile yang aman, skrip build, unit test, schema, dan dokumentasi.
- **Tidak masuk Git:** `*.exe`, installer, ZIP bundle, direktori `packaging/runtime/`, `packaging/staging/`, `packaging/publish/`, `node_modules/`, `bin/`, `obj/`, `__pycache__/`, backup, cache, log, dan file sementara.
- **Jangan commit rahasia:** API key, Tunnel ID yang sensitif, token, kredensial, DPAPI vault, device identity, pairing Chrome, dan konfigurasi mesin pribadi. Gunakan penyimpanan runtime di luar source.
- Periksa `.gitignore` sebelum `git add`. File yang sudah terlanjur terlacak **tidak otomatis hilang** hanya karena masuk `.gitignore`; perlu dikeluarkan dari index secara eksplisit.
- Git commit adalah **checkpoint kode**, bukan pengganti backup data pengguna. Strategi rollback komponen terpasang dibahas dalam [ADR-006](docs/adr/ADR-006-update-and-rollback.md).

## 9. Referensi lanjutan

- [Workflow DEV](dev/README.md)
- [Otorisasi dan kredensial DEV](dev/GUIDE-AUTH-CREDENTIALS.md)
- [Isolasi instalasi Master/User](docs/ZAGENT_V3_MASTER_USER_INSTALLATION_20261008.md)
- [Development Gateway](docs/DEVELOPMENT_GATEWAY_CONTRACT.md)
- [Toolchain](docs/TOOLCHAIN_REQUIREMENTS.md)
- [Rollback dan pembaruan](docs/adr/ADR-006-update-and-rollback.md)
- [ArcMap Agent](agents/arcmap/engine/README.md)

**Prinsip operasional:** Source → build → test → commit → push; rollback → checkout/revert source → rebuild → test → deploy terkontrol. Tidak perlu membuat folder backup source untuk setiap perubahan, dan tidak perlu menyimpan installer `.exe` di Git.
