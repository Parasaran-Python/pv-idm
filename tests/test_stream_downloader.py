import unittest
import tempfile
import os
import shutil
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from idm_core.config import Config
from idm_core.storage import StorageManager
from idm_core.stream_downloader import StreamDownloader, HLSParser, DASHParser


class MockStreamHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class TestStreamDownloader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_dir = tempfile.mkdtemp()
        
        # Create mock .ts chunks
        cls.ts1 = b"MOCK_TS_PACKET_HEADER_1" * 100
        cls.ts2 = b"MOCK_TS_PACKET_HEADER_2" * 100
        cls.ts3 = b"MOCK_TS_PACKET_HEADER_3" * 100

        with open(os.path.join(cls.server_dir, "seg1.ts"), "wb") as f:
            f.write(cls.ts1)
        with open(os.path.join(cls.server_dir, "seg2.ts"), "wb") as f:
            f.write(cls.ts2)
        with open(os.path.join(cls.server_dir, "seg3.ts"), "wb") as f:
            f.write(cls.ts3)

        # Create mock playlist.m3u8
        m3u8_content = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:10.0,\n"
            "seg1.ts\n"
            "#EXTINF:10.0,\n"
            "seg2.ts\n"
            "#EXTINF:10.0,\n"
            "seg3.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        with open(os.path.join(cls.server_dir, "playlist.m3u8"), "w") as f:
            f.write(m3u8_content)

        # Create mock DASH segments
        cls.dash_video_init = b"FTYP_MP4_VIDEO_INIT"
        cls.dash_video_seg1 = b"MOOF_MDAT_VIDEO_SEG1"
        cls.dash_video_seg2 = b"MOOF_MDAT_VIDEO_SEG2"
        cls.dash_audio_init = b"FTYP_MP4_AUDIO_INIT"
        cls.dash_audio_seg1 = b"MOOF_MDAT_AUDIO_SEG1"
        cls.dash_audio_seg2 = b"MOOF_MDAT_AUDIO_SEG2"

        with open(os.path.join(cls.server_dir, "v_init.mp4"), "wb") as f:
            f.write(cls.dash_video_init)
        with open(os.path.join(cls.server_dir, "v_seg-1.m4s"), "wb") as f:
            f.write(cls.dash_video_seg1)
        with open(os.path.join(cls.server_dir, "v_seg-2.m4s"), "wb") as f:
            f.write(cls.dash_video_seg2)
        with open(os.path.join(cls.server_dir, "a_init.mp4"), "wb") as f:
            f.write(cls.dash_audio_init)
        with open(os.path.join(cls.server_dir, "a_seg-1.m4s"), "wb") as f:
            f.write(cls.dash_audio_seg1)
        with open(os.path.join(cls.server_dir, "a_seg-2.m4s"), "wb") as f:
            f.write(cls.dash_audio_seg2)

        # Create mock manifest.mpd
        mpd_content = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT8.0S" type="static">
  <Period duration="PT8.0S">
    <AdaptationSet mimeType="video/mp4" contentType="video">
      <SegmentTemplate timescale="1000" duration="4000" initialization="$RepresentationID$/v_init.mp4" media="$RepresentationID$/v_seg-$Number$.m4s" startNumber="1"/>
      <Representation id="." bandwidth="2000000" width="1280" height="720"/>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" contentType="audio" lang="en">
      <Role schemeIdUri="urn:mpeg:dash:role:2011" value="main"/>
      <SegmentTemplate timescale="1000" duration="4000" initialization="$RepresentationID$/a_init.mp4" media="$RepresentationID$/a_seg-$Number$.m4s" startNumber="1"/>
      <Representation id="." bandwidth="128000" audioSamplingRate="48000"/>
    </AdaptationSet>
  </Period>
