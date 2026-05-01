# AI Trading Desk

Decision-support dashboard for GOLD/XAUUSD and BTC/BTCUSD long-only trading.
Built with Flask. Mobile-ready web app.

---

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Open in browser: `http://localhost:5000`

To enable debug mode:
```bash
set FLASK_DEBUG=1   # Windows
python app.py
```

---

## Deploy to Render (free tier)

1. Push this folder to a GitHub repository.
2. Go to [render.com](https://render.com) → New → Web Service.
3. Connect your GitHub repo.
4. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Environment:** Python 3
5. Deploy. Render sets the `PORT` variable automatically.

> **Note:** Render's free tier spins down after inactivity. First load may take ~30 seconds.

---

## Deploy to Heroku

```bash
heroku login
heroku create your-app-name
git push heroku main
heroku open
```

---

## Open on mobile

After deploying, open the public URL in your smartphone browser.

**Add to Home Screen (iOS):**
1. Open the URL in Safari.
2. Tap the Share button (box with arrow).
3. Tap "Add to Home Screen".
4. The app opens full-screen like a native app.

**Add to Home Screen (Android):**
1. Open the URL in Chrome.
2. Tap the three-dot menu → "Add to Home screen".

---

## Notes

- Data is **in-memory only** — resets on server restart. No database.
- This is a decision-support tool, not an auto-trader.
- No authentication is included. Do not expose to the public internet without adding auth.
- The sticky header badge always shows the current Controller status while scrolling.
