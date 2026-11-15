import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import download_and_upload as dau


class FakeGitHubClient:
    def __init__(self) -> None:
        self.repo = "Neldah-3/Live"
        self.token = "fake-token"
        self.files: dict[str, tuple[str, str]] = {}
        self.uploaded_assets: list[tuple[str, int]] = []
        self.release = {"id": 99, "tag_name": "playlist-test"}

    def ensure_release(self, tag_name: str, title: str, body: str) -> dict:
        return self.release

    def upload_release_asset(self, release: dict, local_path: Path, asset_name: str) -> str:
        size = local_path.stat().st_size
        self.uploaded_assets.append((asset_name, size))
        return f"https://github.com/{self.repo}/releases/download/{release['tag_name']}/{asset_name}"

    def get_text_file(self, repo_path: str, branch: str) -> tuple[str, str | None]:
        if repo_path not in self.files:
            return "", None
        return self.files[repo_path]

    def put_text_file(
        self,
        repo_path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> None:
        new_sha = f"sha-{len(content)}"
        self.files[repo_path] = (content, new_sha)


class DownloadAndUploadTests(unittest.TestCase):
    def test_normalize_escaped_playlist_url(self) -> None:
        raw = (
            r"https://youtube.com/playlist?list=PLX15XB\_3XZuR7NjjcnxLr770zy4PLWut\_"
            r"&amp;si=XZkgP1AHn0UL0DHh"
        )
        cleaned = dau.normalize_youtube_url(raw)
        self.assertEqual(
            cleaned,
            "https://youtube.com/playlist?list=PLX15XB_3XZuR7NjjcnxLr770zy4PLWut_&si=XZkgP1AHn0UL0DHh",
        )
        self.assertEqual(
            dau.extract_playlist_id(cleaned),
            "PLX15XB_3XZuR7NjjcnxLr770zy4PLWut_",
        )

    def test_build_asset_filename_safe_ascii(self) -> None:
        info = {
            "id": "dQw4w9WgXcQ",
            "title": "Live Stream #1: Malam Minggu 🔥 [Full HD]",
            "playlist_index": 3,
            "ext": "mp4",
        }
        name = dau.build_asset_filename(info, Path("/tmp/003_dQw4w9WgXcQ.mp4"))
        self.assertEqual(name, "003_Live-Stream-1-Malam-Minggu-Full-HD_dQw4w9WgXcQ.mp4")

    def test_split_large_file_binary_reassembly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "bigvideo.mp4"
            payload = (b"ABCDEF0123456789" * 1024) * 3  # 48 KiB
            src.write_bytes(payload)

            with patch("shutil.which", return_value=None):
                parts = dau.split_large_file(src, duration=None, max_bytes=20 * 1024)

            self.assertEqual(len(parts), 3)
            self.assertTrue(all(p.stat().st_size <= 20 * 1024 for p in parts))
            rejoined = b"".join(p.read_bytes() for p in parts)
            self.assertEqual(rejoined, payload)

    def test_postprocessor_uploads_each_video_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            archive_path = tmp_dir / "archive.txt"
            videos_md_path = tmp_dir / "VIDEOS.md"
            fake_gh = FakeGitHubClient()

            pp = dau.GitHubUploadPostProcessor(
                gh=fake_gh,  # type: ignore[arg-type]
                branch="main",
                mode="release",
                release_tag="playlist-test",
                release_title="Test Playlist",
                archive_path=archive_path,
                videos_md_path=videos_md_path,
                keep_local=False,
            )

            # Video 1 selesai diunduh -> langsung diunggah
            vid1 = tmp_dir / "vid1.mp4"
            vid1.write_bytes(b"video-one-bytes")
            files_to_del_1, _ = pp.run(
                {
                    "filepath": str(vid1),
                    "id": "vid001",
                    "title": "Video Pertama",
                    "playlist_index": 1,
                }
            )
            self.assertEqual(files_to_del_1, [str(vid1)])
            self.assertEqual(len(fake_gh.uploaded_assets), 1)
            self.assertIn("youtube vid001", fake_gh.files["archive.txt"][0])
            self.assertIn("`vid001`", fake_gh.files["VIDEOS.md"][0])

            # Video 2 (> max_bytes, dipecah 2 part) -> langsung diunggah
            vid2 = tmp_dir / "vid2.mp4"
            vid2.write_bytes(b"X" * 5000)
            with (
                patch.object(dau, "MAX_RELEASE_ASSET_BYTES", 3000),
                patch("shutil.which", return_value=None),
            ):
                files_to_del_2, _ = pp.run(
                    {
                        "filepath": str(vid2),
                        "id": "vid002",
                        "title": "Video Kedua Besar",
                        "playlist_index": 2,
                    }
                )

            self.assertEqual(files_to_del_2, [str(vid2)])
            self.assertEqual(len(fake_gh.uploaded_assets), 3)  # 1 + 2 parts
            self.assertIn("youtube vid002", fake_gh.files["archive.txt"][0])
            self.assertIn("[Part 1]", fake_gh.files["VIDEOS.md"][0])
            self.assertIn("[Part 2]", fake_gh.files["VIDEOS.md"][0])
            # Pastikan file part sementara sudah dibersihkan
            self.assertEqual(list(tmp_dir.glob("vid2.mp4.part*")), [])


if __name__ == "__main__":
    unittest.main()
