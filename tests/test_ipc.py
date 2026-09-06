import unittest
import tempfile
import os
import shutil
import time
from idm_core.config import Config
from idm_core.engine import DownloadEngine
from idm_ipc.socket_server import IPCServer
from idm_ipc.socket_client import IPCClient


class TestIPC(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.test_dir, "test_idm.sock")
        self.config = Config(
            config_dir=self.test_dir,
            socket_path=self.sock_path,
            database_path=os.path.join(self.test_dir, "ipc_test.db"),
            temp_dir=os.path.join(self.test_dir, "temp"),
            download_dir=os.path.join(self.test_dir, "Downloads"),
        )
        self.engine = DownloadEngine(self.config)
        self.server = IPCServer(self.engine, self.config.socket_path)
        self.server.start()
        for _ in range(30):
            if IPCClient(self.config.socket_path).is_server_running():
                break
            time.sleep(0.05)

    def tearDown(self):
        self.server.stop()
        self.engine.shutdown()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ping_and_add_download_via_ipc(self):
        client = IPCClient(self.config.socket_path)
        self.assertTrue(client.is_server_running())

        # Test Ping
        pong = client.ping()
        self.assertTrue(pong.get("pong", False))

        # Test Add Download via IPC
        res = client.send_request({
            "action": "add_download",
            "url": "https://example.com/test_ipc_file.zip",
            "start_immediately": False
        })
        self.assertEqual(res.get("status"), "ok")
        dl_id = res.get("download_id")
        self.assertIsNotNone(dl_id)

        # Test Get Download info
        dl_info = client.send_request({
            "action": "get_download",
            "download_id": dl_id
        })
        self.assertEqual(dl_info.get("status"), "ok")
        self.assertEqual(dl_info["download"]["url"], "https://example.com/test_ipc_file.zip")

        # Test List Downloads
        list_res = client.send_request({"action": "list_downloads"})
        self.assertEqual(list_res.get("status"), "ok")
        self.assertEqual(len(list_res["downloads"]), 1)

    def test_add_download_normalizes_videoplayback_via_ipc(self):
        client = IPCClient(self.config.socket_path)
        self.assertTrue(client.is_server_running())

        res = client.send_request({
            "action": "add_download",
            "url": "https://rr1---sn-4g5ednsl.googlevideo.com/videoplayback?expire=12345&mime=video%2Fmp4",
            "headers": {"Referer": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            "start_immediately": False
        })
        self.assertEqual(res.get("status"), "ok")
        dl_id = res.get("download_id")
        self.assertIsNotNone(dl_id)

        dl_info = client.send_request({
            "action": "get_download",
            "download_id": dl_id
        })
        self.assertEqual(dl_info.get("status"), "ok")
        self.assertEqual(dl_info["download"]["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(dl_info["download"]["filename"], "dQw4w9WgXcQ.mp4")

    def test_query_media_formats_routes_to_ytdlp_for_video_platforms(self):
        client = IPCClient(self.config.socket_path)
        self.assertTrue(client.is_server_running())

        mock_formats = [
            {"label": "1080p (Full HD)", "quality": "1080", "filesize": 16000000, "format": "MP4"},
            {"label": "720p (HD)", "quality": "720", "filesize": 12000000, "format": "MP4"},
            {"label": "Audio Only (MP3)", "quality": "audio", "filesize": 3000000, "format": "MP3"}
        ]

        with unittest.mock.patch("idm_core.ytdlp_downloader.YTDLPDownloader.is_video_platform_url", return_value=True), \
             unittest.mock.patch("idm_core.ytdlp_downloader.YTDLPDownloader.extract_media_formats", return_value=mock_formats) as mock_extract:
            res = client.send_request({
                "action": "query_media_formats",
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            })
            self.assertEqual(res.get("status"), "ok")
            self.assertEqual(res.get("formats"), mock_formats)
            self.assertTrue(mock_extract.called)


if __name__ == "__main__":
    unittest.main()
