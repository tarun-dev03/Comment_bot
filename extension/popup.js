// Popup controller for Comment Bot YouTube Cookie Auto-Sync

const appUrlInput = document.getElementById("app-url");
const userEmailInput = document.getElementById("user-email");
const streamUrlInput = document.getElementById("stream-url");
const syncIntervalSelect = document.getElementById("sync-interval");
const autoSyncCheckbox = document.getElementById("auto-sync");
const btnSync = document.getElementById("btn-sync");
const btnUseTab = document.getElementById("btn-use-tab");
const btnSaveLink = document.getElementById("btn-save-link");
const linkSaveBadge = document.getElementById("link-save-badge");
const statusMsg = document.getElementById("status-msg");
const lastSyncTimeEl = document.getElementById("last-sync-time");
const ytDot = document.getElementById("yt-dot");
const ytText = document.getElementById("yt-text");
const ytSubtext = document.getElementById("yt-subtext");
const badgeStatus = document.getElementById("badge-status");

function formatTimeAgo(timestamp) {
  if (!timestamp) return "Never synced";
  const seconds = Math.floor((Date.now() - timestamp) / 1000);
  if (seconds < 60) return "Just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ago`;
}

function showMessage(text, type = "info") {
  statusMsg.className = `status-msg ${type}`;
  statusMsg.textContent = text;
}

function flashLinkBadge(text = "✓ Saved") {
  if (!linkSaveBadge) return;
  linkSaveBadge.textContent = text;
  linkSaveBadge.style.display = "inline";
  clearTimeout(window._linkBadgeTimer);
  window._linkBadgeTimer = setTimeout(() => {
    linkSaveBadge.style.display = "none";
  }, 2500);
}

async function loadSettings() {
  const defaults = {
    appUrl: "http://localhost:8000",
    userEmail: "",
    streamUrl: "",
    autoSyncEnabled: true,
    syncIntervalMinutes: 15,
    lastSyncTime: 0,
    lastSyncStatus: "",
    lastSyncMessage: "",
  };
  const data = await chrome.storage.local.get(defaults);

  appUrlInput.value = data.appUrl || "http://localhost:8000";
  userEmailInput.value = data.userEmail || "";
  if (streamUrlInput) streamUrlInput.value = data.streamUrl || "";
  syncIntervalSelect.value = String(data.syncIntervalMinutes || 15);
  autoSyncCheckbox.checked = Boolean(data.autoSyncEnabled);

  if (data.lastSyncTime) {
    lastSyncTimeEl.textContent = formatTimeAgo(data.lastSyncTime);
  }

  if (data.lastSyncMessage) {
    showMessage(data.lastSyncMessage, data.lastSyncStatus === "success" ? "success" : "error");
  }

  // Connect to bot app to verify credentials & sync stream URL
  const base = (data.appUrl || "http://localhost:8000").replace(/\/+$/, "");
  try {
    const meRes = await fetch(`${base}/api/me`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (meRes.ok) {
      const meData = await meRes.json();
      const foundEmail = meData.email || meData.suggested_email;
      if (foundEmail && !userEmailInput.value) {
        userEmailInput.value = foundEmail;
        await chrome.storage.local.set({ userEmail: foundEmail });
      }
      if (meData.stream_url && (!streamUrlInput.value || streamUrlInput.value === defaults.streamUrl)) {
        streamUrlInput.value = meData.stream_url;
        await chrome.storage.local.set({ streamUrl: meData.stream_url });
      }
    }
  } catch (_) {}
}

async function saveSettings() {
  const settings = {
    appUrl: appUrlInput.value.trim(),
    userEmail: userEmailInput.value.trim(),
    streamUrl: streamUrlInput ? streamUrlInput.value.trim() : "",
    autoSyncEnabled: autoSyncCheckbox.checked,
    syncIntervalMinutes: parseInt(syncIntervalSelect.value, 10) || 15,
  };
  await chrome.storage.local.set(settings);
  chrome.runtime.sendMessage({ action: "update_schedule" });
}

async function saveStreamUrl(url) {
  url = (url || (streamUrlInput ? streamUrlInput.value : "")).trim();
  if (!url) return;
  if (streamUrlInput) streamUrlInput.value = url;
  await chrome.storage.local.set({ streamUrl: url });

  const appUrl = (appUrlInput.value || "http://localhost:8000").replace(/\/+$/, "");
  try {
    const res = await fetch(`${appUrl}/api/settings/stream-url`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
      },
      credentials: "include",
      body: JSON.stringify({ stream_url: url }),
    });
    if (res.ok) {
      flashLinkBadge("✓ Kept until updated");
    }
  } catch (err) {
    flashLinkBadge("✓ Saved locally");
  }
}

