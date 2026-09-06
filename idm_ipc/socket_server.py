import os
import socket
import threading
from typing import Any, Dict, List, Optional, Set
from idm_core.utils import normalize_youtube_videoplayback_url
from idm_ipc.protocol import decode_message, encode_message
from idm_ipc.transport import BaseConnection, BaseServerTransport, create_server_transport


class IPCServer:
    def __init__(self, engine: Any, socket_path: Optional[str] = None, transport: Optional[BaseServerTransport] = None):
        self.engine = engine
        self.socket_path = socket_path
        self.transport = transport or create_server_transport(socket_path)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client_threads: List[threading.Thread] = []
        self._subscribers: Set[Any] = set()
        self._lock = threading.RLock()

        # Hook engine broadcast notifications to push to IPC subscribers
        self.engine.register_listener("*", self._broadcast_to_subscribers)

    def start(self):
        """Start listening on IPC transport in a background thread."""
        self._stop_event.clear()
        self.transport.start()

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Cleanly shutdown server and transport."""
        self._stop_event.set()
        with self._lock:
            for s in list(self._subscribers):
                try:
                    s.close()
                except Exception:
                    pass
            self._subscribers.clear()

        self.transport.stop()

    def _accept_loop(self):
        while not self._stop_event.is_set():
            try:
                conn = self.transport.accept()
                if conn is None:
                    if self._stop_event.is_set():
                        break
                    continue
                t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                self._client_threads.append(t)
                t.start()
            except Exception:
                if self._stop_event.is_set():
                    break

    def _handle_client(self, client_sock: Any):
        is_subscriber = False
        try:
            while not self._stop_event.is_set():
                msg = decode_message(client_sock)
                if msg is None:
                    break

                action = msg.get("action", "")
                if action == "subscribe_events":
                    with self._lock:
                        self._subscribers.add(client_sock)
                    is_subscriber = True
                    client_sock.sendall(encode_message({"status": "ok", "subscribed": True}))
                    continue

                # Process command
                response = self._dispatch_action(msg)
                client_sock.sendall(encode_message(response))

        except Exception as e:
            pass
        finally:
            if is_subscriber:
                with self._lock:
                    self._subscribers.discard(client_sock)
            try:
                client_sock.close()
            except Exception:
                pass

    def _dispatch_action(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        action = msg.get("action", "")
        try:
            if action == "ping":
                return {"status": "ok", "pong": True}

            elif action in ["show_gui", "open_gui"]:
                self.engine.notify("show_gui", {})
            elif action == "query_media_formats":
                url = msg.get("url")
                if not url:
                    return {"status": "error", "error": "URL is required"}

                # 1. Video platforms (YouTube, Vimeo, Twitch, etc.) -> use yt-dlp first
                from idm_core.ytdlp_downloader import YTDLPDownloader
                if YTDLPDownloader.is_video_platform_url(url):
                    formats = YTDLPDownloader.extract_media_formats(url)
                    if formats:
                        return {"status": "ok", "formats": formats}

                # 2. Direct DASH/HLS stream manifest (.mpd, .m3u8) -> fast-path
                from idm_core.stream_downloader import StreamDownloader
                stype = StreamDownloader.detect_stream_type(url)
                if stype in ["dash", "hls"]:
                    formats = StreamDownloader.extract_formats(url)
                    if formats:
                        return {"status": "ok", "formats": formats}

                # 3. Fallback: yt-dlp then StreamDownloader
                formats = YTDLPDownloader.extract_media_formats(url)
                if not formats and stype in ["dash", "hls"]:
                    formats = StreamDownloader.extract_formats(url)

                return {"status": "ok", "formats": formats or []}

            elif action in ["add_download", "intercept", "download", "download_url"]:
                url = msg.get("url")
                if not url:
                    return {"status": "error", "error": "URL is required"}

                headers = msg.get("headers") or {}
                normalized_url, inferred_fn = normalize_youtube_videoplayback_url(url, headers)
                if normalized_url != url:
                    url = normalized_url
                    msg["url"] = normalized_url
                    fn = msg.get("filename")
                    if inferred_fn and (not fn or fn.startswith("videoplayback") or fn in ["download", "watch"]):
                        msg["filename"] = inferred_fn
                    elif not fn or fn.startswith("videoplayback") or fn in ["download", "watch"]:
                        msg["filename"] = None

                # If GUI is active and show_dialog is requested (default for browser), trigger DownloadInfoDialog prompt
                if msg.get("show_dialog", True) and "download_requested" in self.engine._listeners and self.engine._listeners["download_requested"]:
                    self.engine.notify("download_requested", msg)
                    return {"status": "ok", "message": "download_requested"}

                dl_id = self.engine.add_download(
                    url=url,
                    filename=msg.get("filename"),
                    save_path=msg.get("save_path"),
                    category=msg.get("category"),
                    connections=msg.get("connections"),
                    speed_limit=msg.get("speed_limit", 0),
                    headers=msg.get("headers"),
                    queue_id=msg.get("queue_id"),
                    total_bytes=msg.get("total_bytes", 0),
                    start_immediately=msg.get("start_immediately", True)
                )
                return {"status": "ok", "download_id": dl_id}

            elif action == "start_download":
                dl_id = msg.get("download_id")
                self.engine.start_download(dl_id)
                return {"status": "ok"}

            elif action == "pause_download":
                dl_id = msg.get("download_id")
                self.engine.pause_download(dl_id)
                return {"status": "ok"}

            elif action == "resume_download":
                dl_id = msg.get("download_id")
                self.engine.resume_download(dl_id)
                return {"status": "ok"}

            elif action == "stop_download":
                dl_id = msg.get("download_id")
                self.engine.stop_download(dl_id)
                return {"status": "ok"}

            elif action == "stop_all":
                self.engine.stop_all()
                return {"status": "ok"}

            elif action == "delete_download":
                dl_id = msg.get("download_id")
                del_files = msg.get("delete_files", False)
                deleted = self.engine.delete_download(dl_id, delete_files=del_files)
                return {"status": "ok", "deleted": deleted}

            elif action == "get_download":
                dl_id = msg.get("download_id")
                info = self.engine.get_download_info(dl_id)
                return {"status": "ok", "download": info}

            elif action == "list_downloads":
                dls = self.engine.list_downloads(
                    category=msg.get("category"),
                    status=msg.get("status"),
                    queue_id=msg.get("queue_id"),
                    search=msg.get("search")
                )
                return {"status": "ok", "downloads": dls}

            elif action == "start_queue":
                self.engine.start_queue(msg.get("queue_id", "main"))
                return {"status": "ok"}

            elif action == "stop_queue":
                self.engine.stop_queue(msg.get("queue_id", "main"))
                return {"status": "ok"}

            else:
                return {"status": "error", "error": f"Unknown action: {action}"}

        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _broadcast_to_subscribers(self, event_data: Dict[str, Any]):
        """Push real-time event to all connected subscriber sockets."""
        packet = encode_message(event_data)
        with self._lock:
            dead = []
            for sock in self._subscribers:
                try:
                    sock.sendall(packet)
                except Exception:
                    dead.append(sock)
            for d in dead:
                self._subscribers.discard(d)
