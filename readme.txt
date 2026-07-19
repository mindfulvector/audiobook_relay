audiobook relay -- a small web UI for finding and grabbing audiobooks.

It borrows its entire configuration from Radarr: on startup it asks the
Radarr API for your Newznab indexers and your usenet download client
(SABnzbd or NZBGet), then searches the audiobook category (3030) across
those indexers and sends the NZB you pick straight to the download
client under its own category so Radarr never touches it.

Setup:
    python3 -m pip install -r requirements.txt

Run:
    cp run.sh.example run.sh
    # now run.sh env vars to be correct for your system
    # Get Radarr API key here: Settings -> General -> Security
    ./run.sh

Optional env vars (set in run.sh):
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
