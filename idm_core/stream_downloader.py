"""
HLS (.m3u8) and DASH (.mpd) Stream Parser and Multi-Threaded Video Downloader
"""

import math
import os
import re
import shutil
import subprocess
import threading
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional
from idm_core.config import Config
from idm_core.platform import resolve_binary
from idm_core.speed_limiter import SpeedLimiter
from idm_core.storage import StorageManager


class HLSParser:
    def __init__(self, m3u8_url: str, headers: Optional[Dict[str, str]] = None):
        self.m3u8_url = m3u8_url
        self.headers = headers or {}
        self.key_info = None  # Method, URI, IV

    def _resolve_url(self, base_url: str, relative_url: str) -> str:
        base_clean = (base_url or "").strip()
        rel_clean = (relative_url or "").strip()
        joined = urllib.parse.urljoin(base_clean, rel_clean)
        parsed_src = urllib.parse.urlparse(self.m3u8_url)
        parsed_joined = urllib.parse.urlparse(joined)
        if parsed_src.query and not parsed_joined.query:
            joined = urllib.parse.urlunparse(parsed_joined._replace(query=parsed_src.query))
        return joined

    def parse(self) -> List[str]:
        """Fetch and parse M3U8 manifest, returning list of resolved segment URLs."""
        content = self._fetch_text(self.m3u8_url)
        lines = [line.strip() for line in content.splitlines() if line.strip()]

        # Check if master playlist
        if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
            variant_url = self._select_best_variant(lines, self.m3u8_url)
            content = self._fetch_text(variant_url)
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            base_url = variant_url
        else:
            base_url = self.m3u8_url

        segments = []
        for line in lines:
            if line.startswith("#"):
                if line.startswith("#EXT-X-KEY"):
                    self.key_info = line
                elif line.startswith("#EXT-X-MAP"):
                    m = re.search(r'URI=["\']([^"\']+)["\']', line)
                    if m:
                        init_url = self._resolve_url(base_url, m.group(1))
                        segments.append(init_url)
                continue
            # Resolved segment URL
            seg_url = self._resolve_url(base_url, line)
            segments.append(seg_url)

        return segments

    def _select_best_variant(self, lines: List[str], base_url: str) -> str:
        """Find the matching or highest bitrate/resolution variant stream in a master playlist."""
        quality_req = (self.headers.get("quality") or "best").lower() if self.headers else "best"
        target_height = None
        if quality_req != "best" and quality_req != "audio":
            m = re.search(r"\d+", quality_req)
            if m:
                target_height = int(m.group(0))

        best_bw = -1
        best_url = None
        target_url = None
        target_bw = -1

        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                match = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(match.group(1)) if match else 0
                res_match = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                h = int(res_match.group(2)) if res_match else 0

                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    variant_url = self._resolve_url(base_url, lines[i + 1])
                    if target_height and h == target_height:
                        if bw > target_bw or target_url is None:
                            target_bw = bw
                            target_url = variant_url
                    if bw > best_bw or best_url is None:
                        best_bw = bw
                        best_url = variant_url

        if target_url:
            return target_url
        return best_url or base_url

    def _fetch_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    @classmethod
    def probe_stream_info(cls, m3u8_url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Estimate total stream duration, bitrate, and filesize for an HLS playlist."""
        try:
            parser = cls(m3u8_url, headers=headers)
            content = parser._fetch_text(m3u8_url)
            lines = [line.strip() for line in content.splitlines() if line.strip()]

            bandwidth = 0
            variant_url = m3u8_url
            is_master = any(line.startswith("#EXT-X-STREAM-INF") for line in lines)
            first_variant_url = None

            if is_master:
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
                        match = re.search(r"BANDWIDTH=(\d+)", line)
                        bw = int(match.group(1)) if match else 0
                        if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                            variant_candidate = parser._resolve_url(m3u8_url, lines[i + 1])
                            if first_variant_url is None:
                                first_variant_url = variant_candidate
                            if bw > bandwidth:
                                bandwidth = bw
                                variant_url = variant_candidate
                # If no BANDWIDTH found, use first variant for duration probing
                if variant_url == m3u8_url and first_variant_url:
                    variant_url = first_variant_url
                if variant_url != m3u8_url:
                    content = parser._fetch_text(variant_url)
                    lines = [line.strip() for line in content.splitlines() if line.strip()]

            total_duration = 0.0
            for line in lines:
                if line.startswith("#EXTINF:"):
                    match = re.search(r"#EXTINF:([\d.]+)", line)
                    if match:
                        total_duration += float(match.group(1))

            # Check if this is a live stream (no EXT-X-ENDLIST in media playlist)
            # For master playlist, only check if we successfully fetched a variant
            is_live = False
            if variant_url != m3u8_url:
                is_live = not any(line == "#EXT-X-ENDLIST" for line in lines)
            elif not is_master:
                # Media playlist (not master, no variant fetched)
                is_live = not any(line == "#EXT-X-ENDLIST" for line in lines)

            used_fallback = False
            if not bandwidth:
                bandwidth = 2500000  # Default fallback 2.5 Mbps
                used_fallback = True

            estimated_size = int((bandwidth / 8) * total_duration) if total_duration > 0 else 0
            is_approx = total_duration <= 0 or used_fallback or is_live
            return {
                "duration": total_duration,
                "bandwidth": bandwidth,
                "filesize": estimated_size,
                "filesize_approx": is_approx
            }
        except (urllib.error.URLError, ValueError, ET.ParseError) as e:
            return {"duration": 0, "bandwidth": 0, "filesize": 0, "filesize_approx": True}

    @classmethod
    def extract_formats(cls, m3u8_url: str, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Extract all available video variants from an HLS master playlist."""
        try:
            parser = cls(m3u8_url, headers=headers)
            content = parser._fetch_text(m3u8_url)
            lines = [line.strip() for line in content.splitlines() if line.strip()]

            formats = []
            seen_heights = set()

            if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
                # Master playlist - extract all variants
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
                        bandwidth_match = re.search(r"BANDWIDTH=(\d+)", line)
                        bw = int(bandwidth_match.group(1)) if bandwidth_match else 0

                        resolution_match = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                        width = int(resolution_match.group(1)) if resolution_match else 0
                        height = int(resolution_match.group(2)) if resolution_match else 0

                        fps = 30
                        fps_match = re.search(r"FRAME-RATE=([\d.]+)", line)
                        if fps_match:
                            try:
                                fps = float(fps_match.group(1))
                            except Exception:
                                pass

                        if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                            variant_url = parser._resolve_url(m3u8_url, lines[i + 1])

                            if height and height not in seen_heights:
                                seen_heights.add(height)
                                label = f"{height}p"
                                if fps and fps > 30:
                                    label += f" {int(fps)}fps"
                                if height >= 4320:
                                    label += " (8K Ultra HD)"
                                elif height >= 2160:
                                    label += " (4K Ultra HD)"
                                elif height >= 1440:
                                    label += " (2K Quad HD)"
                                elif height >= 1080:
                                    label += " (Full HD)"
                                elif height >= 720:
                                    label += " (HD)"
                                else:
                                    label += " (SD)"

                                # Estimate duration from variant if possible
                                duration = 0.0
                                is_live = False
                                try:
                                    variant_content = parser._fetch_text(variant_url)
                                    variant_lines = [l.strip() for l in variant_content.splitlines() if l.strip()]
                                    for vl in variant_lines:
                                        if vl.startswith("#EXTINF:"):
                                            match = re.search(r"#EXTINF:([\d.]+)", vl)
                                            if match:
                                                duration += float(match.group(1))
                                    # Live stream detection: no EXT-X-ENDLIST in variant playlist
                                    is_live = not any(l == "#EXT-X-ENDLIST" for l in variant_lines)
                                except Exception:
                                    pass

                                is_approx = duration <= 0 or bw <= 0 or is_live
                                fmt = {
                                    "label": label,
                                    "height": height,
                                    "width": width,
                                    "fps": fps,
                                    "bandwidth": bw,
                                    "quality": str(height),
                                    "format": "MP4",
                                    "filesize": int((bw / 8) * duration) if duration > 0 else 0,
                                    "filesize_approx": is_approx,
                                    "url": m3u8_url,
                                    "mime": "video/mp2t",
                                    "lang": "",
                                    "roles": []
                                }
                                formats.append(fmt)
            else:
                # Media playlist - single quality
                total_duration = 0.0
                for line in lines:
                    if line.startswith("#EXTINF:"):
                        match = re.search(r"#EXTINF:([\d.]+)", line)
                        if match:
                            total_duration += float(match.group(1))

                # Live stream detection: no EXT-X-ENDLIST in media playlist
                is_live = not any(line == "#EXT-X-ENDLIST" for line in lines)
                # Media playlists lack BANDWIDTH info, so size is always approximate
                is_approx = True
                fmt = {
                    "label": "Best Quality",
                    "height": 0,
                    "width": 0,
                    "fps": 0,
                    "bandwidth": 0,
                    "quality": "best",
                    "format": "MP4",
                    "filesize": int((0 / 8) * total_duration) if total_duration > 0 else 0,
                    "filesize_approx": is_approx,
                    "url": m3u8_url,
                    "mime": "video/mp2t",
                    "lang": "",
                    "roles": []
                }
                formats.append(fmt)

            formats.sort(key=lambda x: x.get("height", 0), reverse=True)
            return formats
        except (urllib.error.URLError, ValueError, ET.ParseError):
            return []


class DASHParser:
    def __init__(self, mpd_url: str, headers: Optional[Dict[str, str]] = None):
        self.mpd_url = mpd_url
        self.headers = headers or {}

    @staticmethod
    def parse_iso8601_duration(duration_str: str) -> float:
        """Parse ISO 8601 duration string into seconds (e.g., PT2H25M35.042S -> 8735.042)."""
        if not duration_str:
            return 0.0
        pattern = r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$"
        match = re.match(pattern, duration_str)
        if match:
            years, months, weeks, days, hours, minutes, seconds = match.groups()
            total = 0.0
            if years:
                total += float(years) * 31536000.0
            if months:
                total += float(months) * 2592000.0
            if weeks:
                total += float(weeks) * 604800.0
            if days:
                total += float(days) * 86400.0
            if hours:
                total += float(hours) * 3600.0
            if minutes:
                total += float(minutes) * 60.0
            if seconds:
                total += float(seconds)
            return total

        # Fallback splitting by T to avoid Month vs Minute ambiguity
        if "T" in duration_str:
            date_part, time_part = duration_str.split("T", 1)
        else:
            date_part, time_part = duration_str, ""

        years = re.search(r"(\d+)Y", date_part)
        months = re.search(r"(\d+)M", date_part)
        weeks = re.search(r"(\d+)W", date_part)
        days = re.search(r"(\d+)D", date_part)
        hours = re.search(r"(\d+)H", time_part)
        minutes = re.search(r"(\d+)M", time_part)
        seconds = re.search(r"([\d.]+)S", time_part)

        total = 0.0
        if years:
            total += float(years.group(1)) * 31536000.0
        if months:
            total += float(months.group(1)) * 2592000.0
        if weeks:
            total += float(weeks.group(1)) * 604800.0
        if days:
            total += float(days.group(1)) * 86400.0
        if hours:
            total += float(hours.group(1)) * 3600.0
        if minutes:
            total += float(minutes.group(1)) * 60.0
        if seconds:
            total += float(seconds.group(1))
        return total

    @staticmethod
    def _local_tag(elem) -> str:
        """Strip XML namespace from tag name."""
        return elem.tag.split("}")[-1]

    @staticmethod
    def _format_template(
        template: str,
        rep_id: str = "",
        number: Optional[int] = None,
        time_val: Optional[int] = None,
        bandwidth: Optional[int] = None
    ) -> str:
        """Substitute DASH SegmentTemplate format identifiers ($RepresentationID$, $Number$, $Time$, $Bandwidth$)."""
        res = template.replace("$$", "\x00")
        res = res.replace("$RepresentationID$", str(rep_id))
        if bandwidth is not None:
            def bw_repl(m):
                fmt = m.group(1)
                if fmt:
                    try:
                        return f"%{fmt}" % int(bandwidth)
                    except Exception:
                        return str(bandwidth)
                return str(bandwidth)
            res = re.sub(r"\$Bandwidth(?:%([^$]+))?\$", bw_repl, res)
        if number is not None:
            def num_repl(m):
                fmt = m.group(1)
                if fmt:
                    try:
                        return f"%{fmt}" % int(number)
                    except Exception:
                        return str(number)
                return str(number)
            res = re.sub(r"\$Number(?:%([^$]+))?\$", num_repl, res)
        if time_val is not None:
            def time_repl(m):
                fmt = m.group(1)
                if fmt:
                    try:
                        return f"%{fmt}" % int(time_val)
                    except Exception:
                        return str(time_val)
                return str(time_val)
            res = re.sub(r"\$Time(?:%([^$]+))?\$", time_repl, res)
        res = res.replace("\x00", "$")
        return res

    def _resolve_url(self, base_url: str, relative_url: str) -> str:
        """Resolve a relative URL against base_url while preserving authentication query parameters."""
        base_clean = (base_url or "").strip()
        rel_clean = (relative_url or "").strip()
        joined = urllib.parse.urljoin(base_clean, rel_clean)
        mpd_parsed = urllib.parse.urlparse(self.mpd_url)
        joined_parsed = urllib.parse.urlparse(joined)
        if mpd_parsed.query and not joined_parsed.query:
            joined = urllib.parse.urlunparse(joined_parsed._replace(query=mpd_parsed.query))
        return joined

    def _fetch_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def parse_tracks(self) -> Dict[str, Any]:
        """Parse DASH MPD XML and extract video and audio track segments and metadata."""
        content = self._fetch_text(self.mpd_url)
        root = ET.fromstring(content.encode("utf-8"))
        total_duration = self.parse_iso8601_duration(root.attrib.get("mediaPresentationDuration", ""))

        base_url = self.mpd_url
        for child in root:
            if self._local_tag(child) == "BaseURL" and child.text:
                base_url = self._resolve_url(base_url, child.text.strip())

        periods = [elem for elem in root if self._local_tag(elem) == "Period"]
        if not periods:
            periods = [root]

        all_video_segs: List[str] = []
        all_audio_segs: List[str] = []
        best_video_bw = 0
        best_audio_bw = 0

        for period in periods:
            period_duration_str = period.attrib.get("duration", "")
            period_duration = self.parse_iso8601_duration(period_duration_str) if period_duration_str else total_duration
            p_base_url = base_url
            for child in period:
                if self._local_tag(child) == "BaseURL" and child.text:
                    p_base_url = self._resolve_url(p_base_url, child.text.strip())

            period_tmpl = next((elem for elem in period if self._local_tag(elem) == "SegmentTemplate"), None)

            video_adaptations = []
            audio_adaptations = []

            adaptations = [elem for elem in period if self._local_tag(elem) == "AdaptationSet"]
            for ad in adaptations:
                mime = ad.attrib.get("mimeType", "").lower()
                c_type = ad.attrib.get("contentType", "").lower()
                lang = ad.attrib.get("lang", "")
                ad_base = p_base_url
                for child in ad:
                    if self._local_tag(child) == "BaseURL" and child.text:
                        ad_base = self._resolve_url(ad_base, child.text.strip())

                ad_tmpl = next((elem for elem in ad if self._local_tag(elem) == "SegmentTemplate"), period_tmpl)
                ad_list = next((elem for elem in ad if self._local_tag(elem) == "SegmentList"), None)
                roles = [c.attrib.get("value", "") for c in ad if self._local_tag(c) == "Role"]

                reps = [r for r in ad if self._local_tag(r) == "Representation"]
                is_video = mime.startswith("video") or c_type == "video" or any(r.attrib.get("width") for r in reps)
                is_audio = mime.startswith("audio") or c_type == "audio" or any(r.attrib.get("audioSamplingRate") for r in reps)

                info = {
                    "adaptation_elem": ad,
                    "base_url": ad_base,
                    "template": ad_tmpl,
                    "segment_list": ad_list,
                    "mime": mime,
                    "lang": lang,
                    "roles": roles,
                    "reps": reps,
                    "period_duration": period_duration
                }
                if is_video:
                    video_adaptations.append(info)
                elif is_audio:
                    audio_adaptations.append(info)

            # Quality selection filter (e.g., '720', '480', 'audio', 'best')
            quality_req = (self.headers.get("quality") or "best").lower() if self.headers else "best"
            target_height = None
            if quality_req != "best" and quality_req != "audio":
                m = re.search(r"\d+", quality_req)
                if m:
                    target_height = int(m.group(0))

            # Select video representation in this period
            p_video_rep = None
            p_video_info = None
            p_best_v_bw = -1
            target_v_rep = None
            target_v_info = None
            target_v_bw = -1

            if quality_req != "audio":
                for v_ad in video_adaptations:
                    for rep in v_ad["reps"]:
                        bw = int(rep.attrib.get("bandwidth", 0))
                        h = int(rep.attrib.get("height", 0) or v_ad["adaptation_elem"].attrib.get("height", 0) or 0)
                        if target_height and h == target_height:
                            if bw > target_v_bw or target_v_rep is None:
                                target_v_bw = bw
                                target_v_rep = rep
                                target_v_info = v_ad
                        if bw > p_best_v_bw or p_video_rep is None:
                            p_best_v_bw = bw
                            p_video_rep = rep
                            p_video_info = v_ad

                if target_v_rep is not None:
                    p_video_rep = target_v_rep
                    p_video_info = target_v_info
                    p_best_v_bw = target_v_bw

                if p_best_v_bw > best_video_bw:
                    best_video_bw = p_best_v_bw

            # Select best audio representation in this period (prefer role='main' or first audio adaptation)
            p_audio_rep = None
            p_audio_info = None
            p_best_a_bw = -1
            audio_adaptations.sort(key=lambda a: 0 if "main" in a["roles"] else 1)
            if audio_adaptations:
                target_audio_ad = audio_adaptations[0]
                for rep in target_audio_ad["reps"]:
                    bw = int(rep.attrib.get("bandwidth", 0))
                    if bw > p_best_a_bw or p_audio_rep is None:
                        p_best_a_bw = bw
                        p_audio_rep = rep
                        p_audio_info = target_audio_ad
            if p_best_a_bw > best_audio_bw:
                best_audio_bw = p_best_a_bw

            def extract_segments(rep, ad_info):
                if rep is None or ad_info is None:
                    return []
                segments = []
                rep_base = ad_info["base_url"]
                for child in rep:
                    if self._local_tag(child) == "BaseURL" and child.text:
                        rep_base = self._resolve_url(rep_base, child.text.strip())

                tmpl = next((elem for elem in rep if self._local_tag(elem) == "SegmentTemplate"), ad_info["template"])
                s_list = next((elem for elem in rep if self._local_tag(elem) == "SegmentList"), ad_info["segment_list"])
                rep_id = rep.attrib.get("id", "")
                rep_bw = int(rep.attrib.get("bandwidth", 0))

                if tmpl is not None:
                    timescale = int(tmpl.attrib.get("timescale", 1))
                    duration = int(tmpl.attrib.get("duration", 0))
                    start_num = int(tmpl.attrib.get("startNumber", 1))
                    init_tmpl = tmpl.attrib.get("initialization", "")
                    media_tmpl = tmpl.attrib.get("media", "")

                    if init_tmpl:
                        init_rel = self._format_template(init_tmpl, rep_id=rep_id, bandwidth=rep_bw)
                        segments.append(self._resolve_url(rep_base, init_rel))

                    timeline = next((elem for elem in tmpl if self._local_tag(elem) == "SegmentTimeline"), None)
                    if timeline is not None:
                        curr_time = 0
                        curr_num = start_num
                        for s in [elem for elem in timeline if self._local_tag(elem) == "S"]:
                            if "t" in s.attrib:
                                curr_time = int(s.attrib["t"])
                            d = int(s.attrib.get("d", 0))
                            r = int(s.attrib.get("r", 0))
                            count = r + 1 if r >= 0 else 1
                            for _ in range(count):
                                seg_rel = self._format_template(media_tmpl, rep_id=rep_id, number=curr_num, time_val=curr_time, bandwidth=rep_bw)
                                segments.append(self._resolve_url(rep_base, seg_rel))
                                curr_time += d
                                curr_num += 1
                    elif duration > 0 and timescale > 0:
                        seg_dur_sec = duration / timescale
                        p_dur = ad_info["period_duration"] or total_duration
                        num_segs = max(1, math.ceil(p_dur / seg_dur_sec)) if p_dur > 0 else 1
                        for n in range(start_num, start_num + num_segs):
                            time_val = (n - start_num) * duration
                            seg_rel = self._format_template(media_tmpl, rep_id=rep_id, number=n, time_val=time_val, bandwidth=rep_bw)
                            segments.append(self._resolve_url(rep_base, seg_rel))

                elif s_list is not None:
                    init_elem = next((elem for elem in s_list if self._local_tag(elem) == "Initialization"), None)
                    if init_elem is not None and "sourceURL" in init_elem.attrib:
                        segments.append(self._resolve_url(rep_base, init_elem.attrib["sourceURL"]))
                    for seg_url_elem in [elem for elem in s_list if self._local_tag(elem) == "SegmentURL"]:
                        if "media" in seg_url_elem.attrib:
                            segments.append(self._resolve_url(rep_base, seg_url_elem.attrib["media"]))
                elif rep.text and rep.text.strip():
                    segments.append(self._resolve_url(rep_base, rep.text.strip()))

                return segments

            all_video_segs.extend(extract_segments(p_video_rep, p_video_info))
            all_audio_segs.extend(extract_segments(p_audio_rep, p_audio_info))

        total_bw = max(0, best_video_bw) + max(0, best_audio_bw)
        return {
            "duration": total_duration,
            "bandwidth": total_bw,
            "video_segments": all_video_segs,
            "audio_segments": all_audio_segs,
            "video_bandwidth": max(0, best_video_bw),
            "audio_bandwidth": max(0, best_audio_bw),
        }

    def parse(self) -> List[str]:
        """Fetch and parse MPD manifest, returning full list of video and audio segment URLs."""
        tracks = self.parse_tracks()
        return tracks.get("video_segments", []) + tracks.get("audio_segments", [])

    @classmethod
    def probe_stream_info(cls, mpd_url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Estimate total stream duration, bitrate, and filesize for a DASH manifest."""
        try:
            parser = cls(mpd_url, headers=headers)
            tracks = parser.parse_tracks()
            duration = tracks.get("duration", 0.0)
            bandwidth = tracks.get("bandwidth", 0)
            video_bw = tracks.get("video_bandwidth", 0)
            audio_bw = tracks.get("audio_bandwidth", 0)
            used_fallback = False
            if not bandwidth:
                bandwidth = 2500000  # Default fallback 2.5 Mbps
                used_fallback = True
            estimated_size = int((bandwidth / 8) * duration) if duration > 0 else 0
            # Mark as approximate only if duration unknown or fallback bandwidth used
            # (video-only or audio-only streams with known duration are NOT approximate)
            is_approx = duration <= 0 or used_fallback
            return {
                "duration": duration,
                "bandwidth": bandwidth,
                "filesize": estimated_size,
                "filesize_approx": is_approx
            }
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, ET.ParseError):
            return {"duration": 0, "bandwidth": 0, "filesize": 0, "filesize_approx": True}

    @classmethod
    def extract_formats(cls, mpd_url: str, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Extract all available video and audio representations from a DASH manifest.
        Video format filesizes include best audio track size to match actual download size.
        """
        try:
            parser = cls(mpd_url, headers=headers)
            content = parser._fetch_text(mpd_url)
            root = ET.fromstring(content.encode("utf-8"))
            total_duration = parser.parse_iso8601_duration(root.attrib.get("mediaPresentationDuration", ""))

            base_url = mpd_url
            for child in root:
                if parser._local_tag(child) == "BaseURL" and child.text:
                    base_url = parser._resolve_url(base_url, child.text.strip())

            periods = [elem for elem in root if parser._local_tag(elem) == "Period"]
            if not periods:
                periods = [root]

            formats = []
            seen_heights = set()

            # Collect all audio adaptations across periods to find best audio track
            all_audio_adaptations = []
            for period in periods:
                period_duration_str = period.attrib.get("duration", "")
                period_duration = parser.parse_iso8601_duration(period_duration_str) if period_duration_str else total_duration
                p_base_url = base_url
                for child in period:
                    if parser._local_tag(child) == "BaseURL" and child.text:
                        p_base_url = parser._resolve_url(p_base_url, child.text.strip())

                period_tmpl = next((elem for elem in period if parser._local_tag(elem) == "SegmentTemplate"), None)

                adaptations = [elem for elem in period if parser._local_tag(elem) == "AdaptationSet"]
                for ad in adaptations:
                    mime = ad.attrib.get("mimeType", "").lower()
                    c_type = ad.attrib.get("contentType", "").lower()
                    lang = ad.attrib.get("lang", "")
                    ad_base = p_base_url
                    for child in ad:
                        if parser._local_tag(child) == "BaseURL" and child.text:
                            ad_base = parser._resolve_url(ad_base, child.text.strip())

                    ad_tmpl = next((elem for elem in ad if parser._local_tag(elem) == "SegmentTemplate"), period_tmpl)
                    ad_list = next((elem for elem in ad if parser._local_tag(elem) == "SegmentList"), None)
                    roles = [c.attrib.get("value", "") for c in ad if parser._local_tag(c) == "Role"]

                    reps = [r for r in ad if parser._local_tag(r) == "Representation"]
                    is_audio = mime.startswith("audio") or c_type == "audio" or any(r.attrib.get("audioSamplingRate") for r in reps)

                    if is_audio:
                        # Find best audio representation in this adaptation (highest bandwidth)
                        best_audio_bw = 0
                        for rep in reps:
                            bw = int(rep.attrib.get("bandwidth", 0))
                            if bw > best_audio_bw:
                                best_audio_bw = bw
                        if best_audio_bw > 0:
                            all_audio_adaptations.append({
                                "bandwidth": best_audio_bw,
                                "roles": roles,
                                "lang": lang,
                                "period_duration": period_duration
                            })

            # Select best audio adaptation (prefer role='main', then highest bandwidth)
            best_audio = None
            if all_audio_adaptations:
                all_audio_adaptations.sort(key=lambda a: (0 if "main" in a["roles"] else 1, -a["bandwidth"]))
                best_audio = all_audio_adaptations[0]

            best_audio_bandwidth = best_audio["bandwidth"] if best_audio else 0
            # Use total_duration for size calculation to match actual download across all periods
            best_audio_filesize = int((best_audio_bandwidth / 8) * total_duration) if total_duration > 0 and best_audio_bandwidth > 0 else 0
            best_audio_is_approx = total_duration <= 0 or best_audio_bandwidth <= 0
            has_audio = best_audio is not None

            # Now process video adaptations and add audio size
            for period in periods:
                period_duration_str = period.attrib.get("duration", "")
                period_duration = parser.parse_iso8601_duration(period_duration_str) if period_duration_str else total_duration
                p_base_url = base_url
                for child in period:
                    if parser._local_tag(child) == "BaseURL" and child.text:
                        p_base_url = parser._resolve_url(p_base_url, child.text.strip())

                period_tmpl = next((elem for elem in period if parser._local_tag(elem) == "SegmentTemplate"), None)

                adaptations = [elem for elem in period if parser._local_tag(elem) == "AdaptationSet"]
                for ad in adaptations:
                    mime = ad.attrib.get("mimeType", "").lower()
                    c_type = ad.attrib.get("contentType", "").lower()
                    lang = ad.attrib.get("lang", "")
                    ad_base = p_base_url
                    for child in ad:
                        if parser._local_tag(child) == "BaseURL" and child.text:
                            ad_base = parser._resolve_url(ad_base, child.text.strip())

                    ad_tmpl = next((elem for elem in ad if parser._local_tag(elem) == "SegmentTemplate"), period_tmpl)
                    ad_list = next((elem for elem in ad if parser._local_tag(elem) == "SegmentList"), None)
                    roles = [c.attrib.get("value", "") for c in ad if parser._local_tag(c) == "Role"]

                    reps = [r for r in ad if parser._local_tag(r) == "Representation"]
                    is_video = mime.startswith("video") or c_type == "video" or any(r.attrib.get("width") for r in reps)
                    is_audio = mime.startswith("audio") or c_type == "audio" or any(r.attrib.get("audioSamplingRate") for r in reps)

                    if is_video:
                        for rep in reps:
                            height = rep.attrib.get("height")
                            width = rep.attrib.get("width")
                            bw = int(rep.attrib.get("bandwidth", 0))
                            if height and height not in seen_heights:
                                seen_heights.add(height)
                                h = int(height)
                                fps = 30
                                if "frameRate" in rep.attrib:
                                    try:
                                        fps = float(rep.attrib["frameRate"])
                                    except Exception:
                                        pass

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

                                # Use total_duration for size calculation to match actual download across all periods
                                video_filesize = int((bw / 8) * total_duration) if total_duration > 0 and bw > 0 else 0
                                video_is_approx = total_duration <= 0 or bw <= 0
                                # Combined size: video + best audio (if video is separate track and audio exists)
                                combined_filesize = video_filesize + (best_audio_filesize if has_audio else 0)
                                is_approx = video_is_approx or (best_audio_is_approx if has_audio else False)

                                fmt = {
                                    "label": label,
                                    "height": h,
                                    "width": int(width) if width else 0,
                                    "fps": fps,
                                    "bandwidth": bw,
                                    "quality": str(h),
                                    "format": "MP4",
                                    "filesize": combined_filesize,
                                    "filesize_approx": is_approx,
                                    "url": mpd_url,
                                    "mime": mime,
                                    "lang": lang,
                                    "roles": roles
                                }
                                formats.append(fmt)
                    elif is_audio:
                        for rep in reps:
                            bw = int(rep.attrib.get("bandwidth", 0))
                            sample_rate = rep.attrib.get("audioSamplingRate")
                            audio_ch = rep.attrib.get("audioChannels", "2")
                            label = "Audio Only"
                            if lang:
                                label += f" ({lang})"
                            # Use total_duration for size calculation to match actual download across all periods
                            audio_filesize = int((bw / 8) * total_duration) if total_duration > 0 and bw > 0 else 0
                            audio_is_approx = total_duration <= 0 or bw <= 0
                            fmt = {
                                "label": label,
                                "height": 0,
                                "width": 0,
                                "fps": 0,
                                "bandwidth": bw,
                                "quality": "audio",
                                "format": "MP3",
                                "filesize": audio_filesize,
                                "filesize_approx": audio_is_approx,
                                "url": mpd_url,
                                "mime": mime,
                                "lang": lang,
                                "roles": roles
                            }
                            formats.append(fmt)

            formats.sort(key=lambda x: (x["height"], x.get("fps", 0)), reverse=True)
            return formats
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, ET.ParseError):
            return []


class StreamDownloader:
    def __init__(
        self,
        download_id: str,
        url: str,
        save_path: str,
        storage: Optional[StorageManager] = None,
        config: Optional[Config] = None,
        num_connections: int = 8,
        speed_limit: int = 0,
        headers: Optional[Dict[str, str]] = None,
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
        self.storage = storage or StorageManager(self.config)
        self.num_connections = max(1, min(num_connections, 16))
        self.speed_limiter = SpeedLimiter(speed_limit)
        self.headers = headers or {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        }

        self.on_progress = on_progress
        self.on_segment_update = on_segment_update
        self.on_complete = on_complete
        self.on_error = on_error
        self.on_log = on_log

        self.status = "idle"  # idle, downloading, paused, completed, error
        self.total_segments = 0
        self.downloaded_segments = 0
        self.total_bytes = 0
        self.downloaded_bytes = 0
        self.video_segments: List[str] = []
        self.audio_segments: List[str] = []
        self.segment_urls: List[str] = []
        self.segment_files: List[str] = []

        self._workers: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._lock = threading.RLock()
        self._current_index = 0
        self._speed_bytes = 0
        self._speed_time = time.time()
        self.current_speed = 0.0

    @staticmethod
    def detect_stream_type(url: str, content: Optional[str] = None) -> str:
        """Identify if a URL or manifest content represents HLS (.m3u8) or DASH (.mpd)."""
        lower = url.lower()
        if ".mpd" in lower or "format=mpd" in lower or "type=mpd" in lower:
            return "dash"
        if ".m3u8" in lower or "format=m3u8" in lower or "type=m3u8" in lower:
            return "hls"
        if content:
            if "<MPD" in content or "urn:mpeg:dash" in content:
                return "dash"
            if "#EXTM3U" in content or "#EXT-X-" in content:
                return "hls"
        return "hls"

    @classmethod
    def probe_stream_info(cls, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Probe stream duration, bandwidth, and estimated filesize for HLS or DASH manifests."""
        stype = cls.detect_stream_type(url)
        if stype == "dash":
            return DASHParser.probe_stream_info(url, headers=headers)
        return HLSParser.probe_stream_info(url, headers=headers)

    @classmethod
    def extract_formats(cls, url: str, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Extract all available video and audio formats for HLS or DASH manifests."""
        stype = cls.detect_stream_type(url)
        if stype == "dash":
            return DASHParser.extract_formats(url, headers=headers)
        return HLSParser.extract_formats(url, headers=headers)

    def log(self, msg: str):
        if self.on_log:
            try:
                self.on_log(self.download_id, msg)
            except Exception:
                pass

    def start(self):
        """Parse stream manifest and launch concurrent segment workers."""
        with self._lock:
            if self.status == "downloading":
                return
            self.status = "downloading"
            self._stop_event.clear()
            self._pause_event.clear()

        threading.Thread(target=self._run_stream_engine, daemon=True).start()

    def _run_stream_engine(self):
        try:
            stream_type = self.detect_stream_type(self.url)
            if stream_type == "dash":
                self.log(f"Parsing DASH stream manifest {self.url}...")
                parser = DASHParser(self.url, self.headers)
                tracks = parser.parse_tracks()
                self.video_segments = tracks.get("video_segments", [])
                self.audio_segments = tracks.get("audio_segments", [])
                self.segment_urls = self.video_segments + self.audio_segments
                if tracks.get("duration", 0) > 0 and tracks.get("bandwidth", 0) > 0:
                    self.total_bytes = int((tracks["bandwidth"] / 8) * tracks["duration"])
            else:
                self.log(f"Parsing HLS stream manifest {self.url}...")
                parser = HLSParser(self.url, self.headers)
                self.video_segments = parser.parse()
                self.audio_segments = []
                self.segment_urls = self.video_segments
                probe = HLSParser.probe_stream_info(self.url, self.headers)
                if probe.get("filesize"):
                    self.total_bytes = probe["filesize"]

            self.total_segments = len(self.segment_urls)
            self.log(f"Extracted {self.total_segments} media segments (Video: {len(self.video_segments)}, Audio: {len(self.audio_segments)}).")

            if self.total_segments == 0:
                raise ValueError("No video/audio segments found in manifest.")

            self.segment_files = [
                self.storage.get_temp_segment_path(self.download_id, i)
                for i in range(self.total_segments)
            ]

            # Spawn worker threads
            self._current_index = 0
            self._workers.clear()
            for w_id in range(self.num_connections):
                t = threading.Thread(target=self._worker_loop, args=(w_id,), daemon=True)
                self._workers.append(t)
                t.start()

            for t in self._workers:
                t.join()

            if self._stop_event.is_set() or self._pause_event.is_set() or self.status in ["paused", "cancelled"]:
                return

            self._finalize_stream()

        except Exception as e:
            ffmpeg_bin = resolve_binary("ffmpeg")
            if ffmpeg_bin and not self._stop_event.is_set():
                self.log(f"Direct segment parsing encountered '{e}'. Falling back to ffmpeg stream capture...")
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(self.save_path)), exist_ok=True)
                    cmd = [ffmpeg_bin, "-y"]
                    if self.headers:
                        hdr_str = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items() if k.lower() in ["user-agent", "referer", "cookie"])
                        if hdr_str:
                            cmd.extend(["-headers", hdr_str])
                    cmd.extend(["-i", self.url, "-c", "copy", self.save_path])
                    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
                    if res.returncode == 0 and os.path.exists(self.save_path) and os.path.getsize(self.save_path) > 0:
                        file_size = os.path.getsize(self.save_path)
                        self.downloaded_bytes = file_size
                        self.total_bytes = file_size
                        self.status = "completed"
                        self.log(f"Stream capture completed via ffmpeg: {self.save_path}")
                        if self.on_complete:
                            self.on_complete(self.download_id, self.save_path)
                        return
                except Exception as ex:
                    self.log(f"ffmpeg stream capture fallback failed: {ex}")

            self.status = "error"
            self.log(f"Stream download error: {e}")
            if self.on_error:
                self.on_error(self.download_id, str(e))

    def _worker_loop(self, worker_id: int):
        while not self._stop_event.is_set() and not self._pause_event.is_set():
            with self._lock:
                if self._current_index >= self.total_segments:
                    break
                idx = self._current_index
                self._current_index += 1

            seg_url = self.segment_urls[idx]
            seg_file = self.segment_files[idx]

            # Download chunk
            success = self._fetch_segment(seg_url, seg_file)
            if success:
                with self._lock:
                    self.downloaded_segments += 1
                    self._emit_progress()
            else:
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                # Retry once
                time.sleep(1.0)
                if self._fetch_segment(seg_url, seg_file):
                    with self._lock:
                        self.downloaded_segments += 1
                        self._emit_progress()

    def _fetch_segment(self, url: str, target_file: str) -> bool:
        try:
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.config.network_timeout) as resp:
                data = resp.read()
                self.speed_limiter.acquire(len(data))
                with open(target_file, "wb") as f:
                    f.write(data)
                with self._lock:
                    self.downloaded_bytes += len(data)
                    self._speed_bytes += len(data)
                return True
        except Exception as e:
            self.log(f"Failed to fetch segment {url}: {e}")
            return False

    def _finalize_stream(self):
        self.log("All stream segments fetched. Storing and remuxing stream...")
        os.makedirs(os.path.dirname(os.path.abspath(self.save_path)), exist_ok=True)

        # Normalize target file extension if user/manifest passed .mpd or .m3u8
        if self.save_path.endswith((".mpd", ".m3u8")):
            self.save_path = os.path.splitext(self.save_path)[0] + ".mp4"

        # Check if we have separate audio and video tracks (e.g. DASH)
        has_video = bool(getattr(self, "video_segments", None) and len(self.video_segments) > 0)
        has_audio = bool(getattr(self, "audio_segments", None) and len(self.audio_segments) > 0)

        if has_video and has_audio:
            v_count = len(self.video_segments)
            v_files = self.segment_files[:v_count]
            a_files = self.segment_files[v_count:]

            temp_dir = self.storage.get_temp_dir(self.download_id)
            v_merged = os.path.join(temp_dir, "track_video.mp4")
            a_merged = os.path.join(temp_dir, "track_audio.mp4")

            with open(v_merged, "wb") as outfile:
                for vf in v_files:
                    if os.path.exists(vf):
                        with open(vf, "rb") as infile:
                            shutil.copyfileobj(infile, outfile, length=1024 * 1024)

            with open(a_merged, "wb") as outfile:
                for af in a_files:
                    if os.path.exists(af):
                        with open(af, "rb") as infile:
                            shutil.copyfileobj(infile, outfile, length=1024 * 1024)

            ffmpeg_bin = resolve_binary("ffmpeg")
            if ffmpeg_bin:
                self.log("Running ffmpeg muxing to merge video and audio streams...")
                try:
                    cmd = [ffmpeg_bin, "-y", "-i", v_merged, "-i", a_merged, "-c", "copy", self.save_path]
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                except Exception as e:
                    self.log(f"ffmpeg muxing failed ({e}), falling back to direct video stream.")
                    if os.path.exists(v_merged):
                        shutil.copyfile(v_merged, self.save_path)
            else:
                if os.path.exists(v_merged):
                    shutil.copyfile(v_merged, self.save_path)
        elif not has_video and has_audio:
            # Audio-only DASH stream
            temp_dir = self.storage.get_temp_dir(self.download_id)
            a_merged = os.path.join(temp_dir, "track_audio.mp4")
            with open(a_merged, "wb") as outfile:
                for af in self.segment_files:
                    if os.path.exists(af):
                        with open(af, "rb") as infile:
                            shutil.copyfileobj(infile, outfile, length=1024 * 1024)
            ffmpeg_bin = resolve_binary("ffmpeg")
            if ffmpeg_bin and self.save_path.endswith((".mp4", ".m4a", ".aac", ".mp3", ".mkv")):
                try:
                    cmd = [ffmpeg_bin, "-y", "-i", a_merged, "-c", "copy", self.save_path]
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                except Exception as e:
                    self.log(f"ffmpeg remux failed ({e}), falling back to direct audio stream.")
                    if os.path.exists(a_merged):
                        shutil.copyfile(a_merged, self.save_path)
            elif os.path.exists(a_merged):
                shutil.copyfile(a_merged, self.save_path)
        else:
            # Single stream (HLS TS chunks or multiplexed DASH)
            raw_ts_path = self.save_path if self.save_path.endswith(".ts") else self.save_path + ".temp.ts"
            with open(raw_ts_path, "wb") as outfile:
                for s_file in self.segment_files:
                    if os.path.exists(s_file):
                        with open(s_file, "rb") as infile:
                            shutil.copyfileobj(infile, outfile, length=1024 * 1024)

            # If destination is MP4/MKV and ffmpeg is available, remux losslessly
            ffmpeg_bin = resolve_binary("ffmpeg")
            if ffmpeg_bin and (self.save_path.endswith(".mp4") or self.save_path.endswith(".mkv")):
                self.log("Running ffmpeg stream copy into MP4 container...")
                try:
                    cmd = [ffmpeg_bin, "-y", "-i", raw_ts_path, "-c", "copy", self.save_path]
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                    if os.path.exists(raw_ts_path) and raw_ts_path != self.save_path:
                        os.remove(raw_ts_path)
                except Exception as e:
                    self.log(f"ffmpeg remux failed ({e}), falling back to direct stream output.")
                    if raw_ts_path != self.save_path and os.path.exists(raw_ts_path):
                        if os.path.exists(self.save_path):
                            os.remove(self.save_path)
                        shutil.move(raw_ts_path, self.save_path)
            elif raw_ts_path != self.save_path and os.path.exists(raw_ts_path):
                if os.path.exists(self.save_path):
                    os.remove(self.save_path)
                shutil.move(raw_ts_path, self.save_path)

        self.storage.cleanup_temp(self.download_id)
        self.status = "completed"
        if os.path.exists(self.save_path):
            file_size = os.path.getsize(self.save_path)
            self.downloaded_bytes = file_size
            self.total_bytes = file_size
        self.log(f"Stream download completed: {self.save_path}")

        if self.on_complete:
            try:
                self.on_complete(self.download_id, self.save_path)
            except Exception:
                pass

    def _emit_progress(self):
        now = time.time()
        elapsed = now - self._speed_time
        if elapsed >= 0.5:
            self.current_speed = self._speed_bytes / elapsed if elapsed > 0 else 0.0
            self._speed_bytes = 0
            self._speed_time = now

        eta = 0
        if self.current_speed > 0:
            if self.total_bytes > self.downloaded_bytes:
                eta = int((self.total_bytes - self.downloaded_bytes) / self.current_speed)
            elif self.total_segments > self.downloaded_segments and self.downloaded_segments > 0:
                avg_seg_size = self.downloaded_bytes / self.downloaded_segments
                rem_bytes = (self.total_segments - self.downloaded_segments) * avg_seg_size
                eta = int(rem_bytes / self.current_speed)

        stats = {
            "download_id": self.download_id,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed": int(self.current_speed),
            "eta": eta,
            "segments_done": self.downloaded_segments,
            "total_segments": self.total_segments,
        }
        if self.on_progress:
            try:
                self.on_progress(self.download_id, stats)
            except Exception:
                pass

    def pause(self):
        with self._lock:
            self.status = "paused"
            self._pause_event.set()

    def cancel(self):
        with self._lock:
            self.status = "cancelled"
            self._stop_event.set()
            self._pause_event.set()
            self.storage.cleanup_temp(self.download_id)
