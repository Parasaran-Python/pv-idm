/**
 * IDM Linux - Classic Floating Video Grabber & Sniffer
 * Renders the iconic Windows IDM "Download this video" button above players with dismiss & drag options.
 */

(function () {
  "use strict";

  // Prevent multiple injections
  if (window.__idm_sniffer_injected) return;
  window.__idm_sniffer_injected = true;

  const capturedStreams = new Map(); // url -> { title, format, type }
  let floatingBar = null;
  let activePlayerEl = null;
  let userDismissed = false;
  let customPosition = null; // { x, y }
  const cachedFormatsForUrl = new Map();

  function fetchFormatsForCurrentPage(targetUrl = null) {
    const currentUrl = window.location.href;
    const queryUrl = targetUrl || currentUrl;
    if (cachedFormatsForUrl.has(queryUrl) && cachedFormatsForUrl.get(queryUrl).__fromBackend) return;

    try {
      chrome.runtime.sendMessage({ action: "query_media_formats", url: queryUrl }, (response) => {
        if (chrome.runtime.lastError) {
          return;
        }
        if (response && response.formats && response.formats.length > 0) {
          const fmts = response.formats;
          if (isVideoWatchPage() && fmts.length === 1 && (fmts[0].quality === "best" || fmts[0].label === "Best Quality") && !fmts[0].filesize) {
            return;
          }
          fmts.__fromBackend = true;
          cachedFormatsForUrl.set(currentUrl, fmts);
          cachedFormatsForUrl.set(queryUrl, fmts);
          if (floatingBar) {
            const openMenu = floatingBar.querySelector(".idm-grabber-menu.idm-menu-open");
            if (openMenu) {
              const populateFunc = floatingBar.__idmPopulateMenu;
              if (populateFunc) populateFunc();
            }
          }
        }
      });
    } catch (e) {}
  }

  /**
   * Inject Main-World Page Hook Script (Fallback for browsers without world: MAIN manifest support)
   */
  function injectPageHook() {
    if (window.__idm_page_hook_injected) return;
    try {
      const script = document.createElement("script");
      script.src = chrome.runtime.getURL("content/page_hook.js");
      script.onload = function () {
        this.remove();
      };
      (document.head || document.documentElement).appendChild(script);
    } catch (e) {}
  }

  injectPageHook();

  function isMediaSegment(url) {
    if (!url || typeof url !== "string") return true;
    if (url.startsWith("blob:") || url.startsWith("data:")) return true;
    const lower = url.toLowerCase();

    // Never filter out stream manifests
    if (lower.includes(".mpd") || lower.includes(".m3u8")) {
      return false;
    }

    // Filter out raw YouTube / GoogleVideo DASH fragments (they lack audio)
    if (lower.includes("googlevideo.com/videoplayback") || (lower.includes("/videoplayback") && (lower.includes("expire=") || lower.includes("sparams=")))) {
      return true;
    }

    // Filter out chunk files and fragment extensions
    if (lower.includes(".m4s") || lower.includes(".m4f") || lower.includes(".f4m") || lower.includes(".f4f")) {
      return true;
    }

    // Filter out init chunks (init.mp4, *-init.mp4, init-*.mp4, etc.)
    if (/init(-|_|\.)?.*\.mp4/i.test(lower) || /init\.m4s/i.test(lower) || /initialization/i.test(lower)) {
      return true;
    }

    // Filter out segment patterns (seg-1.mp4, segment-123.ts, chunk_45.ts, frag-12.mp4, etc.)
    if (/(seg|segment|chunk|frag|fragment)[-_0-9]+/i.test(lower)) {
      return true;
    }

    // Filter out byte range fragments
    if (lower.includes("range=") || lower.includes("bytes=") || lower.includes("bytestart=")) {
      return true;
    }

    // Filter out standalone .ts segments (HLS chunks)
    if (/\.ts(\?|$)/i.test(lower) && !lower.includes("playlist") && !lower.includes("manifest")) {
      return true;
    }

    return false;
  }

  /**
   * Helper to extract clean page/video title
   */
  function getMediaTitle() {
    const ytTitle = document.querySelector("h1.ytd-watch-metadata, #title h1, h1.title, .video-title");
    if (ytTitle && ytTitle.innerText.trim()) {
      return ytTitle.innerText.trim().replace(/[\\/:*?"<>|]/g, "_");
    }
    let title = document.title || "video";
    title = title.replace(/ - YouTube$/, "").replace(/ - Vimeo$/, "").trim();
    return title.replace(/[\\/:*?"<>|]/g, "_") || "video";
  }

  /**
   * Create the iconic IDM Floating Download Banner with Dismiss & Drag Options
   */
  function createFloatingDownloadBar() {
    if (floatingBar && document.contains(floatingBar)) {
      return floatingBar;
    }

    const container = document.createElement("div");
    container.id = "idm-floating-grabber-root";
    container.className = "idm-floating-grabber-root";

    const wrapper = document.createElement("div");
    wrapper.className = "idm-grabber-pill-wrapper";

    const dragHandle = document.createElement("div");
    dragHandle.className = "idm-grabber-drag-handle";
    dragHandle.title = "Drag to reposition IDM bar";
    dragHandle.innerHTML = `⠿`;

    const button = document.createElement("div");
    button.className = "idm-grabber-button";
    button.innerHTML = `
      <div class="idm-grabber-icon-box">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
          <path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM17 13l-5 5-5-5h3V9h4v4h3z"/>
        </svg>
      </div>
      <span class="idm-grabber-label">Download this video</span>
      <span class="idm-grabber-arrow">▼</span>
    `;

    const closeBtn = document.createElement("button");
    closeBtn.className = "idm-grabber-close-btn";
    closeBtn.title = "Dismiss IDM download panel";
    closeBtn.innerHTML = "&times;";
    closeBtn.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      e.stopImmediatePropagation();
    });
    closeBtn.addEventListener("mousedown", (e) => {
      e.stopPropagation();
      e.stopImmediatePropagation();
    });
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.stopImmediatePropagation();
      e.preventDefault();
      userDismissed = true;
      try {
        sessionStorage.setItem("__idm_dismissed_" + window.location.pathname, "true");
      } catch (err) {}
      container.style.display = "none";
      if (floatingBar) {
        floatingBar.remove();
        floatingBar = null;
      }
    });

    wrapper.appendChild(dragHandle);
    wrapper.appendChild(button);
    wrapper.appendChild(closeBtn);

    const menu = document.createElement("div");
    menu.className = "idm-grabber-menu";

    function populateMenu() {
      menu.innerHTML = "";
      const currentUrl = window.location.href;
      let items = [];

      // 1. If backend / in-page dynamic formats exist for this video, use exact real formats
      if (cachedFormatsForUrl.has(currentUrl) && cachedFormatsForUrl.get(currentUrl).length > 0) {
        items = [...cachedFormatsForUrl.get(currentUrl)];
      } else {
        // Trigger background format resolution
        fetchFormatsForCurrentPage();

        const isVideoSite = isVideoWatchPage();
        if (isVideoSite) {
          // Provide instant standard quality options while query_media_formats runs in background
          items = [
            { label: "1080p (Full HD)", format: "MP4", quality: "1080", url: currentUrl },
            { label: "720p (HD)", format: "MP4", quality: "720", url: currentUrl },
            { label: "480p (SD)", format: "MP4", quality: "480", url: currentUrl },
            { label: "360p (SD)", format: "MP4", quality: "360", url: currentUrl },
            { label: "Audio Only (MP3)", format: "MP3", quality: "audio", url: currentUrl }
          ];
        } else if (capturedStreams.size > 0) {
          // Add clean direct media files & streaming playlists (HLS / DASH / MP4 / WebM)
          const validStreams = [];
          capturedStreams.forEach((meta, streamUrl) => {
            if (isMediaSegment(streamUrl)) {
              return; // Skip raw chunk fragments
            }
            if (streamUrl.includes(".mpd")) {
              validStreams.push({ label: "DASH Video Stream (.mpd)", format: "DASH", quality: "best", url: streamUrl, priority: 1 });
            } else if (streamUrl.includes(".m3u8")) {
              validStreams.push({ label: "HLS Video Stream (.m3u8)", format: "HLS", quality: "best", url: streamUrl, priority: 2 });
            } else if (streamUrl.includes(".mp4") || (meta.format && meta.format.includes("mp4"))) {
              validStreams.push({ label: "Full MP4 Video (Original)", format: "MP4", quality: null, url: streamUrl, priority: 3 });
            } else if (streamUrl.includes(".webm") || (meta.format && meta.format.includes("webm"))) {
              validStreams.push({ label: "Full WebM Video", format: "WEBM", quality: null, url: streamUrl, priority: 4 });
            } else if (streamUrl.includes(".mp3") || streamUrl.includes(".m4a") || (meta.format && meta.format.includes("audio"))) {
              validStreams.push({ label: "Audio Stream", format: "MP3", quality: "audio", url: streamUrl, priority: 5 });
            } else {
              validStreams.push({ label: "Media Stream", format: "MEDIA", quality: "best", url: streamUrl, priority: 6 });
            }
          });

          validStreams.sort((a, b) => a.priority - b.priority);

          const seen = new Set();
          for (const s of validStreams) {
            if (!seen.has(s.url)) {
              seen.add(s.url);
              items.push(s);
            }
          }
        }

        // If empty, show extracting indicator while background query finishes
        if (items.length === 0) {
          items.push({ label: "⏳ Extracting available formats...", format: "SCAN", quality: "best", url: currentUrl, disabled: true });
        }
      }

      function formatBytes(bytes) {
        if (!bytes || bytes <= 0) return "";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let i = 0;
        let val = bytes;
        while (val >= 1024 && i < units.length - 1) {
          val /= 1024;
          i++;
        }
        return `${val.toFixed(2)} ${units[i]}`;
      }

      items.forEach((item) => {
        const row = document.createElement("div");
        row.className = "idm-grabber-menu-item" + (item.disabled ? " idm-menu-item-disabled" : "");
        const isApprox = item.filesize_approx === true;
        const approxStr = isApprox ? " <span class=\"idm-menu-item-approx\">(approx)</span>" : "";
        const sizeStr = item.filesize && item.filesize > 0 ? ` <span class="idm-menu-item-size">(${formatBytes(item.filesize)})</span>${approxStr}` : (isApprox ? ` <span class="idm-menu-item-size">Unknown size${approxStr}</span>` : "");
        row.innerHTML = `
          <span class="idm-menu-item-text">${item.label}${sizeStr}</span>
          <span class="idm-menu-item-badge">${item.format}</span>
        `;
        if (!item.disabled) {
          row.addEventListener("click", (e) => {
            e.stopPropagation();
            e.preventDefault();
            menu.classList.remove("idm-menu-open");

            const title = getMediaTitle();
            const fmtLow = (item.format || "mp4").toLowerCase();
            const ext = fmtLow === "mp3" || fmtLow === "audio" ? "mp3" : (fmtLow === "webm" ? "webm" : (fmtLow === "hls" || fmtLow === "dash" ? "mp4" : "mp4"));
            const filename = `${title}.${ext}`;

            try {
              chrome.runtime.sendMessage({
                action: "download_media",
                url: item.url,
                filename: filename,
                quality: item.quality,
                filesize: item.filesize || 0,
                format: fmtLow
              }, () => {
                if (chrome.runtime.lastError) { /* ignore */ }
              });
            } catch (err) {}
          });
        }
        menu.appendChild(row);
      });
    }

    // Dragging Logic - attached strictly to drag handle
    let isDragging = false;
    let dragStartX = 0, dragStartY = 0;
    let initialLeft = 0, initialTop = 0;
    let hasMoved = false;

    function onPointerDown(e) {
      isDragging = true;
      hasMoved = false;
      dragStartX = e.clientX;
      dragStartY = e.clientY;
      const rect = container.getBoundingClientRect();
      initialLeft = rect.left;
      initialTop = rect.top;
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
    }

    function onPointerMove(e) {
      if (!isDragging) return;
      const dx = e.clientX - dragStartX;
      const dy = e.clientY - dragStartY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
        hasMoved = true;
        container.classList.add("idm-is-dragging");
        const newX = Math.max(10, Math.min(window.innerWidth - 120, initialLeft + dx));
        const newY = Math.max(10, Math.min(window.innerHeight - 50, initialTop + dy));
        container.style.left = `${newX}px`;
        container.style.top = `${newY}px`;
        container.style.right = "auto";
        customPosition = { x: newX, y: newY };
      }
    }

    function onPointerUp(e) {
      if (!isDragging) return;
      isDragging = false;
      container.classList.remove("idm-is-dragging");
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    }

    dragHandle.addEventListener("pointerdown", onPointerDown);

    button.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (hasMoved) return; // Ignore click if drag occurred
      populateMenu();
      menu.classList.toggle("idm-menu-open");
    });

    document.addEventListener("click", (e) => {
      if (!container.contains(e.target)) {
        menu.classList.remove("idm-menu-open");
      }
    });

    container.appendChild(wrapper);
    container.appendChild(menu);
    container.__idmPopulateMenu = populateMenu;

    (document.fullscreenElement || document.body || document.documentElement).appendChild(container);
    floatingBar = container;
    fetchFormatsForCurrentPage();
    return container;
  }

  /**
   * Validate that an element is a real, visible, non-trivial video player
   */
  function isValidVideoElement(el) {
    if (!el || !(el instanceof HTMLElement)) return false;
    try {
      const style = window.getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") < 0.1) {
        return false;
      }
      const rect = el.getBoundingClientRect();
      const w = Math.max(rect.width, el.offsetWidth, el.videoWidth || 0);
      const h = Math.max(rect.height, el.offsetHeight, el.videoHeight || 0);
      // Must be at least 160x90 px to avoid audio beacons / tracking pixels
      if (w < 160 || h < 90) {
        return false;
      }
      return true;
    } catch (e) {
      return false;
    }
  }

  /**
   * Find the primary active and visible video element
   */
  function findActiveVideo() {
    if (activePlayerEl && isValidVideoElement(activePlayerEl)) {
      return activePlayerEl;
    }
    const candidates = Array.from(document.querySelectorAll("video, #movie_player, .html5-video-player, [data-player]"));
    for (const v of candidates) {
      if (isValidVideoElement(v)) {
        return v;
      }
    }
    return null;
  }

  /**
   * Check if current page URL is a known video watch page
   */
  function isVideoWatchPage() {
    const host = window.location.hostname;
    const path = window.location.pathname;

    if (host.includes("youtube.com")) {
      return path.startsWith("/watch") || path.startsWith("/shorts") || path.startsWith("/live") || path.startsWith("/embed");
    }
    if (host.includes("youtu.be")) return true;
    if (host.includes("vimeo.com")) return path.length > 1 && path !== "/";
    if (host.includes("dailymotion.com")) return path.startsWith("/video");
    if (host.includes("twitch.tv")) return path.length > 1 && path !== "/";
    return false;
  }

  /**
   * Position the floating bar over the active video element
   */
  function repositionBar() {
    let isDismissed = userDismissed;
    try {
      if (sessionStorage.getItem("__idm_dismissed_" + window.location.pathname) === "true") {
        isDismissed = true;
      }
    } catch (e) {}

    if (isDismissed) {
      if (floatingBar) {
        floatingBar.remove();
        floatingBar = null;
      }
      return;
    }

    const video = findActiveVideo();
    const isWatchPage = isVideoWatchPage();

    // Do NOT show grabber on pages without an actual video element or known watch page
    if (!video && !isWatchPage) {
      if (floatingBar) floatingBar.style.display = "none";
      return;
    }

    const bar = createFloatingDownloadBar();
    if (!document.contains(bar)) {
      (document.fullscreenElement || document.body || document.documentElement).appendChild(bar);
    }
    bar.style.display = "block";

    // If user dragged to custom location, preserve it
    if (customPosition) {
      bar.style.left = `${customPosition.x}px`;
      bar.style.top = `${customPosition.y}px`;
      bar.style.right = "auto";
      return;
    }

    if (video) {
      const rect = video.getBoundingClientRect();
      if (rect.bottom > 0 && rect.top < window.innerHeight) {
        bar.style.top = `${Math.max(14, rect.top + 14)}px`;
        bar.style.right = `${Math.max(16, (window.innerWidth - rect.right) + 16)}px`;
        bar.style.left = "auto";
        return;
      }
    }

    // Default top-right positioning on active watch pages or when video is below fold
    bar.style.top = "80px";
    bar.style.right = "24px";
    bar.style.left = "auto";
  }

  /**
   * Scan page and hook player elements
   */
  function scanMediaElements() {
    const mediaEls = document.querySelectorAll("video, #movie_player, .html5-video-player");
    let foundValid = false;

    mediaEls.forEach((el) => {
      if (isValidVideoElement(el)) {
        foundValid = true;
        if (!activePlayerEl) {
          activePlayerEl = el;
        }
      }
      if (!el.dataset.idmHooked) {
        el.dataset.idmHooked = "true";
        el.addEventListener("mouseenter", () => {
          if (isValidVideoElement(el)) {
            activePlayerEl = el;
            repositionBar();
          }
        });
        el.addEventListener("play", () => {
          if (isValidVideoElement(el)) {
            activePlayerEl = el;
            repositionBar();
          }
        });
      }
    });

    const isWatch = isVideoWatchPage();
    if (!foundValid && !isWatch && capturedStreams.size === 0) {
      if (floatingBar) floatingBar.style.display = "none";
      return;
    }

    repositionBar();
  }

  // Debounced scan function for DOM Mutation Observer
  let scanScheduled = false;
  function scheduleScan() {
    if (scanScheduled) return;
    scanScheduled = true;
    requestAnimationFrame(() => {
      scanScheduled = false;
      scanMediaElements();
    });
  }

  // 1. Listen to Page Hook Custom Events
  document.addEventListener("__idm_media_event", (e) => {
    if (e.detail && e.detail.url && !isMediaSegment(e.detail.url)) {
      capturedStreams.set(e.detail.url, { format: e.detail.type || "Stream" });
      try {
        chrome.runtime.sendMessage({
          action: "record_media_stream",
          url: e.detail.url
        }, () => {
          if (chrome.runtime.lastError) { /* ignore */ }
        });
      } catch (err) {}
      // Proactively fetch formats for DASH/HLS manifests to show all qualities
      if (e.detail.url.includes(".mpd") || e.detail.url.includes(".m3u8")) {
        fetchFormatsForCurrentPage(e.detail.url);
      }
      repositionBar();
    }
  });

  document.addEventListener("__idm_discovered_formats", (e) => {
    const url = e.detail ? e.detail.url : "";
    if (url && e.detail.formats && e.detail.formats.length > 0) {
      const existing = cachedFormatsForUrl.get(url);
      if (!existing || !existing.__fromBackend) {
        cachedFormatsForUrl.set(url, e.detail.formats);
        if (floatingBar) {
          const populateFunc = floatingBar.__idmPopulateMenu;
          if (populateFunc) populateFunc();
        }
      }
    }
  });

  // 2. Listen to Background Network Sniffer Messages
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.action === "idm_media_detected" && msg.streamUrl && !isMediaSegment(msg.streamUrl)) {
      capturedStreams.set(msg.streamUrl, { format: msg.contentType || "Stream" });
      // Proactively fetch formats for DASH/HLS manifests to show all qualities
      if (msg.streamUrl.includes(".mpd") || msg.streamUrl.includes(".m3u8")) {
        fetchFormatsForCurrentPage(msg.streamUrl);
      }
      repositionBar();
    }
  });

  // 3. Initialize, Periodic Polling & Throttled DOM Mutation Observer
  scanMediaElements();
  document.addEventListener("DOMContentLoaded", scanMediaElements);
  window.addEventListener("load", scanMediaElements);
  setInterval(scanMediaElements, 2000);

  // MutationObserver for instantaneous dynamic player detection
  try {
    const observer = new MutationObserver(() => {
      scheduleScan();
    });
    observer.observe(document.documentElement || document.body, {
      childList: true,
      subtree: true
    });
  } catch (e) {}

  window.addEventListener("scroll", repositionBar, { passive: true });
  window.addEventListener("resize", repositionBar, { passive: true });
  document.addEventListener("fullscreenchange", () => {
    if (floatingBar) {
      (document.fullscreenElement || document.body || document.documentElement).appendChild(floatingBar);
    }
    repositionBar();
  });

  window.addEventListener("yt-navigate-finish", () => {
    userDismissed = false;
    customPosition = null;
    capturedStreams.clear();
    fetchFormatsForCurrentPage();
    scanMediaElements();
  });

  window.addEventListener("popstate", () => {
    fetchFormatsForCurrentPage();
    scanMediaElements();
  });

  // Extract all links
  chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "extract_all_links") {
      const anchors = document.querySelectorAll("a[href]");
      const links = [];
      anchors.forEach((a) => {
        const href = a.href;
        if (href && (href.startsWith("http://") || href.startsWith("https://") || href.startsWith("ftp://"))) {
          links.push({ url: href, text: (a.innerText || a.title || "").trim() });
        }
      });
      sendResponse({ links: links });
      return true;
    }

    function resolveUrl(url) {
      if (!url || typeof url !== "string" || !url.trim()) return null;
      try {
        const resolved = new URL(url.trim(), window.location.href).href;
        return /^https?:\/\//i.test(resolved) ? resolved : null;
      } catch {
        return null;
      }
    }

    if (request.action === "get_page_metadata") {
      if (window !== window.top) return;
      try {
        let thumbnail = null;
        const metaSelectors = [
          'meta[property="og:image"]',
          'meta[property="og:image:secure_url"]',
          'meta[name="og:image"]',
          'meta[name="og:image:secure_url"]',
          'meta[property="og:image:url"]',
          'meta[name="twitter:image"]',
          'meta[name="twitter:image:src"]',
          'meta[property="twitter:image"]',
          'meta[itemprop="image"]',
          'meta[name="thumbnail"]',
          'link[rel="image_src"]',
          'link[rel="apple-touch-icon"]'
        ];
        for (const sel of metaSelectors) {
          const el = document.querySelector(sel);
          if (el) {
            const val = el.getAttribute("content") || el.getAttribute("href") || el.content || el.href;
            if (val) {
              const res = resolveUrl(val);
              if (res) {
                thumbnail = res;
                break;
              }
            }
          }
        }
        if (!thumbnail) {
          const videoPoster = document.querySelector("video[poster]");
          if (videoPoster && videoPoster.poster) {
            thumbnail = resolveUrl(videoPoster.poster);
          }
        }
        sendResponse({ thumbnail });
      } catch (e) {
        sendResponse({ thumbnail: null });
      }
      return true;
    }
  });

})();
