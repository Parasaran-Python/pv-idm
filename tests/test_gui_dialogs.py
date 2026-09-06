import unittest
import sys
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication
from idm_gui.dialogs.download_info_dialog import DownloadInfoDialog
from idm_gui.dialogs.download_progress_dialog import DownloadProgressDialog
from idm_gui.dialogs.queue_scheduler_dialog import QueueSchedulerDialog
from idm_gui.dialogs.options_dialog import OptionsDialog
from idm_gui.dialogs.batch_download_dialog import BatchDownloadDialog

app = QApplication.instance() or QApplication(sys.argv)


class TestGUIDialogs(unittest.TestCase):
    def test_download_info_dialog(self):
        dialog = DownloadInfoDialog(
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path="/tmp/file.zip",
            category="Compressed"
        )
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.url_edit.text(), "https://example.com/file.zip")
        self.assertEqual(dialog.category_combo.currentText(), "Compressed")

    def test_download_progress_dialog(self):
        dialog = DownloadProgressDialog(download_id="dl-test-dialog", filename="test.bin")
        self.assertIsNotNone(dialog)
        dialog.update_progress({
            "status": "downloading",
            "downloaded_bytes": 500000,
            "total_bytes": 1000000,
            "speed": 250000,
            "eta": 2,
            "resumable": True
        })
        self.assertEqual(dialog.status_label.text(), "Downloading")

    def test_download_progress_dialog_assembling_status(self):
        dialog = DownloadProgressDialog(download_id="dl-test-merging", filename="video.mp4")
        dialog.update_progress({
            "status": "assembling",
            "downloaded_bytes": 1000000,
            "total_bytes": 1000000,
            "speed": 0,
            "eta": 0,
            "resumable": True
        })
        self.assertEqual(dialog.status_label.text(), "Merging audio & video...")
        self.assertEqual(dialog.progress_bar.value(), 100)
        self.assertEqual(dialog.progress_bar.format(), "100% (Merging...)")
        self.assertEqual(dialog.speed_label.text(), "Processing...")
        self.assertEqual(dialog.eta_label.text(), "Finalizing...")

    def test_queue_scheduler_dialog(self):
        dialog = QueueSchedulerDialog()
        self.assertIsNotNone(dialog)

    def test_options_dialog(self):
        dialog = OptionsDialog()
        self.assertIsNotNone(dialog)
        self.assertGreaterEqual(dialog.connections_spin.value(), 1)

    def test_batch_download_dialog(self):
        dialog = BatchDownloadDialog()
        self.assertIsNotNone(dialog)
        dialog.text_edit.setPlainText("https://ex.com/1.zip\nhttps://ex.com/2.zip")
        urls = dialog.get_urls()
        self.assertEqual(len(urls), 2)

    def test_probe_worker_direct_media_skips_ytdlp(self):
        from idm_gui.dialogs.download_info_dialog import ProbeWorker
        from idm_core.ytdlp_downloader import YTDLPDownloader
        import unittest.mock

        worker = ProbeWorker(
            url="https://example.com/get_file/3461901_720p.mp4/?v-acctoken=abc",
            headers={"quality": "best"}
        )
        with unittest.mock.patch.object(YTDLPDownloader, "probe_media_info") as mock_probe:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.headers = {"Content-Length": "2313811557", "Content-Disposition": 'filename="3461901_720p.mp4"'}
            mock_resp.__enter__.return_value = mock_resp
            with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
                worker._run()
                mock_probe.assert_not_called()

    def test_probe_worker_platform_media_uses_ytdlp(self):
        from idm_gui.dialogs.download_info_dialog import ProbeWorker
        from idm_core.ytdlp_downloader import YTDLPDownloader
        import unittest.mock

        worker = ProbeWorker(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            headers={"quality": "1080"}
        )
        with unittest.mock.patch.object(YTDLPDownloader, "is_ytdlp_available", return_value=True), \
             unittest.mock.patch.object(YTDLPDownloader, "probe_media_info", return_value={"filesize": 1000, "filename": "video.mp4"}) as mock_probe:
            worker._run()
            mock_probe.assert_called_once_with("https://www.youtube.com/watch?v=dQw4w9WgXcQ", quality="1080")

    def test_probe_worker_normalizes_videoplayback_url(self):
        from idm_gui.dialogs.download_info_dialog import ProbeWorker
        from idm_core.utils import normalize_youtube_videoplayback_url
        worker = ProbeWorker(
            url="https://rr1---sn-4g5ednsl.googlevideo.com/videoplayback?expire=12345",
            headers={"Referer": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
        )
        worker.url, _ = normalize_youtube_videoplayback_url(worker.url, worker.headers)
        self.assertEqual(worker.url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        # Non-youtube referer should not normalize
        worker2 = ProbeWorker(
            url="https://rr1---sn-4g5ednsl.googlevideo.com/videoplayback?expire=12345",
            headers={"Referer": "https://example.com/watch?v=123"}
        )
        worker2.url, _ = normalize_youtube_videoplayback_url(worker2.url, worker2.headers)
        self.assertEqual(worker2.url, "https://rr1---sn-4g5ednsl.googlevideo.com/videoplayback?expire=12345")

    def test_download_info_dialog_normalizes_videoplayback_url(self):
        dialog = DownloadInfoDialog(
            url="https://rr1---sn-4g5ednsl.googlevideo.com/videoplayback?expire=12345",
            headers={"Referer": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            category="General"
        )
        self.assertEqual(dialog.url_edit.text(), "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        
        # Test category auto-update in _on_probed
        dialog._on_probed(10485760, "MyVideo.mp4")
        self.assertEqual(dialog.category_combo.currentText(), "Video")
        self.assertIn("MyVideo.mp4", dialog.save_edit.text())


if __name__ == "__main__":
    unittest.main()