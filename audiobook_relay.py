#!/usr/bin/env python3
"""
audiobook relay -- a small web UI for finding and grabbing audiobooks.

It borrows its entire configuration from Radarr: on startup it asks the
Radarr API for your Newznab indexers and your usenet download client
(SABnzbd or NZBGet), then searches the audiobook category (3030) across
those indexers and sends the NZB you pick straight to the download
client under its own category so Radarr never touches it.

Setup:
    pip install flask requests

Run:
    RADARR_URL=http://localhost:7878 RADARR_API_KEY=<key> python audiobook_relay.py

    (Radarr API key: Settings -> General -> Security)

Optional env vars:
    PORT               listen port                  (default 8321)
    DOWNLOAD_CATEGORY  category used on the client  (default audiobooks)
    SEARCH_CATS        Newznab categories to search (default 3030)
    INDEXER_KEYS       indexer API keys, for when Radarr masks them as
                       '********': a bare "key" applies to every indexer,
                       or "Name=key,Other Name=key2" to match by name
    CLIENT_API_KEY     SABnzbd API key, for when Radarr masks it
    CLIENT_PASSWORD    NZBGet password, for when Radarr masks it
    COMPLETED_DIR      move finished downloads here -- point it at a folder
                       inside iCloud Drive, e.g.
                       "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Audiobooks"
    NTFY_URL           optional ntfy topic URL (https://ntfy.sh/<topic>) for
                       push notifications when a download finishes
    POLL_INTERVAL      seconds between SABnzbd status polls (default 10)
    JOBS_FILE          job-state file (default relay_jobs.json beside script)

Download tracking (progress, the move to COMPLETED_DIR, notifications)
supports SABnzbd. NZBGet sends still work but are fire-and-forget.
If SABnzbd's History Retention purges a job before the relay sees it
complete, the relay falls back to locating the finished folder inside
SAB's own completed directory (read from SAB's config) and moves that.

Binds to 0.0.0.0 with no auth of its own -- intended to sit behind
Tailscale, where the tailnet is the auth boundary.
"""

import concurrent.futures
import json
import os
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
import urllib3
from flask import Flask, Response, abort, jsonify, request, send_file

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
LISTEN_PORT = int(os.environ.get("PORT", "8321"))
DOWNLOAD_CATEGORY = os.environ.get("DOWNLOAD_CATEGORY", "audiobooks")
DEFAULT_CATS = os.environ.get("SEARCH_CATS", "3030")
CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "")
CLIENT_PASSWORD = os.environ.get("CLIENT_PASSWORD", "")
TIMEOUT = 25


def _parse_indexer_keys(raw):
    """'key' applies to all indexers; 'Name=key,Other=key2' matches by name."""
    named, fallback = {}, ""
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, key = part.split("=", 1)
            named[name.strip().lower()] = key.strip()
        else:
            fallback = part
    return named, fallback


INDEXER_KEY_OVERRIDES, INDEXER_KEY_FALLBACK = _parse_indexer_keys(
    os.environ.get("INDEXER_KEYS", ""))

COMPLETED_DIR = os.environ.get("COMPLETED_DIR", "").rstrip("/\\")
NTFY_URL = os.environ.get("NTFY_URL", "")
POLL_INTERVAL = max(2, int(os.environ.get("POLL_INTERVAL", "10")))
JOBS_FILE = os.environ.get("JOBS_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "relay_jobs.json")
AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".flac", ".opus", ".ogg", ".aac", ".wma"}
TERMINAL = {"done", "failed", "lost"}


def _log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)

NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"

app = Flask(__name__)

_cfg_lock = threading.Lock()
_cfg = {"indexers": [], "client": None, "error": None, "warnings": []}


# ---------------------------------------------------------------- config

def _field(fields, name, default=None):
    """Pull a value out of a Radarr provider 'fields' list."""
    for f in fields:
        if f.get("name") == name:
            return f.get("value", default)
    return default


def _masked(value):
    """Radarr redacts sensitive fields as '********' in API responses."""
    return isinstance(value, str) and len(value) > 0 and set(value) == {"*"}


