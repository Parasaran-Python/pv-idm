/**
 * IDM Linux - Background Service Worker / Script
 * Intercepts downloads across all browsers (Firefox & Chrome/Chromium/Brave/Edge) with full session cookies, headers, & Native Messaging Bridge.
 */

const NATIVE_HOST = "com.idm.linux.native_host";

// Default settings
const DEFAULT_SETTINGS = {
  interceptDownloads: true,
  videoSniffer: true,
  minVideoSize: 1024 * 1024, // 1MB
  interceptExtensions: [
    "3gp", "7z", "aac", "ace", "aif", "apk", "appimage", "arj", "asf", "avi", "bin", "bz2",
    "deb", "dmg", "doc", "docx", "epub", "exe", "flac", "flv", "gz", "iso", "jar", "m4a",
    "m4v", "mkv", "mov", "mp3", "mp4", "mpa", "mpe", "mpeg", "mpg", "msi", "ogg", "opus",
    "pdf", "pkg", "ppt", "pptx", "rar", "rpm", "rtf", "sh", "tar", "tgz", "torrent", "ts",
    "txt", "wav", "webm", "wma", "wmv", "xls", "xlsx", "xz", "zip", "zst"
  ],
  ignoreExtensions: ["html", "htm", "php", "asp", "aspx", "jsp", "css", "js", "json", "xml"]
};

let cachedSettings = { ...DEFAULT_SETTINGS };
let settingsLoaded = false;

/**
 * Retrieve current settings asynchronously with local cache fallback
 */
async function getSettings() {
  if (settingsLoaded) {
    return cachedSettings;
  }
  return new Promise((resolve) => {
    chrome.storage.local.get(["idmSettings"], (res) => {
      if (chrome.runtime.lastError) {
        resolve(cachedSettings);
        return;
      }
      if (res && res.idmSettings) {
        cachedSettings = Object.assign({}, DEFAULT_SETTINGS, res.idmSettings);
      }
      settingsLoaded = true;
      resolve(cachedSettings);
    });
  });
}

// Initial settings load
getSettings();

// Keep settings in sync if modified elsewhere
if (chrome.storage && chrome.storage.onChanged) {
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === "local" && changes.idmSettings) {
      cachedSettings = Object.assign({}, DEFAULT_SETTINGS, changes.idmSettings.newValue || {});
      settingsLoaded = true;
    }
  });
}

/**
 * Tab Media Store using chrome.storage.session (MV3) with in-memory fallback
 */
const inMemoryTabMedia = new Map();

async function getTabMediaStore(tabId) {
  const key = `tab_media_${tabId}`;
  if (chrome.storage && chrome.storage.session) {
    try {
      const res = await new Promise((resolve) => {
        chrome.storage.session.get([key], (data) => {
          if (chrome.runtime.lastError) resolve({});
          else resolve(data || {});
        });
      });
      return new Set(res[key] || []);
    } catch (e) {
      // fallback to memory
    }
  }
  return inMemoryTabMedia.get(tabId) || new Set();
}

async function saveTabMediaStore(tabId, mediaSet) {
  const key = `tab_media_${tabId}`;
  const list = Array.from(mediaSet);
  inMemoryTabMedia.set(tabId, mediaSet);

  if (chrome.storage && chrome.storage.session) {
    try {
      await new Promise((resolve) => {
        chrome.storage.session.set({ [key]: list }, () => {
          if (chrome.runtime.lastError) { /* ignore */ }
          resolve();
        });
      });
    } catch (e) {}
  }
}

async function removeTabMediaStore(tabId) {
  const key = `tab_media_${tabId}`;
  inMemoryTabMedia.delete(tabId);
  if (chrome.storage && chrome.storage.session) {
    try {
      chrome.storage.session.remove([key], () => {
        if (chrome.runtime.lastError) { /* ignore */ }
      });
    } catch (e) {}
  }
}

/**
 * Retrieve current browser session cookies for a given URL safely
 */
