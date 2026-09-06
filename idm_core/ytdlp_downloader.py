"""
Video Platform Stream Downloader using yt-dlp with Real-Time Telemetry & Progress Parsing
Handles downloading from YouTube, Vimeo, Dailymotion, Reddit, Twitter/X, Twitch, etc.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple
from idm_core.config import Config
from idm_core.platform import resolve_binary


class YTDLPDownloader:
    # Explicit tuple of direct static video and audio formats.
    # Note: Streaming manifests (.m3u8, .mpd) and fragmented media chunks (.m4s)
    # are excluded as they require specialized manifest parsers (StreamDownloader).
    # Static image formats are processed via standard engine HTTP downloads.
    DIRECT_MEDIA_EXTS = (
        ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".3gp", ".ts",
        ".mpg", ".mpeg", ".ogv",
        ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".wav", ".opus", ".wma"
    )

    _media_info_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
    _cache_lock = threading.Lock()
    _CACHE_TTL = 300.0  # 5 minutes

    @classmethod
    def clear_cache(cls):
        """Clear cached format and probe metadata."""
        with cls._cache_lock:
            cls._media_info_cache.clear()

    def __init__(
        self,
        download_id: str,
        url: str,
        save_path: str,
        config: Optional[Config] = None,
        speed_limit: int = 0,
        headers: Optional[Dict[str, str]] = None,
        quality: Optional[str] = None,
        total_bytes: int = 0,
        on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_segment_update: Optional[Callable[[str, List[dict]], None]] = None,
        on_complete: Optional[Callable[[str, str], None]] = None,
        on_error: Optional[Callable[[str, str], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ):
        self.download_id = download_id
        self.url = url
        self.save_path = save_path
        self.config = config or Config()
        self.speed_limit = speed_limit
        self.headers = headers or {}
        self.quality = quality
        self.on_progress = on_progress
        self.on_segment_update = on_segment_update
        self.on_complete = on_complete
        self.on_error = on_error
        self.on_log = on_log

        self.status = "idle"
        self.total_bytes = total_bytes
        self._initial_total_bytes = total_bytes
        self.downloaded_bytes = 0
        self.speed = 0.0
        self.eta = 0.0

        self._completed_streams_bytes = 0
        self._current_stream_total = 0
        self._current_stream_downloaded = 0
        self._last_stream_pct = 0.0

        self._process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._lock = threading.RLock()

    @property
    def current_speed(self) -> float:
        return self.speed

    @staticmethod
    def is_ytdlp_available() -> bool:
        return resolve_binary("yt-dlp") is not None or resolve_binary("youtube-dl") is not None

    @classmethod
    def is_direct_media_url(cls, url: str) -> bool:
        """Check if URL directly points to a media file (.mp4, .webm, .mp3, etc.)."""
        if not url or cls.is_video_platform_url(url):
            return False
        try:
            parsed = urllib.parse.urlparse(url)
            clean_path = urllib.parse.unquote(parsed.path.rstrip("/ \t"))
            if clean_path == "/watch" or clean_path.startswith(("/watch/", "/shorts/", "/live/")):
                return False
            ext = os.path.splitext(clean_path)[1].lower()
            return ext in cls.DIRECT_MEDIA_EXTS
        except Exception:
            return False

    @staticmethod
    def is_video_platform_url(url: str) -> bool:
        """Check if URL points to a known streaming video platform."""
        if not url:
            return False
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            domains = (
                "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
                "twitch.tv", "tiktok.com", "twitter.com", "x.com",
                "facebook.com", "fb.watch", "instagram.com", "reddit.com",
                "soundcloud.com", "bilibili.com"
            )
            return any(host == d or host.endswith("." + d) for d in domains)
        except Exception:
            return False

    @classmethod
    def _get_js_runtime_args(cls) -> List[str]:
        """Find and configure available JS runtime for yt-dlp challenge solving."""
        for rt_name in ["node", "deno", "bun", "quickjs"]:
            p = shutil.which(rt_name)
            if p:
                return ["--js-runtimes", f"{rt_name}:{p}"]
        return []

    @classmethod
    def _get_extractor_args(cls, url: str) -> List[str]:
        """Configure optimal extractor arguments to prevent 403 Forbidden and SABR format issues."""
        lower = (url or "").lower()
        if "youtube.com" in lower or "youtu.be" in lower:
            return ["--extractor-args", "youtube:player_client=android,mweb,web_embedded,web"]
        return []

    @classmethod
    def extract_media_formats(cls, url: str) -> List[Dict[str, Any]]:
        """Dynamically extract authentic available formats for any video URL."""
        if not cls.is_ytdlp_available() or not url:
            return []

        now = time.time()
        with cls._cache_lock:
            if url in cls._media_info_cache:
                ts, cached_info = cls._media_info_cache[url]
                if now - ts < cls._CACHE_TTL:
                    return list(cached_info.get("formats") or [])

        bin_name = resolve_binary("yt-dlp") or resolve_binary("youtube-dl") or "yt-dlp"
        cmd = [
            bin_name,
            "-J",
            "--no-playlist",
            "--no-check-certificates",
            "--geo-bypass",
        ]
        if "yt-dlp" in bin_name.lower():
            cmd.extend(["--compat-options", "allow-unsafe-ext", "--remote-components", "ejs:github"])
        cmd.extend(cls._get_js_runtime_args())
        cmd.extend(cls._get_extractor_args(url))
        cmd.append(url)

        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
            if res.returncode != 0 or not res.stdout:
                return []

            data = json.loads(res.stdout)
            tier_formats = cls._parse_formats_and_tiers(data, url)
            raw_title = data.get("title") or ""
            clean_title = re.sub(r'[\\/:*?"<>|]', '_', raw_title).strip()
            ext = data.get("ext") or "mp4"
            duration = data.get("duration") or 0
            with cls._cache_lock:
                cls._media_info_cache[url] = (now, {
                    "title": clean_title,
                    "ext": ext,
                    "duration": duration,
                    "formats": tier_formats
                })
            return tier_formats
        except Exception:
            return []

    @classmethod
    def _get_vcodec_priority(cls, vcodec: Optional[str]) -> int:
        """Score video codec priority matching yt-dlp's default bestvideo preference."""
        v = (vcodec or "").lower()
        if "av01" in v or "av1" in v:
            return 40
        if "vp09.02" in v or "vp9.2" in v:
            return 35
        if "vp09" in v or "vp9" in v:
            return 30
        if "avc1" in v or "h264" in v:
            return 20
        if v != "none" and v != "":
            return 10
        return 0

    @classmethod
    def _get_acodec_priority(cls, acodec: Optional[str]) -> int:
        """Score audio codec priority preferring standard AAC/mp4a over Opus for MP4 container compatibility."""
        a = (acodec or "").lower()
        if "mp4a" in a or "aac" in a:
            return 30
        if "opus" in a:
            return 20
        if a != "none" and a != "":
            return 10
        return 0

    @classmethod
    def _parse_formats_and_tiers(cls, data: Dict[str, Any], url: str) -> List[Dict[str, Any]]:
        """Unified parser ensuring 100% identical format definitions and file sizes across dropdown and dialogs."""
        formats = data.get("formats", [])
        duration = data.get("duration") or 0
        tier_map = {}
        has_audio = False

        def get_format_size(f):
            sz = f.get("filesize") or f.get("filesize_approx") or 0
            tbr = f.get("tbr") or f.get("vbr") or f.get("abr") or 0
            if not sz and tbr and duration:
                sz = int((tbr * 1000 / 8) * duration)
            return sz

        # 1. Best audio stream matching yt-dlp (acodec preference + abr/filesize)
        best_audio_format = None
        for f in formats:
            if f.get("vcodec") == "none" and f.get("acodec") != "none":
                has_audio = True
                acodec_score = cls._get_acodec_priority(f.get("acodec"))
                abr = f.get("abr") or f.get("tbr") or 0
                sz = get_format_size(f)

                is_better_audio = False
                if best_audio_format is None:
                    is_better_audio = True
                else:
                    curr_score = cls._get_acodec_priority(best_audio_format.get("acodec"))
                    curr_abr = best_audio_format.get("abr") or best_audio_format.get("tbr") or 0
                    curr_sz = get_format_size(best_audio_format)
                    if acodec_score > curr_score:
                        is_better_audio = True
                    elif acodec_score == curr_score:
                        if abr > curr_abr or (abr == curr_abr and sz > curr_sz):
                            is_better_audio = True

                if is_better_audio:
                    best_audio_format = f

        best_audio_sz = get_format_size(best_audio_format) if best_audio_format else 0

        # 2. Select standard video format matching yt-dlp format selection
        for f in formats:
            h = f.get("height")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if acodec != "none":
                has_audio = True

            if h and vcodec != "none" and h > 0:
                fps = f.get("fps") or 0
                raw_sz = get_format_size(f)
                tbr = f.get("tbr") or f.get("vbr") or 0
                is_separate_video = (acodec == "none")
                total_sz = raw_sz + (best_audio_sz if is_separate_video else 0)
                vcodec_score = cls._get_vcodec_priority(vcodec)

                # Format preference matching yt-dlp:
                # 1. Prefer separate adaptive video streams over pre-muxed streams when best audio is present
                # 2. Higher fps > Higher codec efficiency (av01 > vp9 > avc1) > Bitrate/Size
                is_better = False
                if h not in tier_map:
                    is_better = True
                else:
                    curr = tier_map[h]
                    curr_score = curr.get("vcodec_score", 0)
                    curr_is_separate = curr.get("is_separate_video", False)
                    if best_audio_format and is_separate_video and not curr_is_separate:
                        is_better = True
                    elif best_audio_format and not is_separate_video and curr_is_separate:
                        is_better = False
                    elif fps > curr["fps"]:
                        is_better = True
                    elif fps == curr["fps"]:
                        if vcodec_score > curr_score:
                            is_better = True
                        elif vcodec_score == curr_score and (raw_sz > curr["raw_sz"] or tbr > curr["tbr"]):
                            is_better = True

                if is_better:
                    label = f"{h}p"
                    if fps and fps > 30:
                        label += f" {int(fps)}fps"
                    if h >= 4320:
                        label += " (8K Ultra HD)"
                    elif h >= 2160:
                        label += " (4K Ultra HD)"
                    elif h >= 1440:
                        label += " (2K Quad HD)"
                    elif h >= 1080:
                        label += " (Full HD)"
                    elif h >= 720:
                        label += " (HD)"
                    else:
                        label += " (SD)"

                    tier_map[h] = {
                        "label": label,
                        "height": h,
                        "fps": fps,
                        "tbr": tbr,
                        "vcodec_score": vcodec_score,
                        "is_separate_video": is_separate_video,
                        "quality": str(h),
                        "format": "MP4",
                        "raw_sz": raw_sz,
                        "filesize": total_sz,
                        "url": url
                    }

        video_formats = list(tier_map.values())
        video_formats.sort(key=lambda x: x["height"], reverse=True)

        if has_audio:
            video_formats.append({
                "label": "Audio Only (MP3)",
                "height": 0,
                "fps": 0,
                "tbr": 0,
                "quality": "audio",
                "format": "MP3",
                "raw_sz": best_audio_sz,
                "filesize": best_audio_sz,
                "url": url
            })

        return video_formats

    @classmethod
    def probe_media_info(cls, url: str, quality: Optional[str] = None) -> Dict[str, Any]:
        """Fast metadata probe to retrieve authentic video title, filename, and exact or estimated filesize."""
        if not cls.is_ytdlp_available() or not url:
            return {"title": "", "filename": "", "filesize": 0}

        now = time.time()
        with cls._cache_lock:
            if url in cls._media_info_cache:
                ts, cached_info = cls._media_info_cache[url]
                if now - ts < cls._CACHE_TTL:
                    tier_formats = cached_info.get("formats", [])
                    clean_title = cached_info.get("title", "")
                    ext = cached_info.get("ext") or "mp4"
                    duration = cached_info.get("duration") or 0
                    chosen_size = 0
                    if quality:
                        q_str = str(quality).lower().strip()
                        q_digits = "".join(filter(str.isdigit, q_str))
                        is_audio_q = "audio" in q_str or "mp3" in q_str
                        for tf in tier_formats:
                            if is_audio_q and tf["quality"] == "audio":
                                chosen_size = tf["filesize"]
                                ext = "mp3"
                                break
                            elif tf["quality"] == q_str:
                                chosen_size = tf["filesize"]
                                break
                            elif q_digits and (tf["quality"] == q_digits or str(tf.get("height", "")) == q_digits):
                                chosen_size = tf["filesize"]
                                break
                    if chosen_size <= 0 and tier_formats:
                        chosen_size = tier_formats[0]["filesize"]
                    filename = f"{clean_title}.{ext}" if clean_title else ""
                    return {
                        "title": clean_title,
                        "filename": filename,
                        "filesize": chosen_size,
                        "duration": duration,
                        "formats": tier_formats
                    }

        bin_name = resolve_binary("yt-dlp") or resolve_binary("youtube-dl") or "yt-dlp"
        cmd = [
            bin_name,
            "-J",
            "--no-playlist",
            "--no-check-certificates",
            "--geo-bypass",
        ]
        if "yt-dlp" in bin_name.lower():
            cmd.extend(["--compat-options", "allow-unsafe-ext", "--remote-components", "ejs:github"])
        cmd.extend(cls._get_js_runtime_args())
        cmd.extend(cls._get_extractor_args(url))
        cmd.append(url)

        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
            if res.returncode != 0 or not res.stdout:
                return {"title": "", "filename": "", "filesize": 0}

            data = json.loads(res.stdout)
            raw_title = data.get("title") or ""
            clean_title = re.sub(r'[\\/:*?"<>|]', '_', raw_title).strip()
            ext = data.get("ext") or "mp4"
            duration = data.get("duration") or 0

            tier_formats = cls._parse_formats_and_tiers(data, url)
            with cls._cache_lock:
                cls._media_info_cache[url] = (now, {
                    "title": clean_title,
                    "ext": ext,
                    "duration": duration,
                    "formats": tier_formats
                })

            chosen_size = 0
            if quality:
                q_str = str(quality).lower().strip()
                q_digits = "".join(filter(str.isdigit, q_str))
                is_audio_q = "audio" in q_str or "mp3" in q_str
                for tf in tier_formats:
                    if is_audio_q and tf["quality"] == "audio":
                        chosen_size = tf["filesize"]
                        ext = "mp3"
                        break
                    elif tf["quality"] == q_str:
                        chosen_size = tf["filesize"]
                        break
                    elif q_digits and (tf["quality"] == q_digits or str(tf.get("height", "")) == q_digits):
                        chosen_size = tf["filesize"]
                        break

            if chosen_size <= 0 and tier_formats:
                chosen_size = tier_formats[0]["filesize"]

            filename = f"{clean_title}.{ext}" if clean_title else ""
            return {
                "title": clean_title,
                "filename": filename,
                "filesize": chosen_size,
                "duration": duration,
                "formats": tier_formats
            }
        except Exception:
            return {"title": "", "filename": "", "filesize": 0}

    def log(self, msg: str):
        if self.on_log:
            try:
                self.on_log(self.download_id, msg)
            except Exception:
                pass

    def start(self):
        with self._lock:
            if self.status == "downloading":
                return
            self.status = "downloading"
            self._stop_event.clear()
            self._pause_event.clear()

        threading.Thread(target=self._run_ytdlp, daemon=True).start()

    def pause(self):
        with self._lock:
            self.status = "paused"
            self._pause_event.set()
            if self._process:
                try:
                    self._process.terminate()
                except Exception:
                    pass

    def resume(self):
        self.start()

    def cancel(self):
        with self._lock:
            self.status = "cancelled"
            self._stop_event.set()
            if self._process:
                try:
                    self._process.kill()
                except Exception:
                    pass

    def _resolve_initial_size_if_needed(self):
        """Auto-probe media info on start if total_bytes is 0 or uninitialized."""
        if self.total_bytes <= 0 and self.url and self.is_ytdlp_available():
            try:
                info = self.probe_media_info(self.url, quality=self.quality or self.headers.get("quality"))
                if info and info.get("filesize", 0) > 0:
                    self.total_bytes = info["filesize"]
                    self._initial_total_bytes = self.total_bytes
            except Exception:
                pass

    def _run_ytdlp(self):
        self._resolve_initial_size_if_needed()
        if self._initial_total_bytes <= 0 and self.total_bytes > 0:
            self._initial_total_bytes = self.total_bytes
        self._completed_streams_bytes = 0
        self._current_stream_total = 0
        self._current_stream_downloaded = 0
        self._last_stream_pct = 0.0

        bin_name = resolve_binary("yt-dlp") or resolve_binary("youtube-dl") or "yt-dlp"
        dest_dir = os.path.dirname(self.save_path)
        os.makedirs(dest_dir, exist_ok=True)

        cmd = [
            bin_name,
            "--newline",
            "--progress",
            "-N", "8",
            "--no-playlist",
            "--no-check-certificates",
            "--geo-bypass",
            "-o", self.save_path,
        ]
        if "yt-dlp" in bin_name.lower():
            cmd.extend(["--compat-options", "allow-unsafe-ext", "--remote-components", "ejs:github"])

        cmd.extend(self._get_js_runtime_args())
        cmd.extend(self._get_extractor_args(self.url))

        # Automatic container merging via ffmpeg if available
        ffmpeg_bin = resolve_binary("ffmpeg")
        if ffmpeg_bin:
            cmd.extend(["--ffmpeg-location", ffmpeg_bin])

        # Quality and format selection with resilient video+audio pairing
        q_str = str(self.quality or self.headers.get("quality", "")).lower().strip()
        is_audio = "audio" in q_str or "mp3" in q_str or self.save_path.lower().endswith((".mp3", ".m4a", ".aac"))
        is_webm = self.save_path.lower().endswith(".webm")

        if is_audio:
            if ffmpeg_bin:
                cmd.extend(["-f", "bestaudio/best", "-x", "--audio-format", "mp3"])
            else:
                cmd.extend(["-f", "bestaudio/best"])
        elif ffmpeg_bin:
            height = "".join(filter(str.isdigit, q_str)) if q_str else ""
            if is_webm:
                # WebM containers require Opus or Vorbis audio; do not force AAC or MP4 muxing
                if height:
                    cmd.extend(["-f", f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"])
                else:
                    cmd.extend(["-f", "bestvideo+bestaudio/best"])
            else:
                # When ffmpeg is available and container is MP4 (or default), prioritize native AAC/m4a audio
                # to guarantee full sound playback compatibility across all OSes (Windows Media Player, QuickTime, mobile, TV)
                if height:
                    cmd.extend([
                        "-f",
                        f"bestvideo[height<={height}]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio[acodec^=mp4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
                    ])
                else:
                    cmd.extend([
                        "-f",
                        "bestvideo+bestaudio[ext=m4a]/bestvideo+bestaudio[acodec^=mp4a]/bestvideo+bestaudio/best"
                    ])
                cmd.extend(["-S", "acodec:m4a,acodec:aac"])
                if not self.save_path.lower().endswith((".mkv",)):
                    cmd.extend(["--merge-output-format", "mp4"])
        else:
            # Without ffmpeg, yt-dlp cannot merge separate video + audio streams.
            # Requesting bestvideo+bestaudio without ffmpeg results in unmerged files with silent video.
            # Fall back to single pre-muxed format containing both video and audio.
            height = "".join(filter(str.isdigit, q_str)) if q_str else ""
            if height:
                cmd.extend(["-f", f"best[height<={height}]/best"])
            else:
                cmd.extend(["-f", "best"])

        # Add User-Agent, Referer, and Cookies if present (case-insensitive per RFC 7230)
        if self.headers:
            user_agent = next((v for k, v in self.headers.items() if k.lower() == "user-agent"), None)
            if user_agent:
                cmd.extend(["--user-agent", user_agent])
            referer = next((v for k, v in self.headers.items() if k.lower() == "referer"), None)
            if referer:
                cmd.extend(["--referer", referer])
            cookie = next((v for k, v in self.headers.items() if k.lower() == "cookie"), None)
            if cookie:
                cmd.extend(["--add-headers", f"Cookie:{cookie}"])

        cmd.append(self.url)

        self.log(f"Starting video stream download with {bin_name} (quality: {q_str or 'best'})...")

        if self.on_progress:
            try:
                self.on_progress(self.download_id, {
                    "download_id": self.download_id,
                    "status": "downloading",
                    "speed": 0,
                    "eta": 0,
                    "downloaded_bytes": 0,
                    "total_bytes": self.total_bytes,
                    "progress_pct": 0.0
                })
            except Exception:
                pass

        last_error_line = ""
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            # Parse yt-dlp output for progress
            # e.g.: [download]  45.2% of  120.50MiB at  4.52MiB/s ETA 00:14
            for line in self._process.stdout:
                line = line.strip()
                if "ERROR:" in line or "error:" in line.lower():
                    last_error_line = line
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                self._parse_line(line)

            self._process.wait()
            ret_code = self._process.returncode

            if self._stop_event.is_set() or self._pause_event.is_set() or self.status in ["paused", "cancelled"]:
                return

            if ret_code == 0 and self.status not in ["paused", "cancelled"]:
                self.status = "completed"
                # Check actual downloaded file
                final_path = self.save_path
                if not os.path.exists(final_path):
                    # Check if yt-dlp changed extension (e.g. .webm -> .mkv / .mp4)
                    base, _ = os.path.splitext(self.save_path)
                    for ext in [".mp4", ".mkv", ".webm", ".mp3", ".m4a"]:
                        if os.path.exists(base + ext):
                            final_path = base + ext
                            break

                file_size = os.path.getsize(final_path) if os.path.exists(final_path) else self.total_bytes
                self.downloaded_bytes = file_size
                self.total_bytes = file_size

                if self.on_progress:
                    self.on_progress(self.download_id, {
                        "status": "completed",
                        "downloaded_bytes": file_size,
                        "total_bytes": file_size,
                        "speed": 0,
                        "eta": 0,
                        "save_path": final_path
                    })

                if self.on_complete:
                    self.on_complete(self.download_id, final_path)
            else:
                self.status = "error"
                err_msg = last_error_line or f"yt-dlp exited with code {ret_code}"
                if self.on_error:
                    self.on_error(self.download_id, err_msg)

        except Exception as e:
            self.status = "error"
            self.log(f"yt-dlp execution error: {e}")
            if self.on_error:
                self.on_error(self.download_id, str(e))

    def _parse_line(self, line: str):
        if not line:
            return

        # Detect merger / assembling phase
        if any(marker in line for marker in ["[Merger]", "[Fixup", "[VideoConvertor]", "[ExtractAudio]", "Merging formats into"]):
            self.status = "assembling"
            self.speed = 0.0
            self.eta = 0.0
            if self.total_bytes > 0:
                self.downloaded_bytes = self.total_bytes
            if self.on_progress:
                self.on_progress(self.download_id, {
                    "status": "assembling",
                    "downloaded_bytes": self.downloaded_bytes,
                    "total_bytes": self.total_bytes,
                    "speed": 0.0,
                    "eta": 0.0,
                    "save_path": self.save_path
                })
            return

        # Check for destination / new stream header transition
        if "[download] Destination:" in line:
            if self._current_stream_total > 0:
                self._completed_streams_bytes += self._current_stream_total
                self._current_stream_total = 0
                self._current_stream_downloaded = 0
                self._last_stream_pct = 0.0

        # [download]  45.2% of  120.50MiB at  4.52MiB/s ETA 00:14
        pct_match = re.search(r"(\d+(?:\.\d+)?)%", line)
        if pct_match:
            pct = float(pct_match.group(1))

            # Detect stream transition if percentage resets (e.g. from 100% to 0% for audio)
            if pct < (self._last_stream_pct - 20.0) and self._current_stream_total > 0:
                self._completed_streams_bytes += self._current_stream_total
                self._current_stream_total = 0
                self._current_stream_downloaded = 0

            self._last_stream_pct = pct

            size_match = re.search(r"of\s+(~)?\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)", line, re.I)
            if size_match:
                is_approx = bool(size_match.group(1))
                val = float(size_match.group(2))
                unit = size_match.group(3).upper()
                mult = {"B": 1, "KB": 1024, "KIB": 1024, "MB": 1024**2, "MIB": 1024**2, "GB": 1024**3, "GIB": 1024**3}.get(unit, 1024**2)
                self._current_stream_total = int(val * mult)
                self._current_stream_downloaded = int((pct / 100.0) * self._current_stream_total)

                self.downloaded_bytes = self._completed_streams_bytes + self._current_stream_downloaded
                if self._completed_streams_bytes > 0:
                    self.total_bytes = self._completed_streams_bytes + self._current_stream_total
                elif is_approx:
                    if self._initial_total_bytes > 0 and self.downloaded_bytes <= self._initial_total_bytes:
                        self.total_bytes = self._initial_total_bytes
                    else:
                        self.total_bytes = self._current_stream_total
                elif self.total_bytes <= 0:
                    self.total_bytes = self._current_stream_total
                else:
                    self.total_bytes = max(self.total_bytes, self._current_stream_total)

                if self.downloaded_bytes > self.total_bytes:
                    self.total_bytes = self.downloaded_bytes

            speed_match = re.search(r"at\s+(\d+(?:\.\d+)?)\s*([KMGT]?i?B)/s", line, re.I)
            if speed_match:
                s_val = float(speed_match.group(1))
                s_unit = speed_match.group(2).upper()
                s_mult = {"B": 1, "KB": 1024, "KIB": 1024, "MB": 1024**2, "MIB": 1024**2, "GB": 1024**3, "GIB": 1024**3}.get(s_unit, 1024**2)
                self.speed = s_val * s_mult

            eta_match = re.search(r"ETA\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", line)
            if eta_match:
                if eta_match.group(3):
                    self.eta = int(eta_match.group(1)) * 3600 + int(eta_match.group(2)) * 60 + int(eta_match.group(3))
                else:
                    self.eta = int(eta_match.group(1)) * 60 + int(eta_match.group(2))

            self.status = "downloading"
            if self.on_progress:
                self.on_progress(self.download_id, {
                    "status": "downloading",
                    "downloaded_bytes": self.downloaded_bytes,
                    "total_bytes": self.total_bytes,
                    "speed": self.speed,
                    "eta": self.eta,
                    "save_path": self.save_path
                })
