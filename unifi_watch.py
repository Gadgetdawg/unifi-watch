#!/usr/bin/env python3
"""
unifi-watch: outage + failover watcher for Kevin's UniFi sites.

Runs OUTSIDE the sites it watches (on Hermes / the Mac mini) so a dead site
can still raise an alarm. Polls the official UniFi Site Manager API
(https://api.ui.com) and alerts to Telegram on:

  * console offline / back online (after N consecutive confirmations)
  * active ISP change at a site (primary -> backup = failover, and back)
  * still-down reminders every REPEAT_MINUTES

Also writes:
  * state/history.jsonl  - one line per event (evidence for ISP credits)
  * state/status.json    - latest snapshot
  * state/status.html    - simple availability dashboard (uptime %, events)

Stdlib only. Python 3.9+.

Usage:
  python3 unifi_watch.py --once           # one poll, then exit (test)
  python3 unifi_watch.py --dump           # save raw API responses to state/raw/
  python3 unifi_watch.py --test-telegram  # send a test Telegram message
  python3 unifi_watch.py                  # loop forever (launchd runs this)
  python3 unifi_watch.py --mock FILE.json # use a canned API payload (testing)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_BASE = "https://api.ui.com"


# --------------------------------------------------------------------------- config
def load_env(path: Path) -> None:
    """Minimal KEY=VALUE loader (no dependency on python-dotenv)."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Config:
    def __init__(self) -> None:
        load_env(Path(os.environ.get("UNIFI_WATCH_ENV", HERE / "config.env")))
        self.api_key = os.environ.get("UI_API_KEY", "")
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.hc_url = os.environ.get("HEALTHCHECKS_URL", "")  # dead-man ping, optional
        self.poll = int(os.environ.get("POLL_SECONDS", "120"))
        self.confirm = int(os.environ.get("CONFIRM_POLLS", "2"))
        self.repeat_min = int(os.environ.get("REPEAT_MINUTES", "15"))
        self.state_dir = Path(os.environ.get("STATE_DIR", HERE / "state"))
        # Site name -> primary ISP name substring, e.g. "Beachfront=MediaCom;Creekside=AT&T"
        self.primary_isp = {}
        for pair in os.environ.get("PRIMARY_ISP", "Beachfront=MediaCom").split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                self.primary_isp[k.strip().lower()] = v.strip().lower()
        # Only watch these site/host names (comma list). Empty = all.
        self.only = {s.strip().lower() for s in os.environ.get("WATCH_ONLY", "").split(",") if s.strip()}


# --------------------------------------------------------------------------- helpers
def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def local(t_iso: str) -> str:
    """Render an ISO timestamp in the Mac's local time for humans."""
    try:
        t = dt.datetime.fromisoformat(t_iso)
        return t.astimezone().strftime("%b %d %I:%M %p %Z")
    except Exception:
        return t_iso


def dig(obj, *path, default=None):
    for p in path:
        if isinstance(obj, dict) and p in obj:
            obj = obj[p]
        else:
            return default
    return obj


def http_json(url: str, headers: dict | None = None, data: dict | None = None, timeout: int = 25):
    body = None
    hdrs = {"Accept": "application/json", "User-Agent": "unifi-watch/1.0"}
    hdrs.update(headers or {})
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "null")


