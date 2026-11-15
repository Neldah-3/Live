#!/usr/bin/env python3
"""
Unduh playlist/video YouTube menggunakan yt-dlp dan langsung unggah ke GitHub
setiap kali 1 video selesai diunduh.

Fitur utama:
- Mengunggah 1 per 1 tepat setelah tiap video selesai diunduh & digabung (after_move).
- Mode 'release' (default): unggah ke GitHub Releases (mendukung hingga ~2 GB per file;
  otomatis memecah video >1.9 GB menjadi beberapa bagian tanpa re-encode jika perlu).
- Mode 'repo': simpan ke folder videos/ di branch Git (untuk video <95 MB; otomatis
  dialihkan ke GitHub Releases bila ukuran >95 MB agar git push tidak ditolak GitHub).
- Menyinkronkan archive.txt & VIDEOS.md ke GitHub setiap selesai 1 video sehingga proses
  bisa dilanjutkan (resume) kapan saja tanpa mengunduh ulang video yang sudah terunggah.
- Menghapus file sementara lokal setelah unggah berhasil agar hemat ruang penyimpanan.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests
import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import MaxDownloadsReached, PostProcessingError

DEFAULT_PLAYLIST_URL = (
    "https://www.youtube.com/playlist?list=PLX15XB_3XZuR7NjjcnxLr770zy4PLWut_"
)
DEFAULT_REPO = "Neldah-3/Live"

# Batas aman di bawah batas keras GitHub Releases (2 GiB = 2048 MiB)
MAX_RELEASE_ASSET_BYTES = 1900 * 1024 * 1024  # 1900 MiB
# Batas aman di bawah batas keras file Git GitHub (100 MiB)
MAX_GIT_REPO_FILE_BYTES = 95 * 1024 * 1024  # 95 MiB
# Batas aman GitHub Contents REST API (payload base64 <= 100 MB)
MAX_CONTENTS_API_BYTES = 45 * 1024 * 1024  # 45 MiB

VIDEOS_MD_HEADER = (
    "# Daftar Video yang Telah Diunggah\n\n"
    "| No | Judul | Video ID | Ukuran | Waktu Unggah (UTC) | Unduh |\n"
    "|---:|-------|----------|-------:|--------------------|-------|\n"
)


def log(msg: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{num_bytes} B"


def normalize_youtube_url(raw_url: str) -> str:
    """Bersihkan escape Markdown/HTML yang sering ikut tersalin pada URL."""
    cleaned = html.unescape(raw_url.strip())
    for escaped, plain in (
        (r"\_", "_"),
        (r"\&", "&"),
        (r"\?", "?"),
        (r"\=", "="),
        (r"\-", "-"),
    ):
        cleaned = cleaned.replace(escaped, plain)
    return cleaned


def extract_playlist_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    plist = qs.get("list")
    if plist and plist[0]:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", plist[0]).strip("-")
        return safe or None
    return None


def sanitize_slug(text: str, max_len: int = 80) -> str:
    """Hasilkan nama aman ASCII [A-Za-z0-9_-] agar GitHub Release tidak mengubah nama aset."""
    ascii_text = text.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-")
    if not slug:
        slug = "video"
    return slug[:max_len].rstrip("-") or "video"


def build_asset_filename(info: dict[str, Any], filepath: Path) -> str:
    ext = filepath.suffix.lstrip(".") or str(info.get("ext") or "mp4")
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext) or "mp4"

    vid_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(info.get("id") or "unknown")) or "unknown"
    title_slug = sanitize_slug(str(info.get("title") or "video"), max_len=80)

    idx = info.get("playlist_index") or info.get("playlist_autonumber")
    if isinstance(idx, int) and idx > 0:
        return f"{idx:03d}_{title_slug}_{vid_id}.{ext}"
    return f"{title_slug}_{vid_id}.{ext}"


def resolve_github_token(cli_token: str | None = None) -> str:
    for candidate in (
        cli_token,
        os.environ.get("GITHUB_TOKEN"),
        os.environ.get("GH_TOKEN"),
    ):
        if candidate and candidate.strip():
            return candidate.strip()

    if shutil.which("gh"):
        try:
            proc = subprocess.run(
                ["gh", "auth", "token"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            tok = proc.stdout.strip()
            if tok:
                return tok
        except Exception:
            pass

    raise RuntimeError(
        "Token GitHub tidak ditemukan.\n"
        "Set environment variable terlebih dahulu (jangan simpan token di kode):\n"
        '  export GITHUB_TOKEN="token_github_anda"\n'
        "atau login menggunakan GitHub CLI: gh auth login"
    )


def detect_repo_slug(cli_repo: str | None = None) -> str:
    if cli_repo and "/" in cli_repo.strip():
        return cli_repo.strip()

    env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in env_repo:
        return env_repo

    if shutil.which("git"):
        try:
            proc = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            remote = proc.stdout.strip()
            m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", remote)
            if m:
                return m.group(1)
        except Exception:
            pass

    return DEFAULT_REPO


class ProgressFileReader:
    """Pembaca streaming dari disk dengan laporan progres tanpa memuat seluruh file ke RAM."""

    def __init__(self, fp: Any, total_bytes: int, label: str) -> None:
        self.fp = fp
        self.total = total_bytes
        self.label = label
        self.bytes_read = 0
        self.start_time = time.monotonic()
        self.last_log_time = self.start_time
        self.last_pct_bucket = -1

    def __len__(self) -> int:
        return self.total

    def read(self, size: int = -1) -> bytes:
        chunk = self.fp.read(size)
        if chunk:
            self.bytes_read += len(chunk)
            now = time.monotonic()
            pct = (self.bytes_read / self.total * 100.0) if self.total > 0 else 100.0
            bucket = int(pct // 10)
            if (
                bucket > self.last_pct_bucket
                or (now - self.last_log_time) >= 5.0
                or self.bytes_read == self.total
            ):
                elapsed = max(0.001, now - self.start_time)
                speed = self.bytes_read / elapsed
                log(
                    f"  Unggah {self.label}: {pct:5.1f}% "
                    f"({format_bytes(self.bytes_read)} / {format_bytes(self.total)}) "
                    f"@ {format_bytes(int(speed))}/s"
                )
                self.last_pct_bucket = bucket
                self.last_log_time = now
        return chunk


class GitHubClient:
    def __init__(
        self,
        repo: str,
        token: str,
        api_base: str = "https://api.github.com",
        uploads_base: str = "https://uploads.github.com",
    ) -> None:
        if "/" not in repo:
            raise ValueError(f"Format repository harus 'owner/repo', diterima: {repo!r}")
        self.repo = repo
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.uploads_base = uploads_base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Live-yt-dlp-uploader/1.0",
            }
        )

    def _request(self, method: str, url: str, retries: int = 3, **kwargs: Any) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.request(method, url, timeout=kwargs.pop("timeout", 60), **kwargs)
                if resp.status_code in (500, 502, 503, 504) and attempt < retries:
                    wait = 2**attempt
                    log(f"GitHub API HTTP {resp.status_code}, coba ulang dalam {wait}d...")
                    time.sleep(wait)
                    continue
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < retries:
                    wait = 2**attempt
                    log(f"Koneksi GitHub gagal ({exc}), coba ulang dalam {wait}d...")
                    time.sleep(wait)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def verify_access(self) -> str:
        """Pastikan repository & izin tulis valid sebelum mulai mengunduh video."""
        url = f"{self.api_base}/repos/{self.repo}"
        resp = self._request("GET", url)
        if resp.status_code == 401:
            raise RuntimeError("Autentikasi GitHub gagal (401 Bad credentials). Periksa GITHUB_TOKEN Anda.")
        if resp.status_code == 404:
            raise RuntimeError(
                f"Repository '{self.repo}' tidak ditemukan atau token tidak memiliki akses (404)."
            )
        resp.raise_for_status()
        data = resp.json()
        perms = data.get("permissions")
        if isinstance(perms, dict) and perms.get("push") is False:
            raise RuntimeError(
                f"Token GitHub tidak memiliki izin tulis (push/contents:write) ke '{self.repo}'."
            )
        return str(data.get("default_branch") or "main")

    def ensure_release(self, tag_name: str, title: str, body: str) -> dict[str, Any]:
        tag_url = f"{self.api_base}/repos/{self.repo}/releases/tags/{urllib.parse.quote(tag_name)}"
        resp = self._request("GET", tag_url)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code != 404:
            resp.raise_for_status()

        create_url = f"{self.api_base}/repos/{self.repo}/releases"
        payload = {
            "tag_name": tag_name,
            "name": title,
            "body": body,
            "draft": False,
            "prerelease": False,
        }
        create_resp = self._request("POST", create_url, json=payload)
        if create_resp.status_code in (200, 201):
            log(f"Membuat GitHub Release baru: {tag_name}")
            return create_resp.json()
        if create_resp.status_code == 422:
            retry_resp = self._request("GET", tag_url)
            if retry_resp.status_code == 200:
                return retry_resp.json()
        create_resp.raise_for_status()
        return create_resp.json()

    def list_release_assets(self, release_id: int) -> list[dict[str, Any]]:
        assets: list[dict[str, Any]] = []
        page = 1
        while True:
            url = f"{self.api_base}/repos/{self.repo}/releases/{release_id}/assets?per_page=100&page={page}"
            resp = self._request("GET", url)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            assets.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return assets

    def delete_release_asset(self, asset_id: int) -> None:
        url = f"{self.api_base}/repos/{self.repo}/releases/assets/{asset_id}"
        resp = self._request("DELETE", url)
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    def upload_release_asset(self, release: dict[str, Any], local_path: Path, asset_name: str) -> str:
        release_id = int(release["id"])
        file_size = local_path.stat().st_size

        existing_assets = self.list_release_assets(release_id)
        for asset in existing_assets:
            if asset.get("name") == asset_name:
                if int(asset.get("size") or -1) == file_size and asset.get("state") == "uploaded":
                    log(f"Aset {asset_name} sudah ada di GitHub Release dengan ukuran identik; melewati unggah ulang.")
                    return str(asset["browser_download_url"])
                log(f"Menghapus aset lama/parsial {asset_name} sebelum mengunggah ulang...")
                self.delete_release_asset(int(asset["id"]))

        upload_url = (
            f"{self.uploads_base}/repos/{self.repo}/releases/{release_id}/assets"
            f"?name={urllib.parse.quote(asset_name)}"
        )

        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                with local_path.open("rb") as fp:
                    reader = ProgressFileReader(fp, file_size, asset_name)
                    headers = {
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(file_size),
                    }
                    resp = self.session.post(
                        upload_url,
                        data=reader,
                        headers=headers,
                        timeout=(30, 1800),
                    )
                if resp.status_code in (200, 201):
                    return str(resp.json()["browser_download_url"])
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            except Exception as exc:
                last_err = exc
                log(f"Gagal mengunggah {asset_name} (percobaan {attempt}/3): {exc}")
                try:
                    for asset in self.list_release_assets(release_id):
                        if asset.get("name") == asset_name:
                            self.delete_release_asset(int(asset["id"]))
                except Exception:
                    pass
                if attempt < 3:
                    time.sleep(3 * attempt)

        raise RuntimeError(f"Gagal mengunggah aset {asset_name} setelah 3 percobaan: {last_err}")

    def get_text_file(self, repo_path: str, branch: str) -> tuple[str, str | None]:
        encoded_path = "/".join(urllib.parse.quote(part) for part in repo_path.strip("/").split("/"))
        url = f"{self.api_base}/repos/{self.repo}/contents/{encoded_path}?ref={urllib.parse.quote(branch)}"
        resp = self._request("GET", url)
        if resp.status_code == 404:
            return "", None
        resp.raise_for_status()
        data = resp.json()
        raw_b64 = data.get("content") or ""
        decoded = base64.b64decode(raw_b64).decode("utf-8", errors="replace")
        return decoded, str(data["sha"])

    def put_text_file(
        self,
        repo_path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> None:
        encoded_path = "/".join(urllib.parse.quote(part) for part in repo_path.strip("/").split("/"))
        url = f"{self.api_base}/repos/{self.repo}/contents/{encoded_path}"

        for attempt in range(2):
            if sha is None:
                _, sha = self.get_text_file(repo_path, branch)
            payload: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            if sha:
                payload["sha"] = sha
            resp = self._request("PUT", url, json=payload)
            if resp.status_code in (200, 201):
                return
            if resp.status_code == 409 and attempt == 0:
                sha = None
                continue
            resp.raise_for_status()

    def put_binary_file_contents_api(
        self,
        local_path: Path,
        repo_path: str,
        message: str,
        branch: str,
    ) -> str:
        encoded_path = "/".join(urllib.parse.quote(part) for part in repo_path.strip("/").split("/"))
        url = f"{self.api_base}/repos/{self.repo}/contents/{encoded_path}"

        _, existing_sha = self.get_text_file(repo_path, branch)
        raw_bytes = local_path.read_bytes()
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(raw_bytes).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha
        resp = self._request("PUT", url, json=payload, timeout=600)
        resp.raise_for_status()
        return f"https://github.com/{self.repo}/blob/{branch}/{encoded_path}?raw=1"


def split_large_file(
    filepath: Path,
    duration: float | None = None,
    max_bytes: int = MAX_RELEASE_ASSET_BYTES,
) -> list[Path]:
    """
    Jika ukuran file melebihi batas GitHub Release (~1.9 GiB), pecah otomatis menjadi
    beberapa bagian. Mencoba stream-copy ffmpeg terlebih dahulu (tanpa re-encode),
    lalu fallback ke pemecahan biner terukur jika diperlukan.
    """
    total_size = filepath.stat().st_size
    if total_size <= max_bytes:
        return [filepath]

    log(
        f"Ukuran file {format_bytes(total_size)} melebihi batas per-aset "
        f"({format_bytes(max_bytes)}). Memecah file menjadi beberapa bagian..."
    )

    stem = filepath.stem
    suffix = filepath.suffix or ".mp4"
    parent = filepath.parent

    if shutil.which("ffmpeg") and duration and duration > 0:
        est_parts = max(2, math.ceil(total_size / (max_bytes * 0.90)))
        segment_seconds = max(30, int(duration / est_parts))
        pattern = parent / f"{stem}.part%02d{suffix}"
        for old in parent.glob(f"{stem}.part*{suffix}"):
            old.unlink(missing_ok=True)

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(filepath),
            "-c",
            "copy",
            "-map",
            "0",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            str(pattern),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        parts = sorted(parent.glob(f"{stem}.part*{suffix}"))
        if proc.returncode == 0 and parts and all(0 < p.stat().st_size <= max_bytes for p in parts):
            log(f"Berhasil memecah video menjadi {len(parts)} bagian via ffmpeg.")
            return parts

        for p in parts:
            p.unlink(missing_ok=True)
        log("Pemecahan via ffmpeg tidak memenuhi batas ukuran; beralih ke pemecahan biner...")

    parts = []
    chunk_buf = 8 * 1024 * 1024  # 8 MiB
    with filepath.open("rb") as src:
        idx = 1
        while True:
            first_piece = src.read(min(chunk_buf, max_bytes))
            if not first_piece:
                break
            part_path = parent / f"{filepath.name}.part{idx:02d}"
            written = 0
            with part_path.open("wb") as dst:
                dst.write(first_piece)
                written += len(first_piece)
                while written < max_bytes:
                    to_read = min(chunk_buf, max_bytes - written)
                    buf = src.read(to_read)
                    if not buf:
                        break
                    dst.write(buf)
                    written += len(buf)
            parts.append(part_path)
            idx += 1

    log(f"Berhasil memecah file menjadi {len(parts)} bagian biner (gabungkan dengan: cat *.part* > {filepath.name}).")
    return parts


def git_commit_and_push_video(
    repo_slug: str,
    token: str,
    branch: str,
    local_video_path: Path,
    asset_name: str,
    commit_message: str,
) -> str:
    """Simpan file video (<95 MiB) ke folder videos/ dan push via git tanpa menyimpan token di .git/config."""
    repo_root = Path.cwd()
    videos_dir = repo_root / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    dest_path = videos_dir / asset_name
    if local_video_path.resolve() != dest_path.resolve():
        shutil.copy2(local_video_path, dest_path)

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as askpass:
        askpass.write(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) echo "x-access-token" ;;\n'
            '  *) echo "$GITHUB_UPLOAD_TOKEN" ;;\n'
            "esac\n"
        )
        askpass_path = askpass.name

    os.chmod(askpass_path, 0o700)
    env = os.environ.copy()
    env["GIT_ASKPASS"] = askpass_path
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GITHUB_UPLOAD_TOKEN"] = token

    remote_url = f"https://github.com/{repo_slug}.git"
    rel_dest = f"videos/{asset_name}"
    try:
        subprocess.run(
            ["git", "-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "add", "--", rel_dest],
            check=True,
            env=env,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", rel_dest],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        if status.stdout.strip():
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=github-actions[bot]",
                    "-c",
                    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                    "commit",
                    "-m",
                    commit_message,
                    "--",
                    rel_dest,
                ],
                check=True,
                env=env,
            )
            subprocess.run(
                ["git", "pull", "--rebase", remote_url, branch],
                check=False,
                env=env,
            )
            subprocess.run(
                ["git", "push", remote_url, f"HEAD:{branch}"],
                check=True,
                env=env,
            )
    finally:
        Path(askpass_path).unlink(missing_ok=True)

    return f"https://github.com/{repo_slug}/blob/{branch}/videos/{urllib.parse.quote(asset_name)}?raw=1"


class GitHubUploadPostProcessor(PostProcessor):
    """
    PostProcessor yt-dlp yang dijalankan pada tahap 'after_move' tepat setelah
    1 video selesai diunduh dan digabung.
    """

    def __init__(
        self,
        gh: GitHubClient,
        branch: str,
        mode: str,
        release_tag: str,
        release_title: str,
        archive_path: Path,
        videos_md_path: Path,
        keep_local: bool = False,
        start_monotonic: float | None = None,
        max_runtime_minutes: float | None = None,
    ) -> None:
        super().__init__(None)
        self.gh = gh
        self.branch = branch
        self.mode = mode
        self.release_tag = release_tag
        self.release_title = release_title
        self.archive_path = archive_path
        self.videos_md_path = videos_md_path
        self.keep_local = keep_local
        self.start_monotonic = start_monotonic or time.monotonic()
        self.max_runtime_minutes = max_runtime_minutes
        self.uploaded_count = 0
        self._release_cache: dict[str, Any] | None = None

    def _get_release(self) -> dict[str, Any]:
        if self._release_cache is None:
            self._release_cache = self.gh.ensure_release(
                tag_name=self.release_tag,
                title=self.release_title,
                body="Arsip video playlist YouTube yang diunggah otomatis 1 per 1 setelah selesai diunduh.",
            )
        return self._release_cache

    def _record_metadata(
        self,
        info: dict[str, Any],
        archive_id: str,
        total_bytes: int,
        download_urls: list[str],
    ) -> None:
        vid_id = str(info.get("id") or "unknown")
        raw_title = str(info.get("title") or vid_id).replace("|", "\\|").replace("\n", " ").strip()
        uploaded_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if len(download_urls) == 1:
            links_md = f"[Download]({download_urls[0]})"
        else:
            links_md = " / ".join(
                f"[Part {i}]({u})" for i, u in enumerate(download_urls, start=1)
            )

        # 1. Perbarui archive.txt di GitHub & lokal
        remote_archive, archive_sha = self.gh.get_text_file("archive.txt", self.branch)
        lines = [ln.strip() for ln in remote_archive.splitlines() if ln.strip()]
        if self.archive_path.exists():
            for ln in self.archive_path.read_text(encoding="utf-8").splitlines():
                if ln.strip() and ln.strip() not in lines:
                    lines.append(ln.strip())
        if archive_id not in lines:
            lines.append(archive_id)

        updated_archive = "\n".join(lines) + "\n"
        self.archive_path.write_text(updated_archive, encoding="utf-8")
        self.gh.put_text_file(
            repo_path="archive.txt",
            content=updated_archive,
            message=f"chore(archive): catat video {vid_id}",
            branch=self.branch,
            sha=archive_sha,
        )
        if self._downloader is not None:
            self._downloader.archive.add(archive_id)

        # 2. Perbarui VIDEOS.md di GitHub & lokal
        remote_md, md_sha = self.gh.get_text_file("VIDEOS.md", self.branch)
        if not remote_md.strip():
            remote_md = VIDEOS_MD_HEADER

        existing_rows = [
            ln for ln in remote_md.splitlines() if ln.startswith("|") and not ln.startswith("| No ") and not ln.startswith("|---")
        ]
        row_num = len(existing_rows) + 1
        new_row = (
            f"| {row_num} | {raw_title} | `{vid_id}` | {format_bytes(total_bytes)} "
            f"| {uploaded_at} | {links_md} |"
        )
        if f"`{vid_id}`" not in remote_md:
            updated_md = remote_md.rstrip("\n") + "\n" + new_row + "\n"
        else:
            updated_md = remote_md

        self.videos_md_path.write_text(updated_md, encoding="utf-8")
        self.gh.put_text_file(
            repo_path="VIDEOS.md",
            content=updated_md,
            message=f"docs(videos): tambah {raw_title[:60]} ({vid_id})",
            branch=self.branch,
            sha=md_sha,
        )

    def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        filepath_str = info.get("filepath")
        if not filepath_str:
            raise PostProcessingError("Path file hasil unduhan tidak ditemukan di info_dict.")

        filepath = Path(filepath_str)
        if not filepath.exists():
            raise PostProcessingError(f"File hasil unduhan tidak ditemukan: {filepath}")

        file_size = filepath.stat().st_size
        asset_base_name = build_asset_filename(info, filepath)
        vid_id = str(info.get("id") or "unknown")
        title = str(info.get("title") or vid_id)

        log(f"Selesai mengunduh: '{title}' ({format_bytes(file_size)}) -> memulai unggah ke GitHub...")

        effective_mode = self.mode
        if effective_mode == "repo" and file_size > MAX_GIT_REPO_FILE_BYTES:
            log(
                f"Ukuran video ({format_bytes(file_size)}) melebihi batas aman file Git GitHub "
                f"({format_bytes(MAX_GIT_REPO_FILE_BYTES)}). Mengalihkan otomatis ke GitHub Releases..."
            )
            effective_mode = "release"

        download_urls: list[str] = []
        created_parts: list[Path] = []
        try:
            if effective_mode == "repo":
                commit_msg = f"feat(video): unggah {title[:60]} [{vid_id}]"
                if (Path.cwd() / ".git").exists() and shutil.which("git"):
                    url = git_commit_and_push_video(
                        repo_slug=self.gh.repo,
                        token=self.gh.token,
                        branch=self.branch,
                        local_video_path=filepath,
                        asset_name=asset_base_name,
                        commit_message=commit_msg,
                    )
                    download_urls.append(url)
                elif file_size <= MAX_CONTENTS_API_BYTES:
                    url = self.gh.put_binary_file_contents_api(
                        local_path=filepath,
                        repo_path=f"videos/{asset_base_name}",
                        message=commit_msg,
                        branch=self.branch,
                    )
                    download_urls.append(url)
                else:
                    log("Di luar git checkout dan ukuran >45 MiB; mengalihkan ke GitHub Releases...")
                    effective_mode = "release"

            if effective_mode == "release":
                release = self._get_release()
                duration = info.get("duration")
                parts = split_large_file(
                    filepath,
                    duration=float(duration) if isinstance(duration, (int, float)) else None,
                    max_bytes=MAX_RELEASE_ASSET_BYTES,
                )
                if parts != [filepath]:
                    created_parts = parts

                for idx, part_path in enumerate(parts, start=1):
                    if len(parts) == 1:
                        part_asset_name = asset_base_name
                    else:
                        stem = Path(asset_base_name).stem
                        sfx = Path(asset_base_name).suffix
                        if part_path.name.endswith(sfx):
                            part_asset_name = f"{stem}.part{idx:02d}{sfx}"
                        else:
                            part_asset_name = f"{asset_base_name}.part{idx:02d}"

                    dl_url = self.gh.upload_release_asset(release, part_path, part_asset_name)
                    download_urls.append(dl_url)

            archive_id = (
                self._downloader._make_archive_id(info)
                if self._downloader is not None and hasattr(self._downloader, "_make_archive_id")
                else f"youtube {vid_id}"
            ) or f"youtube {vid_id}"

            self._record_metadata(
                info=info,
                archive_id=archive_id,
                total_bytes=file_size,
                download_urls=download_urls,
            )
        except Exception as exc:
            raise PostProcessingError(f"Unggah ke GitHub gagal untuk {vid_id}: {exc}") from exc
        finally:
            for p in created_parts:
                p.unlink(missing_ok=True)

        self.uploaded_count += 1
        log(f"Berhasil mengunggah video #{self.uploaded_count}: {title} -> {download_urls[0]}")

        files_to_delete = [] if self.keep_local else [str(filepath)]

        if self.max_runtime_minutes is not None:
            elapsed_min = (time.monotonic() - self.start_monotonic) / 60.0
            if elapsed_min >= self.max_runtime_minutes:
                if not self.keep_local:
                    filepath.unlink(missing_ok=True)
                log(
                    f"Batas waktu eksekusi ({self.max_runtime_minutes} menit) tercapai setelah "
                    f"menyelesaikan unggahan video #{self.uploaded_count}. Menghentikan sesi dengan aman."
                )
                raise MaxDownloadsReached()

        return files_to_delete, info


def build_format_selector(quality: str, has_ffmpeg: bool) -> str:
    if quality == "audio":
        return "ba[ext=m4a]/ba/b"

    height_map = {
        "best": None,
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
    }
    max_h = height_map.get(quality)

    if not has_ffmpeg:
        if max_h:
            return f"b[height<={max_h}][ext=mp4]/b[height<={max_h}]/b[ext=mp4]/b"
        return "b[ext=mp4]/b"

    if max_h:
        return (
            f"bv*[height<={max_h}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={max_h}][ext=mp4]/"
            f"bv*[height<={max_h}]+ba/b[height<={max_h}]"
        )
    return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"


def preload_combined_archive(gh: GitHubClient, branch: str, local_archive: Path) -> set[str]:
    entries: set[str] = set()
    if local_archive.exists():
        for ln in local_archive.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                entries.add(ln.strip())
    try:
        remote_text, _ = gh.get_text_file("archive.txt", branch)
        for ln in remote_text.splitlines():
            if ln.strip():
                entries.add(ln.strip())
    except Exception as exc:
        log(f"Catatan: gagal memuat archive.txt jarak jauh ({exc}); menggunakan arsip lokal.")

    if entries:
        local_archive.write_text("\n".join(sorted(entries)) + "\n", encoding="utf-8")
    return entries


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unduh playlist YouTube dengan yt-dlp dan langsung unggah tiap video ke GitHub."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_PLAYLIST_URL,
        help=f"URL playlist atau video YouTube (default: {DEFAULT_PLAYLIST_URL})",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=f"Target repository GitHub 'owner/repo' (default: deteksi otomatis atau {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Branch untuk menyimpan archive.txt & VIDEOS.md (default: default branch repo)",
    )
    parser.add_argument(
        "--mode",
        choices=("release", "repo"),
        default="release",
        help="Metode unggah: 'release' (GitHub Releases hingga 2GB/bagian, direkomendasikan) atau 'repo' (folder videos/ untuk file <95MB)",
    )
    parser.add_argument(
        "--quality",
        choices=("best", "1080p", "720p", "480p", "360p", "audio"),
        default="best",
        help="Kualitas video yang diunduh (default: best)",
    )
    parser.add_argument(
        "--release-tag",
        default=None,
        help="Tag GitHub Release tujuan (default: playlist-<ID> atau youtube-uploads)",
    )
    parser.add_argument(
        "--cookies",
        default=os.environ.get("YOUTUBE_COOKIES_FILE"),
        help="Path ke file cookies Netscape (cookies.txt) jika YouTube meminta autentikasi",
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        help="Ambil cookies langsung dari browser lokal (contoh: chrome, firefox, edge, brave)",
    )
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=None,
        help="Batas jumlah video yang diunduh dalam satu kali jalan (opsional)",
    )
    parser.add_argument(
        "--max-runtime-minutes",
        type=float,
        default=None,
        help="Hentikan dengan aman setelah menyelesaikan unggahan video saat durasi mencapai N menit",
    )
    parser.add_argument(
        "--keep-local",
        action="store_true",
        help="Jangan hapus file video lokal setelah selesai diunggah ke GitHub",
    )
    parser.add_argument(
        "--download-dir",
        default="downloads",
        help="Folder sementara untuk mengunduh video (default: downloads)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub Personal Access Token (disarankan gunakan env GITHUB_TOKEN sebagai gantinya)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    url = normalize_youtube_url(args.url)
    token = resolve_github_token(args.token)
    repo_slug = detect_repo_slug(args.repo)

    gh = GitHubClient(repo=repo_slug, token=token)
    default_branch = gh.verify_access()
    branch = args.branch or os.environ.get("GITHUB_REF_NAME") or default_branch
    # Pastikan branch target benar-benar ada di remote; jika belum ada gunakan default_branch
    check_branch = gh._request(
        "GET",
        f"{gh.api_base}/repos/{repo_slug}/branches/{urllib.parse.quote(branch)}",
    )
    if check_branch.status_code == 404:
        log(f"Branch '{branch}' belum ada di remote GitHub; menyimpan metadata ke '{default_branch}'.")
        branch = default_branch

    playlist_id = extract_playlist_id(url)
    release_tag = args.release_tag or (f"playlist-{playlist_id}" if playlist_id else "youtube-uploads")
    release_title = (
        f"YouTube Playlist ({playlist_id})" if playlist_id else "YouTube Video Uploads"
    )

    download_dir = Path(args.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    archive_path = Path("archive.txt")
    videos_md_path = Path("VIDEOS.md")

    has_ffmpeg = shutil.which("ffmpeg") is not None
    if not has_ffmpeg:
        log("Peringatan: 'ffmpeg' tidak ditemukan. Menggunakan format single-stream agar tetap dapat mengunduh.")

    archive_entries = preload_combined_archive(gh, branch, archive_path)
    log(
        f"Siap mengunduh dari: {url}\n"
        f"  Target GitHub : {repo_slug} (branch metadata: {branch}, mode: {args.mode})\n"
        f"  Release Tag   : {release_tag}\n"
        f"  Video tercatat: {len(archive_entries)} video sudah diunggah sebelumnya (akan dilewati otomatis)"
    )

    ydl_opts: dict[str, Any] = {
        "format": build_format_selector(args.quality, has_ffmpeg=has_ffmpeg),
        "outtmpl": str(download_dir / "%(playlist_index&{}_|)s%(id)s.%(ext)s"),
        "lazy_playlist": True,
        "ignoreerrors": "only_download",
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
        "nopart": False,
        "concurrent_fragment_downloads": 4,
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}, "quickjs": {}},
    }

    if has_ffmpeg and args.quality != "audio":
        ydl_opts["merge_output_format"] = "mp4"

    if args.cookies:
        cookies_path = Path(args.cookies)
        if not cookies_path.exists():
            raise FileNotFoundError(f"File cookies tidak ditemukan: {cookies_path}")
        ydl_opts["cookiefile"] = str(cookies_path)

    if args.cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (args.cookies_from_browser,)

    if args.max_downloads is not None and args.max_downloads > 0:
        ydl_opts["max_downloads"] = args.max_downloads

    uploader_pp = GitHubUploadPostProcessor(
        gh=gh,
        branch=branch,
        mode=args.mode,
        release_tag=release_tag,
        release_title=release_title,
        archive_path=archive_path,
        videos_md_path=videos_md_path,
        keep_local=args.keep_local,
        start_monotonic=time.monotonic(),
        max_runtime_minutes=args.max_runtime_minutes,
    )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.archive = set(archive_entries)
        ydl.add_post_processor(uploader_pp, when="after_move")
        try:
            retcode = ydl.download([url])
        except MaxDownloadsReached:
            log("Batas unduhan/waktu tercapai. Semua video yang selesai diunduh telah diunggah ke GitHub.")
            retcode = 0

    log(f"Selesai! Total video baru yang diunggah pada sesi ini: {uploader_pp.uploaded_count}")
    return int(retcode or 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Dihentikan oleh pengguna (Ctrl+C). Video yang sudah terunggah tetap tersimpan di archive.txt.")
        sys.exit(130)
    except Exception as err:
        log(f"ERROR: {err}")
        sys.exit(1)