async function getCookiesForUrl(url) {
  if (!url || typeof url !== "string") return "";
  if (!url.startsWith("http://") && !url.startsWith("https://")) return "";
  if (!chrome.cookies || !chrome.cookies.getAll) return "";

  return new Promise((resolve) => {
    try {
      chrome.cookies.getAll({ url: url }, (cookies) => {
        if (chrome.runtime.lastError || !cookies) {
          resolve("");
          return;
        }
        const cookieStr = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
        resolve(cookieStr);
      });
    } catch (e) {
      resolve("");
    }
  });
}

/**
 * Build rich request headers matching the browser session
 */
async function buildDownloadHeaders(targetUrl, refererUrl = "") {
  try {
    const cookieStr = await getCookiesForUrl(targetUrl);
    const headers = {
      "User-Agent": navigator.userAgent || "",
      "Accept": "*/*",
      "Accept-Language": navigator.language || "en-US,en;q=0.9",
    };
    if (refererUrl && (refererUrl.startsWith("http://") || refererUrl.startsWith("https://"))) {
      headers["Referer"] = refererUrl;
    }
    if (cookieStr) {
      headers["Cookie"] = cookieStr;
    }
    return headers;
  } catch (e) {
    return {
      "User-Agent": navigator.userAgent || "",
      "Accept": "*/*",
      "Accept-Language": navigator.language || "en-US,en;q=0.9",
    };
  }
}

/**
 * Send request to IDM Native Messaging Host
 */
function sendNativeMessage(payload) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, payload, (response) => {
        if (chrome.runtime.lastError) {
          console.warn("[IDM Extension] Native messaging warning:", chrome.runtime.lastError.message);
          resolve({ status: "error", error: chrome.runtime.lastError.message });
        } else {
          resolve(response || { status: "ok" });
        }
      });
    } catch (e) {
      console.error("[IDM Extension] Native messaging failed:", e);
      resolve({ status: "error", error: e.toString() });
    }
  });
}

/**
 * Check if a URL is a raw stream segment / chunk fragment rather than a complete media stream / manifest.
 */