</MPD>
"""
        with open(os.path.join(cls.server_dir, "manifest.mpd"), "w") as f:
            f.write(mpd_content)

        class Handler(MockStreamHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=cls.server_dir, **kwargs)

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.server_dir)

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config = Config(
            config_dir=self.test_dir,
            temp_dir=os.path.join(self.test_dir, "temp"),
            download_dir=os.path.join(self.test_dir, "Downloads"),
        )
        self.storage = StorageManager(self.config)
        self.m3u8_url = f"http://127.0.0.1:{self.port}/playlist.m3u8"
        self.mpd_url = f"http://127.0.0.1:{self.port}/manifest.mpd?auth_token=xyz123"

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_hls_parser(self):
        parser = HLSParser(self.m3u8_url)
        segments = parser.parse()
        self.assertEqual(len(segments), 3)
        self.assertTrue(segments[0].endswith("seg1.ts"))
        self.assertTrue(segments[1].endswith("seg2.ts"))
        self.assertTrue(segments[2].endswith("seg3.ts"))

    def test_probe_stream_info(self):
        info = HLSParser.probe_stream_info(self.m3u8_url)
        self.assertEqual(info["duration"], 30.0)
        self.assertTrue(info["filesize"] > 0)

    def test_dash_parser(self):
        parser = DASHParser(self.mpd_url)
        tracks = parser.parse_tracks()
        self.assertEqual(tracks["duration"], 8.0)
        self.assertEqual(len(tracks["video_segments"]), 3)  # init + 2 segs
        self.assertEqual(len(tracks["audio_segments"]), 3)  # init + 2 segs
        self.assertTrue(tracks["video_segments"][0].endswith("v_init.mp4?auth_token=xyz123"))
        self.assertTrue(tracks["video_segments"][1].endswith("v_seg-1.m4s?auth_token=xyz123"))
        self.assertTrue(tracks["video_segments"][2].endswith("v_seg-2.m4s?auth_token=xyz123"))
        self.assertTrue(tracks["audio_segments"][0].endswith("a_init.mp4?auth_token=xyz123"))
        self.assertEqual(tracks["video_bandwidth"], 2000000)
        self.assertEqual(tracks["audio_bandwidth"], 128000)

    def test_dash_probe_stream_info(self):
        info = DASHParser.probe_stream_info(self.mpd_url)
        self.assertEqual(info["duration"], 8.0)
        self.assertEqual(info["bandwidth"], 2128000)
        self.assertTrue(info["filesize"] > 0)

    def test_stream_downloader_download_hls(self):
        dest_path = os.path.join(self.test_dir, "Downloads", "video.ts")
        completed_event = threading.Event()

        def on_complete(dl_id, path):
            completed_event.set()

        downloader = StreamDownloader(
            download_id="dl-stream-test",
            url=self.m3u8_url,
            save_path=dest_path,
            storage=self.storage,
            config=self.config,
            on_complete=on_complete
        )
        downloader.start()

        completed = completed_event.wait(timeout=10.0)
        self.assertTrue(completed)
        self.assertTrue(os.path.exists(dest_path))
        with open(dest_path, "rb") as f:
            data = f.read()
        self.assertEqual(data, self.ts1 + self.ts2 + self.ts3)

    def test_stream_downloader_download_dash(self):
        dest_path = os.path.join(self.test_dir, "Downloads", "dash_video.mp4")
        completed_event = threading.Event()

        def on_complete(dl_id, path):
            completed_event.set()

        downloader = StreamDownloader(
            download_id="dl-dash-test",
            url=self.mpd_url,
            save_path=dest_path,
            storage=self.storage,
            config=self.config,
            on_complete=on_complete
        )
        downloader.start()

        completed = completed_event.wait(timeout=10.0)
        self.assertTrue(completed)
        self.assertTrue(os.path.exists(dest_path))
    def test_iso8601_duration_parsing(self):
        self.assertEqual(DASHParser.parse_iso8601_duration("PT2H25M35.042S"), 8735.042)
        self.assertEqual(DASHParser.parse_iso8601_duration("PT1H30M"), 5400.0)
        self.assertEqual(DASHParser.parse_iso8601_duration("PT45.5S"), 45.5)
        self.assertEqual(DASHParser.parse_iso8601_duration("P1DT2H"), 93600.0)
        self.assertEqual(DASHParser.parse_iso8601_duration(""), 0.0)

    def test_dash_parser_segment_timeline(self):
        timeline_mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT30S" type="static">
  <Period>
    <AdaptationSet mimeType="video/mp4" contentType="video">
      <SegmentTemplate timescale="1000" initialization="init-$Bandwidth$.mp4" media="chunk-$RepresentationID$-$Number%05d$-$Time$.m4s" startNumber="1">
        <SegmentTimeline>
          <S t="0" d="4000" r="2" />
          <S d="6000" r="1" />
        </SegmentTimeline>
      </SegmentTemplate>
      <Representation id="rep1" bandwidth="1500000" width="1920" height="1080"/>
    </AdaptationSet>
  </Period>
</MPD>
"""
        with open(os.path.join(self.server_dir, "timeline.mpd"), "w") as f:
            f.write(timeline_mpd)

        url = f"http://127.0.0.1:{self.port}/timeline.mpd"
        parser = DASHParser(url)
        tracks = parser.parse_tracks()
        # 1 init + 3 segs (t=0, 4000, 8000) + 2 segs (t=12000, 18000) = 6 segments total
        self.assertEqual(len(tracks["video_segments"]), 6)
        self.assertTrue(tracks["video_segments"][0].endswith("init-1500000.mp4"))
        self.assertTrue(tracks["video_segments"][1].endswith("chunk-rep1-00001-0.m4s"))
        self.assertTrue(tracks["video_segments"][2].endswith("chunk-rep1-00002-4000.m4s"))
        self.assertTrue(tracks["video_segments"][3].endswith("chunk-rep1-00003-8000.m4s"))
        self.assertTrue(tracks["video_segments"][4].endswith("chunk-rep1-00004-12000.m4s"))
        self.assertTrue(tracks["video_segments"][5].endswith("chunk-rep1-00005-18000.m4s"))

    def test_dash_parser_segment_list(self):
        list_mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT20S" type="static">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v1" bandwidth="1000000">
        <SegmentList timescale="1000" duration="10000">
          <Initialization sourceURL="v1-init.mp4" />
          <SegmentURL media="v1-seg1.m4s" />
          <SegmentURL media="v1-seg2.m4s" />
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""
        with open(os.path.join(self.server_dir, "list.mpd"), "w") as f:
            f.write(list_mpd)

        url = f"http://127.0.0.1:{self.port}/list.mpd"
        parser = DASHParser(url)
        tracks = parser.parse_tracks()
        self.assertEqual(len(tracks["video_segments"]), 3)
        self.assertTrue(tracks["video_segments"][0].endswith("v1-init.mp4"))
        self.assertTrue(tracks["video_segments"][1].endswith("v1-seg1.m4s"))
        self.assertTrue(tracks["video_segments"][2].endswith("v1-seg2.m4s"))

    def test_dash_parser_base_url_hierarchy(self):
        base_mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT10S" type="static">
  <BaseURL>media/</BaseURL>
  <Period>
    <BaseURL>vod/</BaseURL>
    <AdaptationSet mimeType="video/mp4">
      <BaseURL>video/</BaseURL>
      <SegmentTemplate timescale="1000" duration="5000" initialization="init.mp4" media="seg-$Number$.m4s" startNumber="1"/>
      <Representation id="1080p" bandwidth="2000000"/>
    </AdaptationSet>
  </Period>
