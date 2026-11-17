# Troubleshooting

First look at the logs (Mac: `tail -50 ~/Library/Logs/calendar-optimizer.log`, Linux:
`journalctl -u calendar-optimizer -n 50`, or the terminal if you started it by hand).

| Problem | Fix |
|---|---|
| Notion sync fails with **404 not found** | The database is not shared with your integration: open it → `•••` → **Connections** → add it. |
| Notion sync fails with **401** | `NOTION_TOKEN` is wrong or was reset; copy it again from your integration. |
| "Notion returned no rows at all" | The database ID points somewhere empty, or the share was removed. Nothing was changed. |
| Google sync: **invalid_grant** | The sign-in expired. Delete `backend/google_token.json` and run `.venv/bin/python3 backend/Google_cal_pull.py` again. If it expires every week, publish the OAuth consent screen (Google Cloud → OAuth consent screen → **Publish app**). |
| A calendar never shows up | Check its link in `.env` (Apple: `https://`, not `webcal://`) and that the computer can open it in a browser. |
| Claude calls fail | Run `.venv/bin/python3 backend/llm_client.py --check`; it says what is missing without spending anything. |
| Times are off by hours | Set `TIMEZONE` in `.env`, restart, and rebuild the app (`cd frontend && npm run build`). |
| "Address already in use" on port 8000 | Another copy is running (or another program uses 8000). Stop it, or check `lsof -i :8000`. |
| The plan never runs on Sunday | The computer was asleep or off at 06:30. Use **Re-plan** in the Plan tab, and see [always-on.md](always-on.md). |
| The phone can't reach it | Same Wi-Fi: use the computer's local IP and port 8000, and allow incoming connections if the firewall asks. Tailscale: both devices signed in, and `tailscale serve status` shows port 8000. |
| Changes in `.env` do nothing | Restart: `./deploy/deploy.sh` (or `Ctrl+C` and start again). |
| No review emails | `NOTIFY_EMAIL_TO` is empty, or the SMTP password is your normal password; Gmail needs an app password. |

Quick checks while it runs: `http://localhost:8000/health`, `http://localhost:8000/health/llm`
and `http://localhost:8000/schedule`.