function isMediaSegment(url) {
  if (!url || typeof url !== "string") return true;
  if (url.startsWith("blob:") || url.startsWith("data:")) return true;
  const lower = url.toLowerCase();

  // Never filter out stream manifests
  if (lower.includes(".mpd") || lower.includes(".m3u8")) {
    return false;
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

  // Filter out YouTube adaptive video chunks (videoplayback) which are un-multiplexed/soundless segments
  if (lower.includes("googlevideo.com/videoplayback") || (lower.includes("/videoplayback") && (lower.includes("expire=") || lower.includes("sparams=")))) {
    return true;
  }

  return false;
}

/**
 * Listen for network media requests (HLS, DASH, MP4, WebM, audio)
 */
if (chrome.webRequest && chrome.webRequest.onHeadersReceived) {
  chrome.webRequest.onHeadersReceived.addListener(
    async (details) => {
      try {
        const currentSettings = await getSettings();
        if (!currentSettings.videoSniffer || details.tabId < 0) return;

        const url = details.url || "";
        if (isMediaSegment(url)) return;

        const headers = details.responseHeaders || [];
        let contentType = "";
        let contentLength = 0;

        for (const h of headers) {
          const name = (h.name || "").toLowerCase();
          if (name === "content-type") {
            contentType = (h.value || "").toLowerCase();
          } else if (name === "content-length") {
            contentLength = parseInt(h.value, 10) || 0;
          }
        }

        const isMediaMime =
          contentType.startsWith("video/") ||
          contentType.startsWith("audio/") ||
          contentType.includes("mpegurl") ||
          contentType.includes("dash+xml") ||
          contentType.includes("vnd.apple.mpegurl") ||
          contentType.includes("x-mpegurl") ||
          contentType.includes("mp2t") ||
          contentType.includes("matroska") ||
          contentType.includes("flv") ||
          contentType.includes("ogg") ||
          contentType.includes("octet-stream");

        const isMediaUrl =
          url.includes(".m3u8") ||
          url.includes(".mpd") ||
          url.includes("videoplayback") ||
          url.includes(".mp4") ||
          url.includes(".webm") ||
          url.includes(".m4a") ||
          url.includes(".mp3") ||
          url.includes(".mkv") ||
          url.includes(".flv") ||
          url.includes(".ogg") ||
          url.includes(".opus") ||
          url.includes(".aac") ||
          url.includes(".ts") ||
          url.includes(".m4s");

        if (isMediaMime || isMediaUrl) {
          const mediaSet = await getTabMediaStore(details.tabId);
          if (!mediaSet.has(url)) {
            mediaSet.add(url);
            await saveTabMediaStore(details.tabId, mediaSet);

            // Update badge
            const actionApi = chrome.action || chrome.browserAction;
            if (actionApi && actionApi.setBadgeText) {
              try {
                actionApi.setBadgeText({
                  text: String(mediaSet.size),
                  tabId: details.tabId
                });
                if (actionApi.setBadgeBackgroundColor) {
                  actionApi.setBadgeBackgroundColor({
                    color: "#2b6cb0",
                    tabId: details.tabId
                  });
                }
              } catch (e) {}
            }

            // Notify content script in the active tab
            if (chrome.tabs && chrome.tabs.sendMessage) {
              chrome.tabs.sendMessage(details.tabId, {
                action: "idm_media_detected",
                streamUrl: url,
                contentType: contentType,
                size: contentLength
              }).catch(() => {});
            }
          }
        }
      } catch (err) {
        console.error("[IDM Extension] webRequest handler error:", err);
      }
    },
    { urls: ["<all_urls>"] },
    ["responseHeaders"]
  );
}

// Clean up tab media cache on tab close
if (chrome.tabs && chrome.tabs.onRemoved) {
  chrome.tabs.onRemoved.addListener((tabId) => {
    removeTabMediaStore(tabId);
  });
}

/**
 * Context Menus Setup
 */
function setupContextMenus() {
  if (!chrome.contextMenus) return;
  chrome.contextMenus.removeAll(() => {
    if (chrome.runtime.lastError) { /* ignore */ }
    chrome.contextMenus.create({
      id: "idm_download_link",
      title: "Download with IDM",
      contexts: ["link", "image", "video", "audio"]
    });

    chrome.contextMenus.create({
      id: "idm_download_all",
      title: "Download all links with IDM",
      contexts: ["page", "selection"]
    });
  });
}

chrome.runtime.onInstalled.addListener(() => {
  setupContextMenus();
});

chrome.runtime.onStartup.addListener(() => {
  setupContextMenus();
});

if (chrome.contextMenus && chrome.contextMenus.onClicked) {
  chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    try {
      const refererUrl = tab && tab.url ? tab.url : "";
      if (info.menuItemId === "idm_download_link") {
        const targetUrl = info.linkUrl || info.srcUrl || info.pageUrl;
        if (targetUrl) {
          const headers = await buildDownloadHeaders(targetUrl, refererUrl);
          sendNativeMessage({
            action: "add_download",
            url: targetUrl,
            headers: headers,
            start_immediately: true
          });
        }
      } else if (info.menuItemId === "idm_download_all") {
        if (tab && tab.id) {
          chrome.tabs.sendMessage(tab.id, { action: "extract_all_links" }, async (response) => {
            if (chrome.runtime.lastError) return;
            if (response && response.links && response.links.length > 0) {
              for (const link of response.links) {
                const headers = await buildDownloadHeaders(link.url, refererUrl);
                sendNativeMessage({
                  action: "add_download",
                  url: link.url,
                  filename: link.text || null,
                  headers: headers,
                  start_immediately: false
                });
              }
            }
          });
        }
      }
    } catch (err) {
      console.error("[IDM Extension] Context menu click error:", err);
    }
  });
}

/**
 * Universal Browser Download Interception (Chrome MV3 & Firefox MV2)
 */