async function checkYouTubeSession() {
  try {
    const res = await new Promise((resolve) => {
      chrome.runtime.sendMessage({ action: "check_youtube_cookies" }, resolve);
    });

    if (res && res.ok) {
      ytDot.className = "dot green";
      ytText.textContent = `YouTube session active (${res.count} cookies)`;
      ytSubtext.textContent = "Authentication cookies found and ready";
      badgeStatus.className = "badge connected";
      badgeStatus.textContent = "Ready";
    } else {
      ytDot.className = "dot amber";
      ytText.textContent = "YouTube login not detected";
      ytSubtext.textContent = "Open youtube.com and ensure you are logged in";
      badgeStatus.className = "badge";
      badgeStatus.textContent = "Sign-in required";
    }
  } catch (err) {
    ytDot.className = "dot red";
    ytText.textContent = "Unable to inspect cookies";
    badgeStatus.className = "badge";
    badgeStatus.textContent = "Error";
  }
}

async function handleManualSync() {
  btnSync.disabled = true;
  btnSync.textContent = "⏳ Syncing cookies...";
  showMessage("Fetching cookies and sending to Comment Bot...", "info");

  await saveSettings();

  chrome.runtime.sendMessage({ action: "manual_sync" }, (res) => {
    btnSync.disabled = false;
    btnSync.textContent = "🔄 Sync YouTube Cookie Now";

    if (res && res.ok) {
      showMessage(`✓ Synced successfully! (${res.data.count} cookies updated)`, "success");
      lastSyncTimeEl.textContent = "Just now";
    } else {
      const err = (res && res.error) || "Failed to sync. Is Comment Bot running?";
      showMessage(`Sync failed: ${err}`, "error");
    }
    checkYouTubeSession();
  });
}

function detectActiveYouTubeTab() {
  try {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs && tabs[0] && tabs[0].url) {
        const url = tabs[0].url;
        if (url.includes("youtube.com/watch") || url.includes("youtube.com/live") || url.includes("youtu.be/")) {
          btnUseTab.style.background = "#2563eb";
          btnUseTab.onclick = () => {
            saveStreamUrl(url);
          };
        }
      }
    });
  } catch (_) {}
}

// Event Listeners
btnSync.addEventListener("click", handleManualSync);
appUrlInput.addEventListener("input", saveSettings);
appUrlInput.addEventListener("change", saveSettings);
userEmailInput.addEventListener("input", saveSettings);
userEmailInput.addEventListener("change", saveSettings);
syncIntervalSelect.addEventListener("change", saveSettings);
autoSyncCheckbox.addEventListener("change", saveSettings);

if (streamUrlInput) {
  let streamTimer = null;
  streamUrlInput.addEventListener("input", (e) => {
    const val = e.target.value.trim();
    chrome.storage.local.set({ streamUrl: val });
    clearTimeout(streamTimer);
    streamTimer = setTimeout(() => saveStreamUrl(val), 500);
  });
  streamUrlInput.addEventListener("change", (e) => saveStreamUrl(e.target.value.trim()));
}

if (btnSaveLink) {
  btnSaveLink.addEventListener("click", () => saveStreamUrl());
}

if (btnUseTab) {
  btnUseTab.addEventListener("click", () => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs && tabs[0] && tabs[0].url) {
        saveStreamUrl(tabs[0].url);
      }
    });
  });
}

// Initialize
loadSettings().then(() => {
  checkYouTubeSession();
  detectActiveYouTubeTab();
});
