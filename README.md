# Live — Pengunduh Playlist YouTube (`yt-dlp`) & Unggah Otomatis per Video ke GitHub

Repositori ini berisi skrip dan workflow **GitHub Actions** untuk mengunduh playlist/video YouTube menggunakan `yt-dlp` dan **langsung mengunggah ke GitHub setiap kali 1 video selesai diunduh**.

## Cara Kerja

1. Mengambil daftar video secara bertahap (`lazy_playlist`) dari playlist target:
   `https://www.youtube.com/playlist?list=PLX15XB_3XZuR7NjjcnxLr770zy4PLWut_`
2. Setiap **1 video** selesai diunduh dan digabung (`after_move`):
   - **Mode `release` (Default & Direkomendasikan)**: Mengunggah file langsung ke **GitHub Releases** (mendukung hingga ~2 GB per file; jika ada video *live stream* berukuran >1,9 GB, otomatis dipecah tanpa *re-encode* menggunakan `ffmpeg` atau per bagian biner agar tidak ditolak GitHub).
   - **Mode `repo` (Opsional)**: Menyimpan video berukuran kecil (<95 MB) ke folder `videos/` di dalam repositori Git (otomatis dialihkan ke GitHub Releases apabila ukuran video >95 MB agar `git push` tidak gagal karena batas 100 MB GitHub).
3. Mencatat ID video ke `archive.txt` dan tautan unduhan ke `VIDEOS.md` di GitHub tepat setelah setiap video berhasil diunggah, sehingga:
   - Jika proses berhenti di tengah jalan, saat dijalankan lagi **otomatis melanjutkan dari video berikutnya** tanpa mengunduh ulang.
4. Menghapus file video sementara di penyimpanan lokal setelah berhasil terunggah agar disk tidak penuh.

---

## Cara 1: Jalankan Langsung di GitHub Actions (Tanpa VPS / Tanpa Token Pribadi)

GitHub Actions otomatis menggunakan `GITHUB_TOKEN` bawaan repositori (tidak perlu membuat token `ghp_...`):

1. Gabungkan (*merge*) branch/PR ini ke branch `main`.
2. Buka tab **Actions** di repositori `Neldah-3/Live`.
3. Pilih workflow **Download YouTube Playlist & Upload per Video**.
4. Klik tombol **Run workflow**, atur opsi bila perlu (URL playlist sudah terisi otomatis), lalu klik **Run workflow**.
5. Hasil unggahan akan muncul satu per satu di halaman **Releases** dan daftar lengkapnya tercatat otomatis di `VIDEOS.md`.

> **Tips jika YouTube meminta verifikasi bot pada server GitHub (`Sign in to confirm you're not a bot`):**
> Ekspor cookies YouTube format Netscape (`cookies.txt`), lalu simpan isinya ke **Settings → Secrets and variables → Actions → New repository secret** dengan nama `YOUTUBE_COOKIES`. Workflow akan otomatis menggunakannya dengan aman.

---

## Cara 2: Jalankan di PC / VPS / WSL / Termux / Google Colab

Pastikan `python3` (dan disarankan `ffmpeg`) sudah terpasang, lalu jalankan:

```bash
git clone https://github.com/Neldah-3/Live.git
cd Live

# Set token melalui environment variable (JANGAN simpan token di dalam file!)
export GITHUB_TOKEN="token_github_baru_anda"

# Jalankan (otomatis mengunduh playlist default & mengunggah tiap selesai 1 video)
./run.sh
```

### Contoh Opsi Tambahan

```bash
# Pilih resolusi maksimum 720p agar ukuran lebih hemat
./run.sh --quality 720p

# Gunakan cookies langsung dari browser lokal (Chrome/Firefox/Brave/Edge)
./run.sh --cookies-from-browser chrome

# Batasi hanya mengunduh 5 video terlebih dahulu
./run.sh --max-downloads 5

# Simpan video kecil (<95 MB) langsung ke folder videos/ di branch Git
./run.sh --mode repo
```

---

## Keamanan Token GitHub

- **Jangan pernah** menaruh Personal Access Token (`ghp_...`) di dalam file skrip, commit Git, atau chat terbuka — terutama karena repositori ini bersifat **publik**.
- Jika token pernah terekspos, segera hapus/cabut (*revoke*) melalui **GitHub Settings → Developer settings → Personal access tokens**, lalu buat token baru bila diperlukan (dengan izin `repo` atau Fine-grained token dengan izin **Contents: Read and write** pada repo `Neldah-3/Live`).