const interceptedDownloadIds = new Set();

async function handleDownloadIntercept(downloadItem, suggest = null) {
  try {
    const currentSettings = await getSettings();
    if (!currentSettings.interceptDownloads || !downloadItem || !downloadItem.url) {
      if (suggest) suggest();
      return;
    }

    // Only intercept standard network protocols (skip blob:, data:, filesystem:, chrome:, etc.)
    const itemUrl = (downloadItem.url || "").toLowerCase();
    if (!itemUrl.startsWith("http://") && !itemUrl.startsWith("https://") && !itemUrl.startsWith("ftp://")) {
      if (suggest) suggest();
      return;
    }

    // Avoid recursive loops or duplicate processing
    if (interceptedDownloadIds.has(downloadItem.id)) {
      if (suggest) suggest();
      return;
    }

    const rawFilename = downloadItem.filename || "";
    const filename = rawFilename.split(/[/\\]/).pop() || "";
    const ext = filename.includes(".") ? filename.split(".").pop().toLowerCase() : "";

    // If extension is in ignore list, do not intercept
    if (ext && currentSettings.ignoreExtensions.includes(ext)) {
      if (suggest) suggest();
      return;
    }

    // Check if matches target extensions or binary media MIME types
    const mime = (downloadItem.mime || "").toLowerCase();
    const isTargetExt = ext && currentSettings.interceptExtensions.includes(ext);
    const isBinaryMime = mime.startsWith("video/") || mime.startsWith("audio/") ||
                         mime.includes("zip") || mime.includes("octet-stream") ||
                         mime.includes("pdf") || mime.includes("tar") || mime.includes("gzip");

    // In Chrome/onDeterminingFilename, if neither target extension nor binary mime, do not intercept
    const isGenericOrTarget = isTargetExt || isBinaryMime || (!ext && filename.length === 0);

    if (isGenericOrTarget && (isTargetExt || isBinaryMime || !suggest)) {
      interceptedDownloadIds.add(downloadItem.id);

      // Cancel native browser download
      if (chrome.downloads && chrome.downloads.cancel) {
        try {
          chrome.downloads.cancel(downloadItem.id, () => {
            if (chrome.runtime.lastError) { /* ignore */ }
            if (chrome.downloads.erase) {
              chrome.downloads.erase({ id: downloadItem.id }, () => {
                if (chrome.runtime.lastError) { /* ignore */ }
              });
            }
          });
        } catch (e) {}
      }

      if (suggest) {
        suggest();
      }

      const headers = await buildDownloadHeaders(downloadItem.url, downloadItem.referrer || downloadItem.url);

      sendNativeMessage({
        action: "add_download",
        url: downloadItem.url,
        filename: filename || null,
        total_bytes: downloadItem.fileSize > 0 ? downloadItem.fileSize : (downloadItem.totalBytes > 0 ? downloadItem.totalBytes : 0),
        headers: headers,
        start_immediately: true
      });
    } else {
      if (suggest) {
        suggest();
      }
    }
  } catch (err) {
    console.error("[IDM Extension] Download intercept error:", err);
    if (suggest) {
      suggest();
    }
  }
}

// 1. Chrome / Chromium / Edge / Brave: onDeterminingFilename listener (has full filename & MIME metadata)
if (chrome.downloads && chrome.downloads.onDeterminingFilename) {
  chrome.downloads.onDeterminingFilename.addListener((downloadItem, suggest) => {
    handleDownloadIntercept(downloadItem, suggest);
    return true; // Keep callback channel open for async determination if needed
  });
}

// 2. Firefox / Browser fallback without onDeterminingFilename: onCreated download listener
if (chrome.downloads && chrome.downloads.onCreated) {
  chrome.downloads.onCreated.addListener((downloadItem) => {
    // Only handle onCreated if onDeterminingFilename is not supported (e.g. Firefox)
    if (!chrome.downloads.onDeterminingFilename) {
      handleDownloadIntercept(downloadItem);
    }
  });
}

