// Background Service Worker for Comment Bot YouTube Cookie Auto-Sync

const ALARM_NAME = "youtube_cookie_sync_alarm";

async function getStoredSettings() {
  const defaults = {
    appUrl: "http://localhost:8000",
    userEmail: "",
    syncToken: "",
    autoSyncEnabled: true,
    syncIntervalMinutes: 15,
  };
  const stored = await chrome.storage.local.get(defaults);
  return { ...defaults, ...stored };
}

async function extractYouTubeCookieHeader() {
  // Read strictly YouTube cookies so accounts.google.com cookies never overwrite YouTube sessions
  const primaryCookies = await chrome.cookies.getAll({ url: "https://www.youtube.com" }).catch(() => []);
  const domainCookies = await chrome.cookies.getAll({ domain: "youtube.com" }).catch(() => []);

  const cookieMap = new Map();
  for (const c of primaryCookies) {
    if (c && c.name && c.value) {
      cookieMap.set(c.name, c.value);
    }
  }
  for (const c of domainCookies) {
    if (c && c.name && c.value && !cookieMap.has(c.name)) {
      cookieMap.set(c.name, c.value);
    }
  }

  const hasSapisid =
    cookieMap.has("SAPISID") ||
    cookieMap.has("__Secure-3PAPISID") ||
    cookieMap.has("__Secure-1PAPISID") ||
    cookieMap.has("APISID");

  const hasSid =
    cookieMap.has("SID") ||
    cookieMap.has("__Secure-1PSID") ||
    cookieMap.has("__Secure-3PSID") ||
    cookieMap.has("SSID") ||
    cookieMap.has("__Secure-1PSSID") ||
    cookieMap.has("__Secure-3PSSID") ||
    cookieMap.has("LOGIN_INFO");

  if (!hasSapisid || !hasSid) {
    throw new Error(
      "Missing YouTube authentication cookies. Please open youtube.com and ensure you are signed into your account."
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

async function syncCookieToBot(isAutoSync = false) {
  const settings = await getStoredSettings();
  const base = (settings.appUrl || "http://localhost:8000").replace(/\/+$/, "");
  const endpoint = `${base}/api/auth/cookie`;

  try {
    const extracted = await extractYouTubeCookieHeader();
    let syncToken = (settings.syncToken || "").trim();
    let emailToSend = (settings.userEmail || "").trim().toLowerCase();

    // If no syncToken is configured yet, try fetching from currently signed-in dashboard session
    if (!syncToken) {
      try {
        const tokRes = await fetch(`${base}/api/auth/sync-token`, {
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        if (tokRes.ok) {
          const tokData = await tokRes.json();
          if (tokData.sync_token) {
            if (emailToSend && tokData.email && tokData.email.toLowerCase() !== emailToSend) {
              throw new Error(
                `Account mismatch: extension is set to ${emailToSend}, but dashboard is signed in as ${tokData.email}.`
              );
            }
            syncToken = tokData.sync_token;
            emailToSend = (tokData.email || emailToSend).toLowerCase();
            await chrome.storage.local.set({ syncToken, userEmail: emailToSend });
          }
        }
      } catch (err) {
        if (err && err.message && err.message.startsWith("Account mismatch")) {
          throw err;
        }
      }
    }

    if (!syncToken) {
      throw new Error(
        "Missing Extension Sync Key. Sign in to Comment Bot or paste your Sync Key from Dashboard -> Setup Cookie Mode."
      );
    }

    const payload = {
      cookie: extracted.cookieString,
      auto_sync: Boolean(isAutoSync),
    };
    if (emailToSend) {
      payload.email = emailToSend;
    }

    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": `Bearer ${syncToken}`,
      },
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
    const activeUser = result.user || emailToSend || "active user";

    await chrome.storage.local.set({
      lastSyncTime: now,
      lastSyncStatus: "success",
      lastSyncMessage: `✓ Synced ${extracted.count} cookies for ${activeUser}`,
      lastUser: activeUser,
      userEmail: activeUser,
    });

    chrome.action.setBadgeText({ text: "OK" });
    chrome.action.setBadgeBackgroundColor({ color: "#68d4a6" });

    return {
      success: true,
      count: extracted.count,
      message: result.message || "Cookie updated successfully",
      timestamp: now,
      user: activeUser,
    };
  } catch (err) {
    const errorMsg = err.message || String(err);
    await chrome.storage.local.set({
      lastSyncTime: Date.now(),
      lastSyncStatus: "error",
      lastSyncMessage: `Error: ${errorMsg}`,
    });

    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#f3a68c" });

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
      await syncCookieToBot(true);
      console.log("[Comment Bot] Auto-sync complete.");
    } catch (err) {
      console.error("[Comment Bot] Auto-sync failed:", err);
    }
  }
});

// Listen to messages from popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "manual_sync") {
    syncCookieToBot(false)
      .then((res) => sendResponse({ ok: true, data: res }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
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