</MPD>
"""
        with open(os.path.join(self.server_dir, "base.mpd"), "w") as f:
            f.write(base_mpd)

        url = f"http://127.0.0.1:{self.port}/base.mpd"
        parser = DASHParser(url)
        tracks = parser.parse_tracks()
        self.assertEqual(len(tracks["video_segments"]), 3)
        self.assertTrue(tracks["video_segments"][0].endswith("media/vod/video/init.mp4"))
        self.assertTrue(tracks["video_segments"][1].endswith("media/vod/video/seg-1.m4s"))
        self.assertTrue(tracks["video_segments"][2].endswith("media/vod/video/seg-2.m4s"))

    def test_stream_downloader_universal_probe(self):
        hls_info = StreamDownloader.probe_stream_info(self.m3u8_url)
        self.assertEqual(hls_info["duration"], 30.0)
        self.assertTrue(hls_info["filesize"] > 0)

        dash_info = StreamDownloader.probe_stream_info(self.mpd_url)
        self.assertEqual(dash_info["duration"], 8.0)
        self.assertEqual(dash_info["bandwidth"], 2128000)
        self.assertTrue(dash_info["filesize"] > 0)

    def test_dash_parser_multi_period(self):
        multi_period_mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT20S" type="static">
  <Period id="p1" duration="PT10S">
    <AdaptationSet mimeType="video/mp4">
      <SegmentTemplate timescale="1000" duration="5000" initialization="p1-init.mp4" media="p1-seg-$Number$.m4s" startNumber="1"/>
      <Representation id="v1" bandwidth="1000000"/>
    </AdaptationSet>
  </Period>
  <Period id="p2" duration="PT10S">
    <AdaptationSet mimeType="video/mp4">
      <SegmentTemplate timescale="1000" duration="5000" initialization="p2-init.mp4" media="p2-seg-$Number$.m4s" startNumber="1"/>
      <Representation id="v2" bandwidth="1000000"/>
    </AdaptationSet>
  </Period>
</MPD>
"""
        with open(os.path.join(self.server_dir, "multi_period.mpd"), "w") as f:
            f.write(multi_period_mpd)

        url = f"http://127.0.0.1:{self.port}/multi_period.mpd"
        parser = DASHParser(url)
        tracks = parser.parse_tracks()
        # Period 1 (init + 2 segs) + Period 2 (init + 2 segs) = 6 segments
        self.assertEqual(len(tracks["video_segments"]), 6)
        self.assertTrue(tracks["video_segments"][0].endswith("p1-init.mp4"))
        self.assertTrue(tracks["video_segments"][1].endswith("p1-seg-1.m4s"))
        self.assertTrue(tracks["video_segments"][2].endswith("p1-seg-2.m4s"))
        self.assertTrue(tracks["video_segments"][3].endswith("p2-init.mp4"))
        self.assertTrue(tracks["video_segments"][4].endswith("p2-seg-1.m4s"))
        self.assertTrue(tracks["video_segments"][5].endswith("p2-seg-2.m4s"))

    def test_detect_stream_type(self):
        self.assertEqual(StreamDownloader.detect_stream_type("http://example.com/live/master.m3u8"), "hls")
        self.assertEqual(StreamDownloader.detect_stream_type("http://example.com/live/master.m3u8?token=123"), "hls")
        self.assertEqual(StreamDownloader.detect_stream_type("http://example.com/vod/stream.mpd"), "dash")
        self.assertEqual(StreamDownloader.detect_stream_type("http://example.com/vod/stream.mpd?hdnea=xyz"), "dash")
        self.assertEqual(StreamDownloader.detect_stream_type("http://example.com/unknown", content="<MPD xmlns='...'>"), "dash")
        self.assertEqual(StreamDownloader.detect_stream_type("http://example.com/unknown", content="#EXTM3U\n..."), "hls")

    def test_extract_formats_and_quality_selection(self):
        multi_qual_mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT10S" type="static">
  <Period>
    <AdaptationSet mimeType="video/mp4" contentType="video">
      <SegmentTemplate timescale="1000" duration="5000" initialization="init-$RepresentationID$.mp4" media="chunk-$RepresentationID$-$Number$.m4s" startNumber="1"/>
      <Representation id="1080p" bandwidth="4000000" width="1920" height="1080" frameRate="60"/>
      <Representation id="720p" bandwidth="2000000" width="1280" height="720" frameRate="30"/>
      <Representation id="480p" bandwidth="1000000" width="854" height="480" frameRate="30"/>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" contentType="audio" lang="en">
      <SegmentTemplate timescale="1000" duration="5000" initialization="audio-init.mp4" media="audio-$Number$.m4s" startNumber="1"/>
      <Representation id="a1" bandwidth="128000" audioSamplingRate="48000"/>
    </AdaptationSet>
  </Period>