/**
 * Message Dispatcher (from content scripts & popup)
 */
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (!request || !request.action) return false;

  if (request.action === "ping_idm") {
    sendNativeMessage({ action: "ping" }).then((res) => {
      sendResponse(res);
    });
    return true;
  }

  if (request.action === "open_idm_gui") {
    sendNativeMessage({ action: "open_gui" }).then((res) => {
      sendResponse(res);
    });
    return true;
  }

  if (request.action === "query_media_formats") {
    sendNativeMessage({ action: "query_media_formats", url: request.url }).then((res) => {
      sendResponse(res);
    });
    return true;
  }

  if (request.action === "download_media") {
    (async () => {
      try {
        const pageUrl = (sender.tab && sender.tab.url) ? sender.tab.url : (request.page_url || request.referer || "");
        const headers = await buildDownloadHeaders(request.url, pageUrl);
        if (request.quality) {
          headers["quality"] = request.quality;
        }
        const res = await sendNativeMessage({
          action: "add_download",
          url: request.url,
          filename: request.filename,
          headers: headers,
          quality: request.quality || null,
          total_bytes: request.filesize || request.total_bytes || 0,
          start_immediately: true
        });
        sendResponse(res);
      } catch (err) {
        sendResponse({ status: "error", error: err.toString() });
      }
    })();
    return true;
  }

  if (request.action === "get_tab_media") {
    (async () => {
      try {
        const tabId = request.tabId;
        const mediaSet = await getTabMediaStore(tabId);
        const list = mediaSet ? Array.from(mediaSet) : [];
        sendResponse({ streams: list });
      } catch (err) {
        sendResponse({ streams: [] });
      }
    })();
    return true;
  }

  if (request.action === "get_page_metadata") {
    (async () => {
      try {
        const tabId = request.tabId;
        if (!tabId) {
          sendResponse({ thumbnail: null });
          return;
        }
        const timeoutMs = 3000;
        const timeoutPromise = new Promise((resolve) => {
          setTimeout(() => resolve({ thumbnail: null }), timeoutMs);
        });
        const messagePromise = new Promise((resolve) => {
          chrome.tabs.sendMessage(tabId, { action: "get_page_metadata" }, (response) => {
            if (chrome.runtime.lastError || !response) {
              resolve({ thumbnail: null });
            } else {
              resolve({ thumbnail: response.thumbnail || null });
            }
          });
        });
        const result = await Promise.race([messagePromise, timeoutPromise]);
        sendResponse(result);
      } catch (err) {
        sendResponse({ thumbnail: null });
      }
    })();
    return true;
  }

  if (request.action === "record_media_stream") {
    (async () => {
      try {
        const tabId = sender.tab && sender.tab.id ? sender.tab.id : request.tabId;
        if (tabId && request.url && !isMediaSegment(request.url)) {
          const mediaSet = await getTabMediaStore(tabId);
          mediaSet.add(request.url);
          await saveTabMediaStore(tabId, mediaSet);
        }
        sendResponse({ status: "ok" });
      } catch (err) {
        sendResponse({ status: "error", error: err.toString() });
      }
    })();
    return true;
  }

  if (request.action === "get_settings") {
    (async () => {
      try {
        const current = await getSettings();
        sendResponse({ settings: current });
      } catch (err) {
        sendResponse({ settings: DEFAULT_SETTINGS });
      }
    })();
    return true;
  }

  if (request.action === "save_settings") {
    (async () => {
      try {
        const current = await getSettings();
        const updated = Object.assign({}, current, request.settings || {});
        cachedSettings = updated;
        chrome.storage.local.set({ idmSettings: updated }, () => {
          if (chrome.runtime.lastError) {
            sendResponse({ status: "error", error: chrome.runtime.lastError.message });
          } else {
            sendResponse({ status: "ok" });
          }
        });
      } catch (err) {
        sendResponse({ status: "error", error: err.toString() });
      }
    })();
    return true;
  }

  return false;
});
