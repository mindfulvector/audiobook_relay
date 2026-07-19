audiobook relay -- a small web UI for finding and grabbing audiobooks.

It borrows its entire configuration from Radarr: on startup it asks the
Radarr API for your Newznab indexers and your usenet download client
(SABnzbd or NZBGet), then searches the audiobook category (3030) across
those indexers and sends the NZB you pick straight to the download
client under its own category so Radarr never touches it. Tracked
downloads show live progress in the UI; on completion the finished
folder is moved into your library (COMPLETED_DIR) and you get a
notification with the file's location.

Prerequisites
    - Python 3.8+ and pip. Python dependencies (requirements.txt):
      Flask and requests -- nothing else.
    - Radarr, running and reachable from the relay, with:
        - at least one Newznab indexer configured and enabled. The relay
          searches whatever Radarr has; in this setup that is
          nzbfinder.ws. You need your own account and API key for each
          indexer (the nzbfinder key is on your account's API page).
        - a usenet download client configured (SABnzbd or NZBGet).
    - SABnzbd running on the SAME machine as the relay -- the
      completed-download move is a local filesystem operation, and the
      history-purge fallback probes SAB's completed directory directly.
    - par2 and unrar available to SABnzbd for verify/repair/unpack (this
      is what reassembles multi-part posts into finished files). The
      Windows and macOS SAB installers bundle both; on Linux install
      them with your package manager.
    - A Usenet provider account configured inside SABnzbd, e.g.
      Newshosting: news.newshosting.com, port 563, SSL, with your
      account credentials.
    - Because current Radarr masks stored secrets as '********' in its
      API, the relay cannot read your indexer or SAB keys through
      Radarr. In practice you must also supply INDEXER_KEYS and
      CLIENT_API_KEY (see below).
    - Optional: an "audiobooks" category in SABnzbd so finished jobs get
      their own subfolder (without it SAB uses the Default category; the
      relay still finds the job either way).
    - Optional: Tailscale for remote access, and the ntfy app subscribed
      to your NTFY_URL topic if you want push notifications on your
      phone.

macOS note: if COMPLETED_DIR points inside iCloud Drive
("$HOME/Library/Mobile Documents/com~apple~CloudDocs/..."), the process
running the relay needs permission to write there -- grant Terminal (or
whatever launches run.sh) Full Disk Access under System Settings ->
Privacy & Security, or the move fails with "Operation not permitted".
The machine must be signed into iCloud; the relay only does the local
move, and iCloud handles the sync upstream.

Setup:
    python3 -m pip install -r requirements.txt

Run:
    cp run.sh.example run.sh
    # edit the env vars in run.sh to be correct for your system
    ./run.sh

Required env vars (set in run.sh):
    RADARR_URL         e.g. http://localhost:7878
    RADARR_API_KEY     Radarr: Settings -> General -> Security
    INDEXER_KEYS       needed in practice; see below
    CLIENT_API_KEY     needed in practice; SABnzbd: Config -> General ->
                       API Key

Optional env vars (set in run.sh):
    PORT               listen port                  (default 8321)
    DOWNLOAD_CATEGORY  category used on the client  (default audiobooks)
    SEARCH_CATS        Newznab categories to search (default 3030;
                       some audiobooks hide in 3010/MP3)
    INDEXER_KEYS       indexer API keys, for when Radarr masks them as
                       '********': a bare "key" applies to every indexer,
                       or "Name=key,Other Name=key2" to match by the
                       indexer names as they appear in Radarr
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

Notifications: the in-page Downloads panel always updates while the tab
is open. Native browser notifications (the [enable notifications] link)
only work over HTTPS or localhost -- plain HTTP across Tailscale won't
fire them; "tailscale serve" can front the port with HTTPS if you want
that. For notifications that reach your phone regardless, set NTFY_URL.

Binds to 0.0.0.0 with no auth of its own -- intended to sit behind
Tailscale, where the tailnet is the auth boundary.

Files
    audiobook_relay.py   the whole app (UI embedded)
    requirements.txt     flask, requests
    run.sh.example       template for env vars + launch
    relay_jobs.json      created at runtime beside the script; persists
                         download tracking state across restarts
