# unifi-watch

Outage and failover alerts for Kevin's UniFi sites (Beachfront, Creekside, Westcroft),
sent to Telegram, plus a simple availability dashboard.

**Why it runs on Hermes (the Mac mini) and not at the sites:** when a site goes dark,
its own UniFi push alerts have no way out. This watcher sits outside every site and
polls the official UniFi Site Manager API, so a dead site still raises an alarm.

## What it alerts on
| Event | Message | When |
|---|---|---|
| Console offline | 🔴 `Beachfront is OFFLINE` | 2 bad polls in a row (~4 min) |
| Still down | 🔴 `still OFFLINE (N min)` | every 15 min until it recovers |
| Back online | 🟢 `back ONLINE (was down ~N min)` | 2 good polls in a row |
| Failover | 🟠 `FAILED OVER: now on AT&T Wireless` | active ISP is not the site's primary |
| Failback | 🟢 `back on primary ISP MediaCom` | |

Every event is appended to `state/history.jsonl`, which is your evidence log for ISP credits.
`state/status.html` is a self-refreshing dashboard: status, active ISP, uptime and an event list.

## One-time setup (about 10 minutes)
1. **UniFi API key:** go to https://unifi.ui.com, open your profile menu, choose **API**, then **Create API Key**. The key is read-only.
2. **Telegram bot:** in Telegram, message **@BotFather**, send `/newbot`, and copy the token. Then send your new bot any message.
3. **Deploy from the Studio** (first run creates `config.env` on the mini and stops):
   ```bash
   cd ~/repos && git clone git@github.com:<owner>/unifi-watch.git && cd unifi-watch
   ./deploy/deploy.sh                       # defaults to hermes@hermes-mini
   ```
4. **Fill in the secrets on the mini:** edit `~/unifi-watch/config.env` and add `UI_API_KEY` and `TELEGRAM_BOT_TOKEN`. Run
   `python3 ~/unifi-watch/unifi_watch.py --find-chat` to get `TELEGRAM_CHAT_ID`, then
   `python3 ~/unifi-watch/unifi_watch.py --test-telegram`.
5. **Deploy again:** `./deploy/deploy.sh`. It runs the tests, does one live poll, and installs the launchd job.
   Output is saved to `deploy/last-deploy.log`.

**Optional:** add a free https://healthchecks.io check and set `HEALTHCHECKS_URL`. If the watcher
or the mini dies, Healthchecks emails or texts you. This is the "watch the watcher" layer.

## Useful commands (on the mini)
```bash
python3 unifi_watch.py --once     # one poll, prints status, updates dashboard
python3 unifi_watch.py --dump     # saves raw API JSON to state/raw/ (schema debugging)
tail -f ~/Library/Logs/unifi-watch.log
launchctl kickstart -k gui/$(id -u)/com.herrin.unifi-watch   # restart
```

## Design notes
- Standard library only, Python 3.9+. Nothing to install.
- Read-only. It never changes anything in UniFi.
- The API field names are parsed defensively. If the first live poll shows `?` for status or a blank ISP,
  run `--dump` and adjust `SiteManager.snapshot()` against the real JSON.
- Offline tests: `python3 -m unittest discover -s tests`.

## Roadmap (small steps, each testable on its own)
1. ✅ Console up/down plus failover alerts to Telegram, history log, dashboard
2. Yale lock presence: watch the lock's Wi-Fi client through the Site Manager connector proxy, and alert if it drops off
3. Instant alerts: UniFi Alarm Manager webhooks into Hermes, for degradation while the site is still up
4. Publish the dashboard (Hermes web route or a static page)
5. Automated lock bounce after WAN failover/failback, once the manual block/unblock test proves it works
