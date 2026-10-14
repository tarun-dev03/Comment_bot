// Background Service Worker for Comment Bot YouTube Cookie Auto-Sync

const ALARM_NAME = "youtube_cookie_sync_alarm";

async function getStoredSettings() {
  const defaults = {
    appUrl: "http://localhost:8000",
    userEmail: "",
    autoSyncEnabled: true,
    syncIntervalMinutes: 15,
  };
  const stored = await chrome.storage.local.get(defaults);
  return { ...defaults, ...stored };
}

async function extractYouTubeCookieHeader() {
  const [ytCookies, googleCookies] = await Promise.all([
    chrome.cookies.getAll({ domain: "youtube.com" }),
    chrome.cookies.getAll({ domain: "google.com" }),
  ]);

  const cookieMap = new Map();

  // Add Google cookies first
  for (const c of googleCookies) {
    if (c.name && c.value) {
      cookieMap.set(c.name, c.value);
    }
  }

  // Add/override with YouTube specific cookies
  for (const c of ytCookies) {
    if (c.name && c.value) {
      cookieMap.set(c.name, c.value);
    }
  }

  const hasSapisid = cookieMap.has("SAPISID") || cookieMap.has("__Secure-3PAPISID");
  const hasSid = cookieMap.has("SID") || cookieMap.has("__Secure-1PSID") || cookieMap.has("__Secure-3PSID");

  if (!hasSapisid || !hasSid) {
    throw new Error(
      "Missing SAPISID or SID in browser cookies. Please open youtube.com and ensure you are signed into your account."
    );
  }

  const cookieStr = Array.from(cookieMap.entries())
    .map(([k, v]) => `${k}=${v}`)
    .join("; ");

  return {
    cookieString: cookieStr,
    count: cookieMap.size,
    hasSapisid,
    hasSid,
  };
}

async function syncCookieToBot() {
  const settings = await getStoredSettings();
  const endpoint = `${settings.appUrl.replace(/\/+$/, "")}/api/auth/cookie`;

  try {
    const extracted = await extractYouTubeCookieHeader();
    const payload = {
      cookie: extracted.cookieString,
    };
    if (settings.userEmail && settings.userEmail.trim()) {
      payload.email = settings.userEmail.trim();
    }

    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
      },
      credentials: "include",
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      let errDetail = `HTTP ${response.status}`;
      try {
        const errJson = await response.json();
        if (errJson.detail) errDetail = errJson.detail;
      } catch (_) {}
      throw new Error(`Bot API returned error: ${errDetail}`);
    }

    const result = await response.json().catch(() => ({}));
    const now = Date.now();

    await chrome.storage.local.set({
      lastSyncTime: now,
      lastSyncStatus: "success",
      lastSyncMessage: `✓ Synced ${extracted.count} cookies successfully`,
      lastUser: result.user || settings.userEmail || "current user",
    });

    chrome.action.setBadgeText({ text: "OK" });
    chrome.action.setBadgeBackgroundColor({ color: "#10b981" });

    return {
      success: true,
      count: extracted.count,
      message: result.message || "Cookie updated successfully",
      timestamp: now,
    };
  } catch (err) {
    const errorMsg = err.message || String(err);
    await chrome.storage.local.set({
      lastSyncTime: Date.now(),
      lastSyncStatus: "error",
      lastSyncMessage: `Error: ${errorMsg}`,
    });

    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#ef4444" });

    throw err;
  }
}

async function updateSyncSchedule() {
  const settings = await getStoredSettings();
  await chrome.alarms.clear(ALARM_NAME);

  if (settings.autoSyncEnabled) {
    const period = Math.max(1, parseInt(settings.syncIntervalMinutes, 10) || 15);
    chrome.alarms.create(ALARM_NAME, {
      periodInMinutes: period,
      delayInMinutes: period,
    });
    console.log(`[Comment Bot] Cookie sync alarm scheduled every ${period} minutes.`);
  } else {
    console.log("[Comment Bot] Auto-sync alarm disabled.");
  }
}

// Listen to scheduled alarms
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_NAME) {
    console.log("[Comment Bot] Auto-sync alarm fired. Fetching and syncing cookies...");
    try {
      await syncCookieToBot();
      console.log("[Comment Bot] Auto-sync complete.");
    } catch (err) {
      console.error("[Comment Bot] Auto-sync failed:", err);
    }
  }
});

// Listen to messages from popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "manual_sync") {
    syncCookieToBot()
      .then((res) => sendResponse({ ok: true, data: res }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true; // async sendResponse
  }
  if (request.action === "update_schedule") {
    updateSyncSchedule()
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  if (request.action === "check_youtube_cookies") {
    extractYouTubeCookieHeader()
      .then((res) => sendResponse({ ok: true, count: res.count }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
});

// Setup schedule on startup and install
chrome.runtime.onInstalled.addListener(() => {
  updateSyncSchedule();
});
chrome.runtime.onStartup.addListener(() => {
  updateSyncSchedule();
});