def load_config():
    """Read indexers and the usenet download client from Radarr."""
    cfg = {"indexers": [], "client": None, "error": None, "warnings": []}
    headers = {"X-Api-Key": RADARR_API_KEY}
    try:
        r = requests.get(f"{RADARR_URL}/api/v3/indexer",
                         headers=headers, timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        for ix in r.json():
            if ix.get("implementation") != "Newznab":
                continue
            if not (ix.get("enableRss") or ix.get("enableAutomaticSearch")
                    or ix.get("enableInteractiveSearch")):
                continue
            fields = ix.get("fields", [])
            base = (_field(fields, "baseUrl") or "").rstrip("/")
            path = _field(fields, "apiPath") or "/api"
            if not path.startswith("/"):
                path = "/" + path
            if base:
                name = ix.get("name") or base
                key = _field(fields, "apiKey") or ""
                override = (INDEXER_KEY_OVERRIDES.get(name.lower())
                            or INDEXER_KEY_FALLBACK)
                if override:
                    key = override
                elif _masked(key):
                    key = ""
                    cfg["warnings"].append(
                        f"{name}: Radarr hides this API key -- "
                        "supply it via INDEXER_KEYS")
                cfg["indexers"].append(
                    {"name": name, "url": base + path, "apikey": key})

        r = requests.get(f"{RADARR_URL}/api/v3/downloadclient",
                         headers=headers, timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        for dc in r.json():
            if not dc.get("enable", True) or dc.get("protocol") != "usenet":
                continue
            impl = (dc.get("implementation") or "").lower()
            fields = dc.get("fields", [])
            scheme = "https" if _field(fields, "useSsl") else "http"
            host = _field(fields, "host", "localhost")
            port = _field(fields, "port")
            url_base = (_field(fields, "urlBase") or "").strip("/")
            base = f"{scheme}://{host}:{port}" + (f"/{url_base}" if url_base else "")
            if "sabnzbd" in impl:
                key = CLIENT_API_KEY or (_field(fields, "apiKey") or "")
                if _masked(key):
                    key = ""
                    cfg["warnings"].append(
                        f"{dc.get('name', 'SABnzbd')}: Radarr hides its API key -- "
                        "set CLIENT_API_KEY (SAB: Config -> General -> API Key)")
                cfg["client"] = {"type": "sabnzbd", "name": dc.get("name", "SABnzbd"),
                                 "base": base, "apikey": key}
                break
            if "nzbget" in impl:
                password = CLIENT_PASSWORD or (_field(fields, "password") or "")
                if _masked(password):
                    password = ""
                    cfg["warnings"].append(
                        f"{dc.get('name', 'NZBGet')}: Radarr hides its password -- "
                        "set CLIENT_PASSWORD")
                cfg["client"] = {"type": "nzbget", "name": dc.get("name", "NZBGet"),
                                 "base": base,
                                 "user": _field(fields, "username") or "",
                                 "pass": password}
                break

        if not cfg["indexers"]:
            cfg["error"] = "No enabled Newznab indexers found in Radarr."
        elif not cfg["client"]:
            cfg["error"] = "No enabled SABnzbd/NZBGet client found in Radarr."
    except Exception as exc:
        cfg["error"] = f"Could not read Radarr config: {exc}"

    with _cfg_lock:
        _cfg.update(cfg)
    return cfg


def get_config():
    with _cfg_lock:
        return dict(_cfg)


# ---------------------------------------------------------------- jobs

_jobs_lock = threading.Lock()
_jobs = {}  # nzo_id -> job dict


def _save_jobs_locked():
    try:
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(list(_jobs.values()), f, indent=1)
        os.replace(tmp, JOBS_FILE)
    except Exception:
        pass


def _load_jobs():
    try:
        with open(JOBS_FILE) as f:
            for job in json.load(f):
                if isinstance(job, dict) and job.get("id"):
                    _jobs[job["id"]] = job
    except Exception:
        pass


def _prune_locked(keep=60):
    if len(_jobs) > keep:
        for job in sorted(_jobs.values(), key=lambda j: j.get("added", 0))[:-keep]:
            _jobs.pop(job["id"], None)


def new_job(job_id, title, sent_name):
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "title": title, "sent_name": sent_name,
                         "added": time.time(), "seen": False,
                         "status": "sent", "detail": "waiting for SABnzbd",
                         "dest": "", "pretty": "", "files": [], "error": ""}
        _prune_locked()
        _save_jobs_locked()
    _log(f"job {job_id}: tracking '{title}'")


def update_job(job_id, **changes):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        old_status = job.get("status")
        job.update(changes)
        _save_jobs_locked()
    new_status = changes.get("status")
    if new_status and new_status != old_status:
        _log(f"job {job_id}: {old_status} -> {new_status}"
             + (f" ({changes['error']})" if changes.get("error") else ""))


def _pretty_path(path):
    """Render paths inside the iCloud Drive container the way Finder does."""
    marker = "com~apple~CloudDocs"
    if marker in path:
        rel = path.split(marker, 1)[1].lstrip("/\\").replace("\\", "/")
        return "iCloud Drive/" + rel
    return path


def _audio_files(dest):
    if os.path.isfile(dest):
        return [os.path.basename(dest)]
    found = []
    for root, _dirs, names in os.walk(dest):
        for name in names:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                found.append(os.path.relpath(os.path.join(root, name), dest))
    return sorted(found)[:50]


def _notify(title, message):
    if not NTFY_URL:
        return
    try:
        requests.post(NTFY_URL, data=message.encode("utf-8"),
                      headers={"Title": title, "Tags": "headphones"}, timeout=10)
    except Exception:
        pass


def _unique(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for n in range(2, 100):
        candidate = f"{stem} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return path


def _finalize(job_id, title, storage):
    """SAB says Completed: move the finished job into COMPLETED_DIR."""
    if not storage or not os.path.exists(storage):
        update_job(job_id, status="failed",
                   error=f"completed, but {storage!r} does not exist here -- "
                         "is the relay running on the SABnzbd machine?")
        return
    dest = storage
    if COMPLETED_DIR:
        update_job(job_id, status="moving", detail="moving to library")
        try:
            os.makedirs(COMPLETED_DIR, exist_ok=True)
            target = _unique(os.path.join(
                COMPLETED_DIR, os.path.basename(storage.rstrip("/\\"))))
            _log(f"job {job_id}: moving {storage!r} -> {target!r}")
            shutil.move(storage, target)
            dest = target
        except Exception as exc:
            update_job(job_id, status="failed", error=f"move failed: {exc}")
            return
    update_job(job_id, status="done", detail="", dest=dest,
               pretty=_pretty_path(dest), files=_audio_files(dest))
    _notify("Audiobook ready", f"{title}\n{_pretty_path(dest)}")


_sab_conf = {"dir": "", "ts": 0.0}


def _sab_complete_dir(client):
    """Ask SABnzbd for its completed-downloads directory (cached 5 min)."""
    if time.time() - _sab_conf["ts"] < 300:
        return _sab_conf["dir"]
    path = ""
    try:
        r = requests.get(client["base"] + "/api",
                         params={"mode": "get_config", "section": "misc",
                                 "output": "json", "apikey": client["apikey"]},
                         timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        path = (r.json().get("config", {}).get("misc", {})
                .get("complete_dir") or "")
        if path and not os.path.isabs(path):
            path = ""  # relative to SAB's home dir; can't resolve from here
    except Exception:
        path = ""
    _sab_conf.update(dir=path, ts=time.time())
    return path


def _probe_completed(client, job):
    """Job vanished from SAB's history (e.g. History Retention purged it):
    look for the finished folder in SAB's completed directory instead."""
    base = _sab_complete_dir(client)
    if not base:
        return ""
    names = []
    for n in (job.get("sent_name"), job.get("title")):
        if n and n not in names:
            names += [n, n.replace(" ", "_")]
    for sub in (os.path.join(base, DOWNLOAD_CATEGORY), base):
        for name in names:
            candidate = os.path.join(sub, name)
            if os.path.exists(candidate):
                return candidate
    return ""


def _poll_once():
    client = get_config()["client"]
    if not client or client["type"] != "sabnzbd":
        return
    with _jobs_lock:
        active = [dict(j) for j in _jobs.values() if j["status"] not in TERMINAL]
    if not active:
        return
    params = {"output": "json", "apikey": client["apikey"]}
    try:
        q = requests.get(client["base"] + "/api", params={**params, "mode": "queue"},
                         timeout=TIMEOUT, verify=False).json()
        queue = {s.get("nzo_id"): s for s in q.get("queue", {}).get("slots", [])}
        h = requests.get(client["base"] + "/api",
                         params={**params, "mode": "history", "limit": "80"},
                         timeout=TIMEOUT, verify=False).json()
        history = {s.get("nzo_id"): s for s in h.get("history", {}).get("slots", [])}
    except Exception as exc:
        for job in active:
            update_job(job["id"], detail=f"SABnzbd unreachable: {exc}")
        return
    for job in active:
        job_id = job["id"]
        if job_id in queue:
            slot = queue[job_id]
            pct = slot.get("percentage", "0")
            left = slot.get("timeleft", "")
            update_job(job_id, seen=True, status="downloading",
                       detail=f"{pct}%" + (f" \u00b7 {left} left" if left else ""))
        elif job_id in history:
            slot = history[job_id]
            state = slot.get("status", "")
            if state == "Completed":
                storage = slot.get("storage") or _probe_completed(client, job)
                if storage:
                    _finalize(job_id, job["title"], storage)
                else:
                    update_job(job_id, seen=True, status="post-processing",
                               detail="finalizing")
            elif state == "Failed":
                update_job(job_id, seen=True, status="failed",
                           error=slot.get("fail_message") or "failed in SABnzbd")
            else:  # Verifying / Repairing / Extracting / Moving / Running ...
                update_job(job_id, seen=True, status="post-processing",
                           detail=state.lower() or "post-processing")
        else:
            age = time.time() - job.get("added", 0)
            if job.get("seen") or age > 60:
                found = _probe_completed(client, job)
                if found:
                    _log(f"job {job_id}: gone from SAB queue/history; "
                         f"found finished files at {found!r}")
                    _finalize(job_id, job["title"], found)
                    continue
            if age > 180:
                base = _sab_complete_dir(client) or "SAB's completed folder"
                update_job(job_id, status="lost",
                           error="vanished from SABnzbd's queue/history with no "
                                 f"matching folder under {base} -- check SAB's "
                                 "History Retention setting, then recheck")


def _poll_loop():
    while True:
        try:
            _poll_once()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------- search

def search_indexer(ix, query, cats):
    """Run one Newznab search. Returns (results, error_or_None)."""
    params = {"t": "search", "q": query, "cat": cats, "limit": "100"}
    if ix["apikey"]:
        params["apikey"] = ix["apikey"]
    results = []
    try:
        r = requests.get(ix["url"], params=params, timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip() or "(untitled)"
            enclosure = item.find("enclosure")
            url = ""
            size = 0
            if enclosure is not None:
                url = enclosure.get("url") or ""
                try:
                    size = int(enclosure.get("length") or 0)
                except ValueError:
                    size = 0
            if not url:
                url = (item.findtext("link") or "").strip()
            grabs = ""
            for attr in item.findall(NEWZNAB_NS + "attr"):
                name, value = attr.get("name"), attr.get("value", "")
                if name == "size" and not size:
                    try:
                        size = int(value)
                    except ValueError:
                        pass
                elif name == "grabs":
                    grabs = value
            age_days = None
            pub = item.findtext("pubDate")
            if pub:
                try:
                    dt = parsedate_to_datetime(pub)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_days = max(0, (datetime.now(timezone.utc) - dt).days)
                except Exception:
                    pass
            if url:
                results.append({"title": title, "url": url, "size": size,
                                "age": age_days, "grabs": grabs,
                                "indexer": ix["name"]})
        return results, None
    except Exception as exc:
        msg = re.sub(r"apikey=[^&\s]+", "apikey=<hidden>", str(exc))
        return [], f"{ix['name']}: {msg}"


# ---------------------------------------------------------------- send

def send_to_client(client, nzb_url, title):
    """Hand the NZB to the client. Returns (trackable_job_id_or_None, sent_name)."""
    safe_title = re.sub(r"[^\w\s.\-()\[\]&,']", "_", title).strip() or "audiobook"
    if client["type"] == "sabnzbd":
        r = requests.get(client["base"] + "/api", params={
            "mode": "addurl", "name": nzb_url, "nzbname": safe_title,
            "cat": DOWNLOAD_CATEGORY, "apikey": client["apikey"],
            "output": "json",
        }, timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        body = r.json()
        if not body.get("status"):
            raise RuntimeError(body.get("error") or "SABnzbd rejected the NZB")
        ids = body.get("nzo_ids") or []
        return (ids[0] if ids else None), safe_title
    else:  # nzbget
        payload = {"jsonrpc": "2.0", "id": 1, "method": "append",
                   "params": [safe_title + ".nzb", nzb_url, DOWNLOAD_CATEGORY,
                              0, False, False, "", 0, "SCORE"]}
        r = requests.post(client["base"] + "/jsonrpc", json=payload,
                          auth=(client["user"], client["pass"]),
                          timeout=TIMEOUT, verify=False)
        r.raise_for_status()
        body = r.json()
        result = body.get("result")
        if body.get("error") or (isinstance(result, int) and result <= 0):
            raise RuntimeError(str(body.get("error") or "NZBGet rejected the NZB"))
        return None, safe_title  # tracking is SABnzbd-only for now


# ---------------------------------------------------------------- routes

@app.route("/api/status")
def api_status():
    if request.args.get("refresh"):
        load_config()
    cfg = get_config()
    client = cfg["client"]
    warnings = list(cfg.get("warnings", []))
    if not COMPLETED_DIR:
        warnings.append("COMPLETED_DIR not set -- finished downloads will stay "
                        "in SABnzbd's completed folder")
    return jsonify({
        "indexers": [ix["name"] for ix in cfg["indexers"]],
        "client": ({"name": client["name"], "type": client["type"],
                    "base": client["base"]} if client else None),
        "category": DOWNLOAD_CATEGORY,
        "completedPretty": _pretty_path(COMPLETED_DIR) if COMPLETED_DIR else "",
        "defaultCats": DEFAULT_CATS,
        "error": cfg["error"],
        "warnings": warnings,
    })


@app.route("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    cats = (request.args.get("cats") or DEFAULT_CATS).strip() or DEFAULT_CATS
    if not query:
        return jsonify({"error": "Empty query."}), 400
    cfg = get_config()
    if not cfg["indexers"]:
        return jsonify({"error": cfg["error"] or "No indexers configured."}), 503
    results, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(search_indexer, ix, query, cats)
                   for ix in cfg["indexers"]]
        for fut in concurrent.futures.as_completed(futures):
            found, err = fut.result()
            results.extend(found)
            if err:
                errors.append(err)
    results.sort(key=lambda x: (x["age"] is None, x["age"] or 0))
    return jsonify({"results": results, "errors": errors})


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(silent=True) or {}
    url, title = data.get("url"), data.get("title", "audiobook")
    if not url:
        return jsonify({"error": "Missing NZB url."}), 400
    client = get_config()["client"]
    if not client:
        return jsonify({"error": "No download client configured."}), 503
    try:
        job_id, sent_name = send_to_client(client, url, title)
        if job_id:
            new_job(job_id, title, sent_name)
        return jsonify({"ok": True, "tracked": bool(job_id)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/jobs/<job_id>/recheck", methods=["POST"])
def api_job_recheck(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job["status"] not in ("failed", "lost"):
            return jsonify({"error": "only failed/lost jobs can be rechecked"}), 400
        job.update(status="sent", detail="rechecking", error="",
                   added=time.time(), seen=True)
        _save_jobs_locked()
    _log(f"job {job_id}: recheck requested")
    return jsonify({"ok": True})


@app.route("/api/jobs")
def api_jobs():
    with _jobs_lock:
        jobs = sorted(_jobs.values(),
                      key=lambda j: j.get("added", 0), reverse=True)
        return jsonify([dict(j) for j in jobs])


@app.route("/api/jobs/clear", methods=["POST"])
def api_jobs_clear():
    with _jobs_lock:
        for job_id in [i for i, j in _jobs.items() if j["status"] in TERMINAL]:
            _jobs.pop(job_id, None)
        _save_jobs_locked()
    return jsonify({"ok": True})


@app.route("/files/<job_id>/<path:relpath>")
def serve_file(job_id, relpath):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["status"] != "done" or not job.get("dest"):
        abort(404)
    base = os.path.realpath(job["dest"])
    full = os.path.realpath(os.path.join(base, relpath)) \
        if os.path.isdir(base) else base
    if full != base and not full.startswith(base + os.sep):
        abort(403)
    if not os.path.isfile(full):
        abort(404)
    return send_file(full, conditional=True)


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>audiobook relay</title>
<style>
  :root {
    --bg: #131110; --panel: #1b1815; --line: #2d2823;
    --amber: #e0a458; --amber-dim: #8a6a3e;
    --text: #d3c9ba; --dim: #837a6d;
    --ok: #8fae5d; --err: #c9705a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 ui-monospace, "Cascadia Code", Consolas, Menlo, monospace;
  }
  .wrap { max-width: 860px; margin: 0 auto; padding: 28px 16px 64px; }
  header h1 {
    margin: 0; font-size: 16px; font-weight: 600;
    color: var(--amber); letter-spacing: .35em; text-transform: uppercase;
  }
  header .sub { color: var(--dim); font-size: 12px; margin-top: 2px; }
  #status {
    margin: 18px 0 22px; padding: 10px 12px;
    border: 1px solid var(--line); border-left: 3px solid var(--amber-dim);
    background: var(--panel); font-size: 12px; color: var(--dim);
    overflow-wrap: anywhere;
  }
  #status.bad { border-left-color: var(--err); color: var(--err); }
  #status .warn { color: var(--err); margin-top: 4px; }
  #status a { color: var(--amber); cursor: pointer; text-decoration: underline; }
  form { display: flex; gap: 8px; flex-wrap: wrap; }
  input {
    background: var(--panel); border: 1px solid var(--line);
    color: var(--text); font: inherit; padding: 9px 11px; border-radius: 2px;
  }
  input:focus, button:focus-visible { outline: 2px solid var(--amber); outline-offset: 1px; }
  #q { flex: 1 1 240px; }
  #cats { width: 110px; color: var(--dim); }
  button {
    background: none; border: 1px solid var(--amber-dim); color: var(--amber);
    font: inherit; padding: 9px 16px; border-radius: 2px; cursor: pointer;
  }
  button:hover:enabled { border-color: var(--amber); background: #241e16; }
  button:disabled { opacity: .5; cursor: default; }
  #note { margin: 18px 0 6px; color: var(--dim); font-size: 12px; min-height: 18px; }
  #errors { color: var(--err); font-size: 12px; white-space: pre-line; }
  .row {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 8px; border-bottom: 1px solid var(--line);
  }
  .row:hover { background: var(--panel); box-shadow: inset 3px 0 0 var(--amber-dim); }
  .info { flex: 1; min-width: 0; }
  .title { overflow-wrap: anywhere; }
  .meta { color: var(--dim); font-size: 12px; margin-top: 2px; }
  .send { padding: 6px 12px; flex-shrink: 0; }
  .send.ok { border-color: var(--ok); color: var(--ok); }
  .send.err { border-color: var(--err); color: var(--err); }
  #jobs { margin: 22px 0 0; border: 1px solid var(--line); background: var(--panel); padding: 10px 12px; }
  #jobs-head { color: var(--amber); font-size: 12px; letter-spacing: .2em; text-transform: uppercase; }
  #jobs-head a { color: var(--amber); cursor: pointer; text-transform: none;
                 letter-spacing: 0; margin-left: 10px; text-decoration: underline; }
  .job { padding: 8px 0 2px; border-top: 1px solid var(--line); margin-top: 8px; }
  #jobs-list .job:first-child { border-top: none; margin-top: 2px; }
  .jtitle { overflow-wrap: anywhere; }
  .jmeta { font-size: 12px; color: var(--dim); margin-top: 2px; overflow-wrap: anywhere; }
  .jmeta.jok { color: var(--ok); }
  .jmeta.jerr { color: var(--err); }
  .jmeta a { color: var(--amber); text-decoration: underline; cursor: pointer; margin-left: 8px; }
  .jfiles { margin: 4px 0 6px; font-size: 12px; display: flex; flex-direction: column; gap: 3px; }
  .jfiles a { color: var(--amber); overflow-wrap: anywhere; }
  @media (prefers-reduced-motion: no-preference) {
    .row, button, input { transition: background .12s, border-color .12s; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Audiobook Relay</h1>
    <div class="sub">radarr-linked usenet search</div>
  </header>

  <div id="status">connecting to radarr&hellip;</div>

  <form id="form">
    <input id="q" type="search" placeholder="title or author" autofocus>
    <input id="cats" title="Newznab categories (3030 = audiobooks)">
    <button id="go" type="submit">Search</button>
  </form>

  <section id="jobs" hidden>
    <div id="jobs-head">Downloads <span id="jobs-count"></span>
      <a id="notif" hidden>[enable notifications]</a>
      <a id="clearjobs">[clear finished]</a>
    </div>
    <div id="jobs-list"></div>
  </section>

  <div id="note">Results are sent to your download client, not to Radarr.</div>
  <div id="errors"></div>
  <div id="results"></div>
</div>

<script>
const $ = id => document.getElementById(id);

function fmtSize(bytes) {
  if (!bytes) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + " " + units[i];
}
function fmtAge(days) {
  if (days === null || days === undefined) return "?";
  if (days < 1) return "today";
  if (days < 365) return days + "d";
  return (days / 365).toFixed(1) + "y";
}

async function loadStatus(refresh) {
  const el = $("status");
  try {
    const res = await fetch("/api/status" + (refresh ? "?refresh=1" : ""));
    const s = await res.json();
    if (!$("cats").value) $("cats").value = s.defaultCats;
    el.className = s.error ? "bad" : "";
    el.textContent = s.error
      ? s.error + "  "
      : s.indexers.length + " indexer" + (s.indexers.length === 1 ? "" : "s")
        + " (" + s.indexers.join(", ") + ")  \u00b7  "
        + s.client.name + " @ " + s.client.base
        + "  \u2192  category \u201c" + s.category + "\u201d  "
        + (s.completedPretty ? "\u00b7  library: " + s.completedPretty + "  " : "");
    const a = document.createElement("a");
    a.textContent = "[reload]";
    a.onclick = () => loadStatus(true);
    el.appendChild(a);
    for (const w of (s.warnings || [])) {
      const d = document.createElement("div");
      d.className = "warn"; d.textContent = "\u26a0 " + w;
      el.appendChild(d);
    }
  } catch (e) {
    el.className = "bad";
    el.textContent = "relay backend unreachable: " + e;
  }
}

function render(items) {
  const box = $("results");
  box.textContent = "";
  for (const it of items) {
    const row = document.createElement("div"); row.className = "row";
    const info = document.createElement("div"); info.className = "info";
    const title = document.createElement("div"); title.className = "title";
    title.textContent = it.title;
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = [fmtSize(it.size), fmtAge(it.age),
                        it.grabs ? it.grabs + " grabs" : null, it.indexer]
                       .filter(Boolean).join(" \u00b7 ");
    info.append(title, meta);
    const btn = document.createElement("button");
    btn.className = "send"; btn.textContent = "send";
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = "\u2026";
      try {
        const res = await fetch("/api/send", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({url: it.url, title: it.title}),
        });
        const body = await res.json();
        if (!res.ok) throw new Error(body.error || res.status);
        btn.textContent = "sent \u2713"; btn.className = "send ok";
        pollJobs();
      } catch (e) {
        btn.textContent = "failed"; btn.className = "send err";
        btn.title = String(e); btn.disabled = false;
      }
    };
    row.append(info, btn);
    box.appendChild(row);
  }
}

const TERM = new Set(["done", "failed", "lost"]);
let known = null, jobsTimer = null;

function maybeNotify(jobs) {
  if (known === null) {           // first poll: baseline, no notifications
    known = {};
    for (const j of jobs) known[j.id] = j.status;
    return;
  }
  for (const j of jobs) {
    const prev = known[j.id];
    known[j.id] = j.status;
    if (prev === j.status || !TERM.has(j.status)) continue;
    if (("Notification" in window) && Notification.permission === "granted") {
      new Notification(j.status === "done" ? "Audiobook ready" : "Download " + j.status,
                       {body: j.title + (j.pretty ? "\n" + j.pretty : "")});
    }
  }
}

function renderJobs(jobs) {
  const panel = $("jobs"), list = $("jobs-list");
  if (!jobs.length) { panel.hidden = true; return; }
  panel.hidden = false;
  const act = jobs.filter(j => !TERM.has(j.status)).length;
  $("jobs-count").textContent = act ? "(" + act + " active)" : "";
  list.textContent = "";
  for (const j of jobs) {
    const row = document.createElement("div"); row.className = "job";
    const t = document.createElement("div"); t.className = "jtitle";
    t.textContent = j.title;
    const m = document.createElement("div"); m.className = "jmeta";
    if (j.status === "done") {
      m.classList.add("jok");
      m.textContent = "done \u2192 " + (j.pretty || j.dest);
    } else if (TERM.has(j.status)) {
      m.classList.add("jerr");
      m.textContent = j.status + ": " + (j.error || "");
      const r = document.createElement("a");
      r.textContent = "[recheck]";
      r.onclick = async () => {
        await fetch("/api/jobs/" + encodeURIComponent(j.id) + "/recheck",
                    {method: "POST"});
        pollJobs();
      };
      m.appendChild(r);
    } else {
      m.textContent = j.status + (j.detail ? " \u00b7 " + j.detail : "");
    }
    row.append(t, m);
    if (j.status === "done" && j.files && j.files.length) {
      const box = document.createElement("div"); box.className = "jfiles";
      for (const rel of j.files) {
        const a = document.createElement("a");
        a.href = "/files/" + encodeURIComponent(j.id) + "/"
               + rel.split("/").map(encodeURIComponent).join("/");
        a.textContent = "\u25b8 " + rel;
        box.appendChild(a);
      }
      row.appendChild(box);
    }
    list.appendChild(row);
  }
}

async function pollJobs() {
  clearTimeout(jobsTimer);
  let active = false;
  try {
    const res = await fetch("/api/jobs");
    const jobs = await res.json();
    maybeNotify(jobs);
    renderJobs(jobs);
    active = jobs.some(j => !TERM.has(j.status));
  } catch (e) { /* backend briefly unreachable; retry on schedule */ }
  jobsTimer = setTimeout(pollJobs, active ? 4000 : 15000);
}

if (("Notification" in window) && Notification.permission === "default") {
  const link = $("notif");
  link.hidden = false;
  link.onclick = () => Notification.requestPermission()
    .then(() => { link.hidden = Notification.permission !== "default"; });
}
$("clearjobs").onclick = async () => {
  await fetch("/api/jobs/clear", {method: "POST"});
  pollJobs();
};

$("form").onsubmit = async (ev) => {
  ev.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  $("go").disabled = true;
  $("note").textContent = "searching\u2026";
  $("errors").textContent = "";
  $("results").textContent = "";
  try {
    const res = await fetch("/api/search?q=" + encodeURIComponent(q)
                            + "&cats=" + encodeURIComponent($("cats").value.trim()));
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.status);
    $("note").textContent = body.results.length
      ? body.results.length + " results, newest first"
      : "no results";
    $("errors").textContent = (body.errors || []).join("\n");
    render(body.results);
  } catch (e) {
    $("note").textContent = "";
    $("errors").textContent = String(e);
  }
  $("go").disabled = false;
};

loadStatus(false);
pollJobs();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if not RADARR_API_KEY:
        raise SystemExit("Set RADARR_API_KEY (Radarr: Settings -> General -> Security).")
    load_config()
    _load_jobs()
    threading.Thread(target=_poll_loop, daemon=True).start()
    cfg = get_config()
    print(f"audiobook relay on http://0.0.0.0:{LISTEN_PORT}"
          f"  |  {len(cfg['indexers'])} indexer(s)"
          f"  |  client: {cfg['client']['name'] if cfg['client'] else 'none'}"
          f"  |  library: {COMPLETED_DIR or '(COMPLETED_DIR not set; finished jobs stay in SAB)'}"
          + (f"  |  WARNING: {cfg['error']}" if cfg["error"] else ""))
    app.run(host="0.0.0.0", port=LISTEN_PORT)
