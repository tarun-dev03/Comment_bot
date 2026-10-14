// Popup controller for Comment Bot YouTube Cookie Auto-Sync

const appUrlInput = document.getElementById("app-url");
const userEmailInput = document.getElementById("user-email");
const syncIntervalSelect = document.getElementById("sync-interval");
const autoSyncCheckbox = document.getElementById("auto-sync");
const btnSync = document.getElementById("btn-sync");
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

async function loadSettings() {
  const defaults = {
    appUrl: "http://localhost:8000",
    userEmail: "",
    autoSyncEnabled: true,
    syncIntervalMinutes: 15,
    lastSyncTime: 0,
    lastSyncStatus: "",
    lastSyncMessage: "",
  };
  const data = await chrome.storage.local.get(defaults);

  appUrlInput.value = data.appUrl;
  userEmailInput.value = data.userEmail;
  syncIntervalSelect.value = String(data.syncIntervalMinutes || 15);
  autoSyncCheckbox.checked = Boolean(data.autoSyncEnabled);

  if (data.lastSyncTime) {
    lastSyncTimeEl.textContent = formatTimeAgo(data.lastSyncTime);
  }

  if (data.lastSyncMessage) {
    showMessage(data.lastSyncMessage, data.lastSyncStatus === "success" ? "success" : "error");
  }
}

async function saveSettings() {
  await chrome.storage.local.set({
    appUrl: appUrlInput.value.trim(),
    userEmail: userEmailInput.value.trim(),
    autoSyncEnabled: autoSyncCheckbox.checked,
    syncIntervalMinutes: parseInt(syncIntervalSelect.value, 10) || 15,
  });
  chrome.runtime.sendMessage({ action: "update_schedule" });
}

async function checkYouTubeSession() {
  try {
    const res = await new Promise((resolve) => {
      chrome.runtime.sendMessage({ action: "check_youtube_cookies" }, resolve);
    });

    if (res && res.ok) {
      ytDot.className = "dot green";
      ytText.textContent = `YouTube session active (${res.count} cookies)`;
      ytSubtext.textContent = "SAPISID & authentication cookies found";
      badgeStatus.className = "badge connected";
      badgeStatus.textContent = "Ready";
    } else {
      ytDot.className = "dot amber";
      ytText.textContent = "YouTube login not detected";
      ytSubtext.textContent = "Open youtube.com and sign into your account";
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

// Event Listeners
btnSync.addEventListener("click", handleManualSync);
appUrlInput.addEventListener("change", saveSettings);
userEmailInput.addEventListener("change", saveSettings);
syncIntervalSelect.addEventListener("change", saveSettings);
autoSyncCheckbox.addEventListener("change", saveSettings);

// Initialize
loadSettings().then(checkYouTubeSession);
