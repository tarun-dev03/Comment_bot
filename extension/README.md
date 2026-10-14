# Comment Bot — YouTube Cookie Auto-Sync Extension

A lightweight browser extension that **automatically extracts your YouTube session cookies** and syncs them to your Comment Bot app every **10–15 minutes** (or with 1 click).

You will **never** have to open DevTools, copy cookie headers, or manually paste them into the dashboard again!

---

## 🚀 How to Install (Takes 30 seconds)

### 1. In Google Chrome, Edge, or Brave:
1. Open your browser and navigate to:
   - Chrome: `chrome://extensions`
   - Edge: `edge://extensions`
   - Brave: `brave://extensions`
2. Turn ON **Developer mode** (the toggle switch in the top-right corner).
3. Click the **Load unpacked** button (top-left).
4. Select the `extension` folder located inside this project directory (`Comment_bot/extension`).

---

## ⚙️ How It Works

1. **Click the Extension Icon** in your browser toolbar.
2. Verify:
   - **Comment Bot App URL**: Defaults to `http://localhost:8000` (or enter your deployed Render URL).
   - **Background Auto-Sync**: Checked (default every 15 minutes).
3. Click **🔄 Sync YouTube Cookie Now**.
4. The extension reads all active cookies from your logged-in YouTube session (including `SAPISID`, `SID`, `HSID`, and `SSID`) and securely transmits them to your Comment Bot backend.
5. In the background, the extension continues to keep your session cookie updated every 10–15 minutes automatically as long as your browser is open!