# --------------------------------------------------------------------------- API
class SiteManager:
    def __init__(self, cfg: Config, mock: Path | None = None) -> None:
        self.cfg = cfg
        self.mock = json.loads(mock.read_text()) if mock else None

    def get(self, path: str):
        if self.mock is not None:
            return self.mock.get(path, {"data": []})
        if not self.cfg.api_key:
            raise SystemExit("UI_API_KEY is not set (config.env). Create one at https://unifi.ui.com -> API.")
        return http_json(API_BASE + path, {"X-API-KEY": self.cfg.api_key})

    def snapshot(self) -> dict:
        """Return {name: {online, isp, wan_uptime, external_ip, host_state, raw_state}} keyed by site name."""
        hosts = self.get("/v1/hosts").get("data") or []
        sites = self.get("/v1/sites").get("data") or []
        by_host = {}
        for h in hosts:
            name = (dig(h, "reportedState", "name") or dig(h, "reportedState", "hostname")
                    or h.get("id", "unknown"))
            state = (dig(h, "reportedState", "state") or h.get("state") or "").lower()
            online = state in ("connected", "online") if state else None
            by_host[h.get("id")] = {"name": name, "online": online, "host_state": state or "unknown"}

        out = {}
        used_hosts = set()
        for s in sites:
            used_hosts.add(s.get("hostId"))
            host = by_host.get(s.get("hostId"), {})
            name = (dig(s, "meta", "desc") or dig(s, "meta", "name") or host.get("name") or s.get("siteId"))
            # Prefer the console's name (e.g. "Beachfront") when the site is just "Default".
            if str(name).lower() in ("default", "") and host.get("name"):
                name = host["name"]
            stats = s.get("statistics") or {}
            isp = (dig(stats, "ispInfo", "name") or dig(stats, "ispInfo", "organization") or "")
            out[name] = {
                "online": host.get("online"),
                "host_state": host.get("host_state", "unknown"),
                "isp": isp,
                "wan_uptime": dig(stats, "percentages", "wanUptime"),
                "external_ip": dig(stats, "wans", "WAN", "externalIp"),
                "internet_issues": len(stats.get("internetIssues") or []),
                "offline_devices": dig(stats, "counts", "offlineDevice"),
            }
        # Hosts with no site row (e.g. old controllers) still get tracked.
        for hid, h in by_host.items():
            if hid not in used_hosts and h["name"] not in out:
                out[h["name"]] = {"online": h["online"], "host_state": h["host_state"], "isp": "",
                                  "wan_uptime": None, "external_ip": None,
                                  "internet_issues": 0, "offline_devices": None}
        if self.cfg.only:
            out = {k: v for k, v in out.items() if k.lower() in self.cfg.only}
        return out


# --------------------------------------------------------------------------- alerting
class Notifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def send(self, text: str) -> bool:
        print(f"[{iso(now())}] ALERT: {text}", flush=True)
        if not (self.cfg.tg_token and self.cfg.tg_chat):
            return False
        try:
            http_json(f"https://api.telegram.org/bot{self.cfg.tg_token}/sendMessage",
                      data={"chat_id": self.cfg.tg_chat, "text": text,
                            "disable_web_page_preview": True})
            return True
        except Exception as e:  # never let alerting kill the loop
            print(f"telegram send failed: {e}", file=sys.stderr, flush=True)
            return False


