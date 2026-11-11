# Real-Time Parking Spot Rotation Calendar (Nov 9, 2026 – Aug 31, 2027)

Full-stack real-time parking spot rotation application for **Navya** and **Kulwinder**.

## Schedule Overview
- **Start Date:** Monday, November 9, 2026 (Week #1 — **Navya**)
- **Next Week:** Monday, November 16, 2026 (Week #2 — **Kulwinder**)
- **Rotation Cadence:** Alternates every Monday at 12:00 AM (`Monday – Sunday`)
- **End Date:** Tuesday, August 31, 2027 (296 total days across 43 rotation weeks)
  - **Navya Baseline:** 149 total days (107 weekdays)
  - **Kulwinder Baseline:** 147 total days (105 weekdays)

## 3 Ways to Share & Co-Edit with Kulwinder

### Option 1: Live Shared Server on the Same Wi-Fi / Network (Real-Time SQLite + SSE)
`server.py` binds to `0.0.0.0:8765` and prints your local network URL on startup:
```bash
./run_app.sh
```
Both you and Kulwinder can open the printed `http://<your-ip>:8765` link on laptop or phone. Any day/week swap or 1-for-1 trade is saved in `parking_schedule.db` and pushed live to both browsers via Server-Sent Events (`/api/stream`).

### Option 2: Cloud Hosting (Render / Railway / Fly.io)
Push this Git repository to GitHub and connect it to Render (uses the included [`render.yaml`](./render.yaml) and [`Procfile`](./Procfile) with zero external dependencies). Both you and Kulwinder get a permanent shared URL backed by the Python + SQLite server.

### Option 3: GitHub Pages + One-Click Shareable Sync Link (`🔗 Share Link with Kulwinder`)
Because root [`index.html`](./index.html) includes an embedded fallback engine:
1. Enable **GitHub Pages** on the repository root (`main` branch).
2. Whenever either you or Kulwinder swaps a day/week or executes a 1-for-1 trade, click **🔗 Share Link with Kulwinder** in the top bar.
3. That copies a URL with all current overrides encoded (`#sync=...`)—when the other person opens the link, their calendar automatically imports and syncs the swaps.