</MPD>
"""
        with open(os.path.join(self.server_dir, "multi_qual.mpd"), "w") as f:
            f.write(multi_qual_mpd)

        url = f"http://127.0.0.1:{self.port}/multi_qual.mpd"

        # 1. Test extract_formats
        fmts = StreamDownloader.extract_formats(url)
        self.assertTrue(len(fmts) >= 4)
        labels = [f["label"] for f in fmts]
        self.assertTrue(any("1080p" in l for l in labels))
        self.assertTrue(any("720p" in l for l in labels))
        self.assertTrue(any("480p" in l for l in labels))
        self.assertTrue(any("Audio" in l for l in labels))

        # 2. Test quality selection for 720p
        p_720 = DASHParser(url, headers={"quality": "720"})
        tracks_720 = p_720.parse_tracks()
        self.assertTrue(tracks_720["video_segments"][0].endswith("init-720p.mp4"))

        # 3. Test quality selection for 480p
        p_480 = DASHParser(url, headers={"quality": "480p"})
        tracks_480 = p_480.parse_tracks()
        self.assertTrue(tracks_480["video_segments"][0].endswith("init-480p.mp4"))

        # 4. Test audio-only quality selection
        p_audio = DASHParser(url, headers={"quality": "audio"})
        tracks_audio = p_audio.parse_tracks()
        self.assertEqual(len(tracks_audio["video_segments"]), 0)
        self.assertTrue(len(tracks_audio["audio_segments"]) > 0)

    def test_hls_live_stream_detection(self):
        """Test that live HLS streams (missing EXT-X-ENDLIST) are marked as approximate."""
        # Live master playlist (variant has no EXT-X-ENDLIST)
        live_master = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
live_720p.m3u8
"""
        live_variant = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
