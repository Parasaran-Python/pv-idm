document.addEventListener("DOMContentLoaded", () => {
  const badge = document.getElementById("engine-badge");
  const toggleIntercept = document.getElementById("toggle-intercept");
  const toggleSniffer = document.getElementById("toggle-sniffer");
  const inputUrl = document.getElementById("input-url");
  const btnStartDownload = document.getElementById("btn-start-download");
  const btnOpenGui = document.getElementById("btn-open-gui");
  const feedback = document.getElementById("url-feedback");

  // 1. Check IDM connection status
  chrome.runtime.sendMessage({ action: "ping_idm" }, (res) => {
    if (chrome.runtime.lastError || !res || res.status !== "ok") {
      badge.textContent = "Disconnected";
      badge.className = "badge badge-disconnected";
    } else {
      badge.textContent = "Connected";
      badge.className = "badge badge-connected";
    }
  });

  // 2. Load Settings
  chrome.runtime.sendMessage({ action: "get_settings" }, (res) => {
    if (chrome.runtime.lastError) return;
    if (res && res.settings) {
      toggleIntercept.checked = !!res.settings.interceptDownloads;
      toggleSniffer.checked = !!res.settings.videoSniffer;
    }
  });

  toggleIntercept.addEventListener("change", () => {
    chrome.runtime.sendMessage({
      action: "save_settings",
      settings: { interceptDownloads: toggleIntercept.checked }
    }, () => {
      if (chrome.runtime.lastError) { /* ignore */ }
    });
  });

  toggleSniffer.addEventListener("change", () => {
    chrome.runtime.sendMessage({
      action: "save_settings",
      settings: { videoSniffer: toggleSniffer.checked }
    }, () => {
      if (chrome.runtime.lastError) { /* ignore */ }
    });
  });

  // 3. Direct URL Download
  function submitDownload() {
    const url = inputUrl.value.trim();
    if (!url) {
      feedback.textContent = "⚠️ Please paste a valid URL.";
      feedback.className = "feedback-msg error";
      return;
    }

    if (!url.startsWith("http://") && !url.startsWith("https://") && !url.startsWith("ftp://")) {
      feedback.textContent = "⚠️ URL must begin with http://, https://, or ftp://";
      feedback.className = "feedback-msg error";
      return;
    }

    feedback.textContent = "⏳ Adding download to IDM...";
    feedback.className = "feedback-msg";

    chrome.runtime.sendMessage({
      action: "download_media",
      url: url,
      page_url: url
    }, (res) => {
      if (chrome.runtime.lastError) {
        feedback.textContent = `❌ ${chrome.runtime.lastError.message || "Failed to communicate with extension."}`;
        feedback.className = "feedback-msg error";
        return;
      }
      if (res && res.status === "ok") {
        feedback.textContent = "✅ Download started in IDM!";
        feedback.className = "feedback-msg success";
        inputUrl.value = "";
        setTimeout(() => {
          window.close();
        }, 1200);
      } else {
        feedback.textContent = `❌ ${res && res.error ? res.error : "Failed to add download."}`;
        feedback.className = "feedback-msg error";
      }
    });
  }

  btnStartDownload.addEventListener("click", submitDownload);
  inputUrl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      submitDownload();
    }
  });

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

  function extractYouTubeVideoId(url) {
    if (!url) return null;
    try {
      const parsed = new URL(url);
      const host = parsed.hostname.toLowerCase();
      if (host.includes("youtube.com")) {
        if (parsed.pathname.startsWith("/watch")) {
          return parsed.searchParams.get("v");
        } else if (parsed.pathname.startsWith("/shorts/") || parsed.pathname.startsWith("/live/") || parsed.pathname.startsWith("/embed/")) {
          return parsed.pathname.split("/").pop();
        }
      } else if (host.includes("youtu.be")) {
        return parsed.pathname.slice(1);
      }
    } catch (e) {}
    return null;
  }

  function getYouTubeThumbnailUrl(videoId, quality = "hqdefault") {
    if (!videoId) return null;
    return `https://img.youtube.com/vi/${videoId}/${quality}.jpg`;
  }

  async function fetchPageThumbnail(tabId) {
    try {
      const response = await chrome.runtime.sendMessage({ action: "get_page_metadata", tabId });
      if (response && response.thumbnail) {
        return response.thumbnail;
      }
    } catch (e) {}
    return null;
  }

  // 4. Detected Active Tab Media Streams
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (chrome.runtime.lastError) return;
    if (tabs && tabs[0] && tabs[0].id) {
      const activeTab = tabs[0];
      let isYouTube = false;
      let ytVideoId = null;
      if (activeTab.url) {
        try {
          const parsedUrl = new URL(activeTab.url);
          const host = parsedUrl.hostname.toLowerCase();
          const path = parsedUrl.pathname.toLowerCase();
          if (host.includes("youtube.com")) {
            isYouTube = path.startsWith("/watch") || path.startsWith("/shorts") || path.startsWith("/live") || path.startsWith("/embed");
            if (isYouTube) {
              ytVideoId = extractYouTubeVideoId(activeTab.url);
            }
          } else if (host.includes("youtu.be")) {
            isYouTube = true;
            ytVideoId = extractYouTubeVideoId(activeTab.url);
          }
        } catch (e) {}
      }
      chrome.runtime.sendMessage({ action: "get_tab_media", tabId: activeTab.id }, async (res) => {
        if (chrome.runtime.lastError) return;
        const mediaCard = document.getElementById("media-card");
        const mediaList = document.getElementById("media-list");
        if (!mediaCard || !mediaList) return;

        const rawStreams = (res && res.streams ? res.streams : []).filter((url) => !isMediaSegment(url));
        if (rawStreams.length === 0 && !isYouTube) return;

        mediaCard.style.display = "block";
        mediaList.innerHTML = "";

        let pageThumbnail = null;
        if (!isYouTube && rawStreams.length > 0) {
          pageThumbnail = await fetchPageThumbnail(activeTab.id);
        }

        const validStreams = [];
        if (isYouTube) {
          const ytTitle = activeTab.title ? activeTab.title.replace(/[\\/:*?"<>|]/g, "_").trim() : "YouTube Video";
          const thumbnailUrl = getYouTubeThumbnailUrl(ytVideoId);
          validStreams.push({
            url: activeTab.url,
            label: `YouTube: ${ytTitle}`,
            badge: "YOUTUBE",
            priority: 0,
            filename: `${ytTitle}.mp4`,
            thumbnail: thumbnailUrl
          });
        }

        rawStreams.forEach((streamUrl) => {
          let filename = "";
          try {
            const parsed = new URL(streamUrl);
            filename = parsed.pathname.split("/").filter(Boolean).pop() || "";
          } catch (e) {}

          let label = "Video Stream";
          let badge = "STREAM";
          let priority = 5;
          let thumbnail = null;

          if (streamUrl.includes(".mpd")) {
            label = filename ? `DASH: ${filename}` : "DASH Full Video Stream (.mpd)";
            badge = "DASH";
            priority = 1;
          } else if (streamUrl.includes(".m3u8")) {
            label = filename ? `HLS: ${filename}` : "HLS Full Video Stream (.m3u8)";
            badge = "HLS";
            priority = 2;
          } else if (streamUrl.includes(".mp4")) {
            label = filename ? `MP4: ${filename}` : "Full MP4 Video (Original)";
            badge = "MP4";
            priority = 3;
          } else if (streamUrl.includes(".webm")) {
            label = filename ? `WebM: ${filename}` : "Full WebM Video";
            badge = "WEBM";
            priority = 4;
          } else if (streamUrl.includes(".mp3") || streamUrl.includes(".m4a")) {
            label = filename ? `Audio: ${filename}` : "Audio Stream";
            badge = "MP3";
            priority = 5;
          } else if (streamUrl.includes("youtube.com") || streamUrl.includes("youtu.be")) {
            label = "YouTube Video";
            badge = "YOUTUBE";
            priority = 1;
            const ytId = extractYouTubeVideoId(streamUrl);
            thumbnail = getYouTubeThumbnailUrl(ytId);
          } else {
            thumbnail = pageThumbnail;
          }

          validStreams.push({ url: streamUrl, label: label, badge: badge, priority: priority, filename: filename, thumbnail });
        });

        validStreams.sort((a, b) => a.priority - b.priority);

        const seen = new Set();
        validStreams.forEach((item) => {
          if (seen.has(item.url)) return;
          seen.add(item.url);

          const row = document.createElement("div");
          row.className = "media-item";
          row.title = item.url;
          const cleanUrlHint = item.url.split("?")[0].replace(/^https?:\/\//, "");
          const thumbHtml = item.thumbnail
            ? `<img class="media-item-thumb" src="${item.thumbnail}" alt="" loading="lazy" onerror="this.style.display='none'">`
            : `<div class="media-item-thumb-placeholder"></div>`;
          row.innerHTML = `
            ${thumbHtml}
            <div class="media-item-info">
              <span class="media-item-title">${item.label}</span>
              <span class="media-item-url-hint">${cleanUrlHint}</span>
            </div>
            <span class="media-item-badge">${item.badge}</span>
          `;
          row.addEventListener("click", () => {
            const title = activeTab.title ? activeTab.title.replace(/[\\/:*?"<>|]/g, "_").trim() : "video";
            const badgeLow = item.badge.toLowerCase();
            const isStreamOrPlatform = badgeLow === "hls" || badgeLow === "dash" || badgeLow === "youtube";
            const ext = isStreamOrPlatform ? "mp4" : badgeLow;
            const quality = isStreamOrPlatform ? "best" : null;
            chrome.runtime.sendMessage({
              action: "download_media",
              url: item.url,
              page_url: activeTab.url || "",
              filename: `${title}.${ext}`,
              quality: "best"
            }, () => {
              if (chrome.runtime.lastError) { /* ignore */ }
              window.close();
            });
          });
          mediaList.appendChild(row);
        });
      });
    }
  });

  // 5. Open Desktop Application
  btnOpenGui.addEventListener("click", () => {
    chrome.runtime.sendMessage({ action: "open_idm_gui" }, () => {
      if (chrome.runtime.lastError) { /* ignore */ }
    });
  });
});