def ping_healthcheck(cfg: Config, fail: bool = False) -> None:
    if not cfg.hc_url:
        return
    try:
        urllib.request.urlopen(cfg.hc_url.rstrip("/") + ("/fail" if fail else ""), timeout=10).read()
    except Exception as e:
        print(f"healthcheck ping failed: {e}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- state machine
class Watcher:
    def __init__(self, cfg: Config, api: SiteManager, notify: Notifier) -> None:
        self.cfg, self.api, self.notify = cfg, api, notify
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = cfg.state_dir / "state.json"
        self.hist_path = cfg.state_dir / "history.jsonl"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def log(self, site: str, event: str, detail: str = "") -> None:
        rec = {"t": iso(now()), "site": site, "event": event, "detail": detail}
        with self.hist_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def is_backup(self, site: str, isp: str) -> bool | None:
        prim = self.cfg.primary_isp.get(site.lower())
        if not prim or not isp:
            return None
        return prim not in isp.lower()

    def step(self) -> dict:
        snap = self.api.snapshot()
        t = now()
        for site, cur in snap.items():
            st = self.state.setdefault(site, {"status": "unknown", "down_polls": 0, "up_polls": 0,
                                              "isp": "", "since": iso(t), "last_alert": None})
            online = cur["online"]
            # ---- online / offline with N-poll confirmation (debounce)
            if online is False:
                st["down_polls"] += 1
                st["up_polls"] = 0
                if st["status"] != "down" and st["down_polls"] >= self.cfg.confirm:
                    st.update(status="down", since=iso(t), last_alert=iso(t))
                    self.log(site, "DOWN", cur["host_state"])
                    self.notify.send(f"🔴 {site} is OFFLINE (console unreachable since ~{local(iso(t))}).")
                elif st["status"] == "down" and st["last_alert"]:
                    last = dt.datetime.fromisoformat(st["last_alert"])
                    if (t - last).total_seconds() >= self.cfg.repeat_min * 60:
                        mins = int((t - dt.datetime.fromisoformat(st["since"])).total_seconds() // 60)
                        st["last_alert"] = iso(t)
                        self.notify.send(f"🔴 {site} still OFFLINE ({mins} min).")
            elif online is True:
                st["up_polls"] += 1
                st["down_polls"] = 0
                if st["status"] == "down" and st["up_polls"] >= self.cfg.confirm:
                    mins = int((t - dt.datetime.fromisoformat(st["since"])).total_seconds() // 60)
                    st.update(status="up", since=iso(t), last_alert=None)
                    self.log(site, "UP", f"after {mins} min")
                    self.notify.send(f"🟢 {site} is back ONLINE (was down ~{mins} min).")
                elif st["status"] == "unknown":
                    st.update(status="up", since=iso(t))
            # ---- ISP / failover tracking
            isp = cur.get("isp") or ""
            if isp and st.get("isp") and isp != st["isp"]:
                backup = self.is_backup(site, isp)
                if backup is True:
                    self.log(site, "FAILOVER", f"{st['isp']} -> {isp}")
                    self.notify.send(f"🟠 {site} FAILED OVER: now on {isp} (was {st['isp']}).")
                elif backup is False:
                    self.log(site, "FAILBACK", f"{st['isp']} -> {isp}")
                    self.notify.send(f"🟢 {site} back on primary ISP {isp}.")
                else:
                    self.log(site, "ISP_CHANGE", f"{st['isp']} -> {isp}")
                    self.notify.send(f"ℹ️ {site} ISP changed: {st['isp']} -> {isp}.")
            if isp:
                st["isp"] = isp
            st["last"] = cur
            st["last_poll"] = iso(t)
        self.state_path.write_text(json.dumps(self.state, indent=2))
        (self.cfg.state_dir / "status.json").write_text(json.dumps(
            {"generated": iso(t), "sites": snap}, indent=2))
        write_dashboard(self.cfg.state_dir, self.state, self.hist_path)
        return snap


# --------------------------------------------------------------------------- dashboard
def uptime_pct(hist_path: Path, site: str, days: int = 30) -> float | None:
    """Uptime % from DOWN/UP events in history (only counts time this watcher observed)."""
    if not hist_path.exists():
        return None
    cutoff = now() - dt.timedelta(days=days)
    down_secs, down_start, first_seen = 0.0, None, None
    for line in hist_path.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("site") != site:
            continue
        t = dt.datetime.fromisoformat(r["t"])
        first_seen = first_seen or t
        if t < cutoff:
            continue
        if r["event"] == "DOWN":
            down_start = t
        elif r["event"] == "UP" and down_start:
            down_secs += (t - down_start).total_seconds()
            down_start = None
    if down_start:
        down_secs += (now() - down_start).total_seconds()
    start = max(cutoff, first_seen) if first_seen else cutoff
    window = max((now() - start).total_seconds(), 1)
    return round(100 * (1 - down_secs / window), 2)


def write_dashboard(state_dir: Path, state: dict, hist_path: Path) -> None:
    rows = []
    for site, st in sorted(state.items()):
        last = st.get("last") or {}
        color = {"up": "#1a7f37", "down": "#cf222e"}.get(st.get("status"), "#6e7781")
        up = uptime_pct(hist_path, site)
        rows.append(
            f"<tr><td><b>{html.escape(site)}</b></td>"
            f"<td style='color:{color};font-weight:600'>{html.escape(st.get('status','?').upper())}</td>"
            f"<td>{html.escape(local(st.get('since','')))}</td>"
            f"<td>{html.escape(str(last.get('isp') or '–'))}</td>"
            f"<td>{'' if last.get('wan_uptime') is None else html.escape(str(last.get('wan_uptime')))+'%'}</td>"
            f"<td>{'' if up is None else str(up)+'%'}</td>"
            f"<td>{html.escape(local(st.get('last_poll','')))}</td></tr>")
    events = []
    if hist_path.exists():
        for line in reversed(hist_path.read_text().splitlines()[-200:]):
            try:
                r = json.loads(line)
            except Exception:
                continue
            events.append(f"<tr><td>{html.escape(local(r['t']))}</td><td>{html.escape(r['site'])}</td>"
                          f"<td>{html.escape(r['event'])}</td><td>{html.escape(r.get('detail',''))}</td></tr>")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60"><title>Site availability</title>
<style>body{{font:14px -apple-system,Helvetica,sans-serif;margin:24px;color:#1f2328}}
table{{border-collapse:collapse;margin:12px 0 28px}}td,th{{padding:6px 12px;border-bottom:1px solid #d0d7de;text-align:left}}
th{{background:#f6f8fa}}</style></head><body>
<h2>Site availability</h2><p>Updated {html.escape(local(iso(now())))} · refreshes every 60 s</p>
<table><tr><th>Site</th><th>Status</th><th>Since</th><th>Active ISP</th><th>UniFi WAN uptime</th>
<th>Watcher uptime (30d)</th><th>Last poll</th></tr>{''.join(rows)}</table>
<h3>Events (newest first)</h3>
<table><tr><th>Time</th><th>Site</th><th>Event</th><th>Detail</th></tr>{''.join(events) or '<tr><td colspan=4>No events yet</td></tr>'}</table>
</body></html>"""
    (state_dir / "status.html").write_text(page)


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--find-chat", action="store_true",
                    help="print Telegram chat ids that have messaged the bot")
    ap.add_argument("--mock", type=Path)
    a = ap.parse_args()

    cfg = Config()
    notify = Notifier(cfg)
    if a.find_chat:
        if not cfg.tg_token:
            raise SystemExit("Set TELEGRAM_BOT_TOKEN in config.env first, then message your bot once.")
        upd = http_json(f"https://api.telegram.org/bot{cfg.tg_token}/getUpdates")
        chats = {}
        for u in upd.get("result", []):
            c = dig(u, "message", "chat") or dig(u, "channel_post", "chat") or {}
            if c.get("id"):
                chats[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
        if not chats:
            print("No messages yet. Send your bot any message in Telegram, then rerun.")
        for cid, who in chats.items():
            print(f"TELEGRAM_CHAT_ID={cid}   # {who}")
        return 0
    if a.test_telegram:
        ok = notify.send("✅ unifi-watch test message. Alerts are wired up.")
        print("sent" if ok else "NOT sent (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return 0 if ok else 1

    api = SiteManager(cfg, a.mock)
    if a.dump:
        raw = cfg.state_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        for p in ("/v1/hosts", "/v1/sites"):
            (raw / (p.strip("/").replace("/", "_") + ".json")).write_text(json.dumps(api.get(p), indent=2))
        print(f"raw API responses saved to {raw}")
        return 0

    w = Watcher(cfg, api, notify)
    while True:
        try:
            snap = w.step()
            print(f"[{iso(now())}] polled {len(snap)} sites: " +
                  ", ".join(f"{k}={'up' if v['online'] else 'DOWN' if v['online'] is False else '?'}"
                            f"/{v.get('isp') or '-'}" for k, v in snap.items()), flush=True)
            ping_healthcheck(cfg)
        except urllib.error.HTTPError as e:
            print(f"API HTTP {e.code}: {e.reason}", file=sys.stderr, flush=True)
            ping_healthcheck(cfg, fail=True)
        except Exception as e:
            print(f"poll error: {e!r}", file=sys.stderr, flush=True)
            ping_healthcheck(cfg, fail=True)
        if a.once or a.mock:
            return 0
        time.sleep(cfg.poll)


if __name__ == "__main__":
    sys.exit(main())