"""
        # VOD master playlist (variant has EXT-X-ENDLIST)
        vod_master = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
vod_720p.m3u8
"""
        vod_variant = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXT-X-ENDLIST
"""
        # Live media playlist (no EXT-X-ENDLIST)
        live_media = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
"""
        # VOD media playlist (with EXT-X-ENDLIST)
        vod_media = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXT-X-ENDLIST
"""
        import tempfile
        import os
        import shutil
        from pathlib import Path

        temp_dir = tempfile.mkdtemp()
        try:
            # Write test files
            with open(os.path.join(temp_dir, 'live_master.m3u8'), 'w') as f:
                f.write(live_master)
            with open(os.path.join(temp_dir, 'live_720p.m3u8'), 'w') as f:
                f.write(live_variant)
            with open(os.path.join(temp_dir, 'vod_master.m3u8'), 'w') as f:
                f.write(vod_master)
            with open(os.path.join(temp_dir, 'vod_720p.m3u8'), 'w') as f:
                f.write(vod_variant)
            with open(os.path.join(temp_dir, 'live_media.m3u8'), 'w') as f:
                f.write(live_media)
            with open(os.path.join(temp_dir, 'vod_media.m3u8'), 'w') as f:
                f.write(vod_media)

            base_url = Path(temp_dir).as_uri() + '/'

            # Test live master playlist
            live_master_url = base_url + 'live_master.m3u8'
            probe = HLSParser.probe_stream_info(live_master_url)
            self.assertEqual(probe["duration"], 20.0)
            self.assertTrue(probe["filesize_approx"], "Live master should be marked as approximate")

            # Test VOD master playlist
            vod_master_url = base_url + 'vod_master.m3u8'
            probe = HLSParser.probe_stream_info(vod_master_url)
            self.assertEqual(probe["duration"], 20.0)
            self.assertFalse(probe["filesize_approx"], "VOD master should not be marked as approximate")

            # Test live media playlist
            live_media_url = base_url + 'live_media.m3u8'
            probe = HLSParser.probe_stream_info(live_media_url)
            self.assertEqual(probe["duration"], 20.0)
            self.assertTrue(probe["filesize_approx"], "Live media should be marked as approximate")

            # Test VOD media playlist
            vod_media_url = base_url + 'vod_media.m3u8'
            probe = HLSParser.probe_stream_info(vod_media_url)
            self.assertEqual(probe["duration"], 20.0)
            # Media playlists lack BANDWIDTH, so they use fallback and are marked approximate
            self.assertTrue(probe["filesize_approx"], "Media playlists lack BANDWIDTH, so they are approximate")

            # Test extract_formats for live vs VOD
            live_formats = HLSParser.extract_formats(live_master_url)
            self.assertTrue(live_formats[0]["filesize_approx"], "Live extract_formats should be approximate")

            vod_formats = HLSParser.extract_formats(vod_master_url)
            self.assertFalse(vod_formats[0]["filesize_approx"], "VOD extract_formats should not be approximate")

            live_media_formats = HLSParser.extract_formats(live_media_url)
            self.assertTrue(live_media_formats[0]["filesize_approx"], "Live media extract_formats should be approximate")

            vod_media_formats = HLSParser.extract_formats(vod_media_url)
            self.assertTrue(vod_media_formats[0]["filesize_approx"], "Media extract_formats lacks BANDWIDTH, so approximate")

        finally:
            shutil.rmtree(temp_dir)

    def test_hls_master_without_bandwidth(self):
        """Test HLS master playlist without BANDWIDTH attribute still probes duration."""
        master_no_bw = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:RESOLUTION=1280x720
720p.m3u8
"""
        variant = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXTINF:10.0,
seg3.ts
#EXT-X-ENDLIST
"""

        import tempfile
        import os
        import shutil
        from pathlib import Path

        temp_dir = tempfile.mkdtemp()
        try:
            with open(os.path.join(temp_dir, 'master.m3u8'), 'w') as f:
                f.write(master_no_bw)
            with open(os.path.join(temp_dir, '720p.m3u8'), 'w') as f:
                f.write(variant)

            master_url = Path(os.path.join(temp_dir, 'master.m3u8')).as_uri()
            probe = HLSParser.probe_stream_info(master_url)

            # Should fetch variant and get duration even without BANDWIDTH
            self.assertEqual(probe["duration"], 30.0)
            self.assertEqual(probe["bandwidth"], 2500000)  # Fallback bandwidth
            self.assertTrue(probe["filesize_approx"])  # Marked approx due to fallback bandwidth
        finally:
            shutil.rmtree(temp_dir)

    def test_dash_dynamic_manifest(self):
        """Test that dynamic DASH manifests (no mediaPresentationDuration) are marked as approximate."""
        dynamic_mpd = r"""<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic">
  <Period>
    <AdaptationSet mimeType="video/mp4" contentType="video">
      <SegmentTemplate timescale="1000" duration="4000" initialization="init.mp4" media="seg-\$Number\$.m4s" startNumber="1"/>
      <Representation id="720p" bandwidth="2000000" width="1280" height="720"/>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" contentType="audio" lang="en">
      <Role schemeIdUri="urn:mpeg:dash:role:2011" value="main"/>
      <SegmentTemplate timescale="1000" duration="4000" initialization="audio-init.mp4" media="audio-\$Number\$.m4s" startNumber="1"/>
      <Representation id="audio" bandwidth="128000" audioSamplingRate="48000"/>
    </AdaptationSet>
  </Period>
</MPD>
"""

        import tempfile
        import os
        from pathlib import Path

        with tempfile.NamedTemporaryFile(mode='w', suffix='.mpd', delete=False) as f:
            f.write(dynamic_mpd)
            mpd_path = f.name

        try:
            mpd_url = Path(mpd_path).as_uri()
            formats = DASHParser.extract_formats(mpd_url)
            probe = DASHParser.probe_stream_info(mpd_url)

            # Dynamic manifest should have approx flag
            self.assertTrue(probe["filesize_approx"], "Dynamic DASH should be marked as approximate")
            self.assertEqual(probe["duration"], 0.0)
            self.assertEqual(probe["filesize"], 0)

            for fmt in formats:
                self.assertTrue(fmt["filesize_approx"], "Dynamic DASH formats should be approximate")
        finally:
            os.unlink(mpd_path)

    def test_stream_downloader_assembling_status(self):
        progress_events = []
        downloader = StreamDownloader(
            "stream-asm",
            self.m3u8_url,
            os.path.join(self.test_dir, "asm.mp4"),
            on_progress=lambda did, s: progress_events.append(s)
        )
        downloader.segment_files = []
        downloader._finalize_stream()
        self.assertEqual(downloader.status, "completed")
        statuses = [e["status"] for e in progress_events]
        self.assertIn("assembling", statuses)


if __name__ == "__main__":
    unittest.main()

