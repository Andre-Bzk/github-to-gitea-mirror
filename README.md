# GitHub → Gitea Mirror

[English](#english) | [Deutsch](#deutsch)

---

## English

Mirrors **all** GitHub repositories of a user into a self-hosted Gitea instance
as pull mirrors — including repositories created later.

Gitea can already mirror a single repository through its web UI. What it cannot
do is watch an account and pick up new repositories on its own. That gap is the
only reason this script exists: a daily cron job asks the GitHub API which
repositories exist and creates the mirrors that are missing. Keeping their
*content* up to date remains Gitea's job.

| Task | Handled by | Frequency |
| --- | --- | --- |
| Discover **new** repositories, create mirrors | this script (cron) | daily |
| Pull new commits into **existing** mirrors | Gitea itself | `MIRROR_INTERVAL` |

### Requirements

- Gitea 1.20+ with an API token (scopes `write:repository,read:user`)
- Python 3.7+ — standard library only, no `pip install`, no `jq`
- `cron`

Deliberately dependency-free so it runs on a small box: on a Raspberry Pi 3B
with 1 GB RAM the sync is invisible in `free -h`.

### Installation

```bash
git clone <this-repo> gitea-github-mirror
cd gitea-github-mirror
./install.sh
```

`install.sh` copies the scripts to `~/gitea-github-mirror`, creates `mirror.env`
from the example, fixes permissions and adds the cron entry. It is safe to
re-run — an existing `mirror.env` is never overwritten.

```bash
TARGET=/opt/gitea-mirror ./install.sh   # different directory
CRON_TIME="0 3" ./install.sh            # different schedule
```

Create the Gitea token on the Gitea host:

```bash
docker exec -u git <container> gitea admin user generate-access-token \
  --username <user> --token-name github-mirror-sync \
  --scopes write:repository,read:user
```

Then fill in `mirror.env` and verify:

```bash
cd ~/gitea-github-mirror
DRY_RUN=true python3 sync_mirrors.py   # preview, changes nothing
python3 sync_mirrors.py                # run for real
```

### Configuration (`mirror.env`)

| Key | Meaning |
| --- | --- |
| `GITHUB_USER` | GitHub account to mirror |
| `GITHUB_TOKEN` | See below. Empty = public repositories only |
| `GITEA_URL` | Gitea base URL as seen from this machine |
| `GITEA_TOKEN` | Gitea API token |
| `GITEA_OWNER` | Gitea user the mirrors belong to |
| `MIRROR_INTERVAL` | Go duration, e.g. `24h`, `8h`, `30m` |
| `INCLUDE_FORKS` | Mirror forks too (default `false`) |
| `INCLUDE_ARCHIVED` | Mirror archived repositories (default `true`) |
| `DRY_RUN` | `true` = show what would happen, change nothing |

Every key can be overridden by an environment variable of the same name, which
is what `DRY_RUN=true python3 sync_mirrors.py` uses.

### GitHub token

Needed only for private repositories. Use a **fine-grained** token
(<https://github.com/settings/personal-access-tokens>):

- Repository access: **All repositories** — required, otherwise repositories
  created later are invisible to the script and auto-discovery is pointless.
- Permissions: **Contents: Read-only**. Metadata read is added automatically,
  everything else stays on "No access".
- Expiration: **No expiration** is the sensible choice for an unattended cron
  job — an expiring token stalls the mirror without anyone noticing. The token
  can only read, so an unlimited lifetime costs little here.

A classic token with scope `repo` works too, but grants full write and delete
access to every repository — far more than a read-only mirror needs, for a
token that sits in plaintext on the server.

If you do set an expiry (or your organization enforces one), each run logs the
remaining validity, warns from 14 days out and exits non-zero once expired:

```
[INFO] GITHUB_TOKEN valid until 2026-12-31 (145 days)
[WARN] GITHUB_TOKEN expires in 7 day(s), on 2026-08-15 - renew it
[ERROR] GITHUB_TOKEN expired on 2026-08-01 - renew it
```

### Scope and limits

- Mirrored is **git data only**: all branches and tags. Issues, pull requests,
  releases and the wiki are excluded on purpose — a pull mirror never syncs
  those, so importing them once would leave a snapshot that silently goes
  stale. To change that, flip the flags in `create_mirror()`.
- The script **never deletes** anything in Gitea. If a repository disappears or
  is renamed on GitHub, its mirror stays and is reported as an orphan in the
  log — for a backup that is the right direction.
- A Gitea repository that exists but is **not** a mirror is left untouched, so
  a real working repository can never be overwritten.
- Mirrors are read-only in Gitea. Push to GitHub, not to the mirror.

### Files

| File | Purpose |
| --- | --- |
| `sync_mirrors.py` | Creates missing mirrors, aligns the mirror interval |
| `run_sync.sh` | Cron wrapper: appends to `sync.log`, trims it to 1000 lines |
| `install.sh` | Installs scripts, config and cron entry |
| `mirror.env.example` | Configuration template |
| `mirror.env` | Real config with secrets — git-ignored, `chmod 600` |
| `sync.log` | Last ~2000 log lines |

### Troubleshooting

```bash
tail -f ~/gitea-github-mirror/sync.log
```

| Log line | Cause |
| --- | --- |
| `GitHub API 401` | Token invalid or expired |
| `GitHub API 403` | Rate limit, or token lacks repository access |
| `migrate failed (HTTP 409)` | Name already taken in Gitea |
| `... exists in Gitea but is not a mirror` | Real repo with that name — rename one of them |
| `Missing config value(s)` | `mirror.env` not filled in |
| `no private repositories` despite a token | Token's repository access is "Public repositories", or no repository permission is set at all |

**Fine-grained token shows no private repositories.** Repository access and
permissions are two separate settings and both are needed: access decides
*which* repositories the token covers, permissions decide *what* it may do with
them. With "All repositories" but an empty permission list the token can see
nothing, and private repositories do not even appear in the listing. Set
**Contents: Read-only**; Metadata read is added automatically.

**After rotating the GitHub token**, updating `mirror.env` is not enough. Gitea
stores the credentials inside each private mirror's remote URL when the mirror
is created, so existing private mirrors keep using the old token and start
failing silently. Either update *Settings → Mirror Settings* per repository in
Gitea, or delete the private mirrors and let the next run recreate them. Public
mirrors are unaffected — they clone without credentials.

---

## Deutsch

Spiegelt **alle** GitHub-Repositories eines Benutzers als Pull-Mirror in eine
selbst gehostete Gitea-Instanz — auch Repositories, die erst später entstehen.

Ein einzelnes Repository kann Gitea bereits über die Weboberfläche spiegeln.
Was Gitea nicht kann: einen Account beobachten und neue Repositories von selbst
aufnehmen. Genau dafür gibt es dieses Skript — ein täglicher Cron-Job fragt die
GitHub-API, welche Repositories existieren, und legt die fehlenden Mirrors an.
Deren *Inhalt* aktuell zu halten bleibt Giteas Aufgabe.

| Aufgabe | Erledigt von | Häufigkeit |
| --- | --- | --- |
| **Neue** Repositories finden, Mirror anlegen | dieses Skript (Cron) | täglich |
| Commits in **bestehende** Mirrors holen | Gitea selbst | `MIRROR_INTERVAL` |

### Voraussetzungen

- Gitea 1.20+ mit API-Token (Scopes `write:repository,read:user`)
- Python 3.7+ — nur Standardbibliothek, kein `pip install`, kein `jq`
- `cron`

Bewusst ohne Abhängigkeiten, damit es auf kleiner Hardware läuft: Auf einem
Raspberry Pi 3B mit 1 GB RAM ist der Sync in `free -h` nicht zu sehen.

### Installation

```bash
git clone <dieses-repo> gitea-github-mirror
cd gitea-github-mirror
./install.sh
```

`install.sh` kopiert die Skripte nach `~/gitea-github-mirror`, legt `mirror.env`
aus der Vorlage an, setzt die Rechte und trägt den Cron-Job ein. Mehrfaches
Ausführen ist unbedenklich — eine vorhandene `mirror.env` wird nie überschrieben.

```bash
TARGET=/opt/gitea-mirror ./install.sh   # anderes Verzeichnis
CRON_TIME="0 3" ./install.sh            # andere Uhrzeit
```

Den Gitea-Token auf dem Gitea-Host erzeugen:

```bash
docker exec -u git <container> gitea admin user generate-access-token \
  --username <user> --token-name github-mirror-sync \
  --scopes write:repository,read:user
```

Danach `mirror.env` ausfüllen und prüfen:

```bash
cd ~/gitea-github-mirror
DRY_RUN=true python3 sync_mirrors.py   # Vorschau, ändert nichts
python3 sync_mirrors.py                # echter Lauf
```

### Konfiguration (`mirror.env`)

| Schlüssel | Bedeutung |
| --- | --- |
| `GITHUB_USER` | Zu spiegelnder GitHub-Account |
| `GITHUB_TOKEN` | Siehe unten. Leer = nur öffentliche Repositories |
| `GITEA_URL` | Gitea-Basis-URL, von dieser Maschine aus gesehen |
| `GITEA_TOKEN` | Gitea-API-Token |
| `GITEA_OWNER` | Gitea-Benutzer, dem die Mirrors gehören |
| `MIRROR_INTERVAL` | Go-Duration, z. B. `24h`, `8h`, `30m` |
| `INCLUDE_FORKS` | Forks mitspiegeln (Standard `false`) |
| `INCLUDE_ARCHIVED` | Archivierte Repositories spiegeln (Standard `true`) |
| `DRY_RUN` | `true` = nur anzeigen, nichts ändern |

Jeder Schlüssel lässt sich durch eine gleichnamige Umgebungsvariable
überschreiben — genau das nutzt `DRY_RUN=true python3 sync_mirrors.py`.

### GitHub-Token

Nur für private Repositories nötig. Empfohlen ist ein **fine-grained** Token
(<https://github.com/settings/personal-access-tokens>):

- Repository access: **All repositories** — zwingend, sonst sind später
  angelegte Repositories für das Skript unsichtbar und die automatische
  Erkennung wäre sinnlos.
- Permissions: **Contents: Read-only**. Metadata-Lesen kommt automatisch dazu,
  alles andere bleibt auf „No access".
- Expiration: **No expiration** ist für einen unbeaufsichtigten Cron-Job die
  sinnvolle Wahl — ein ablaufender Token legt den Spiegel still, ohne dass es
  jemand merkt. Der Token kann ohnehin nur lesen, unbegrenzte Laufzeit kostet
  hier also wenig.

Ein Classic-Token mit Scope `repo` funktioniert ebenfalls, gibt aber Vollzugriff
zum Schreiben und Löschen auf jedes Repository — deutlich mehr, als ein
Lese-Spiegel braucht, für einen Token, der im Klartext auf dem Server liegt.

Wird doch ein Ablaufdatum gesetzt (oder von der Organisation erzwungen),
protokolliert jeder Lauf die Restlaufzeit, warnt ab 14 Tagen und endet nach
Ablauf mit Exit-Code 1:

```
[INFO] GITHUB_TOKEN valid until 2026-12-31 (145 days)
[WARN] GITHUB_TOKEN expires in 7 day(s), on 2026-08-15 - renew it
[ERROR] GITHUB_TOKEN expired on 2026-08-01 - renew it
```

### Umfang und Grenzen

- Gespiegelt werden **nur Git-Daten**: alle Branches und Tags. Issues, Pull
  Requests, Releases und Wiki bleiben bewusst außen vor — ein Pull-Mirror
  synchronisiert das nie, ein einmaliger Import würde also stillschweigend
  veralten. Umstellbar über die Flags in `create_mirror()`.
- Das Skript **löscht nie** etwas in Gitea. Verschwindet ein Repository auf
  GitHub oder wird umbenannt, bleibt der Mirror bestehen und wird im Log als
  verwaist gemeldet — für ein Backup ist das die richtige Richtung.
- Ein Gitea-Repository, das existiert, aber **kein** Mirror ist, wird nicht
  angefasst; ein echtes Arbeits-Repository kann so nie überschrieben werden.
- Mirrors sind in Gitea schreibgeschützt. Gepusht wird nach GitHub, nicht in
  den Spiegel.

### Dateien

| Datei | Zweck |
| --- | --- |
| `sync_mirrors.py` | Legt fehlende Mirrors an, gleicht das Mirror-Intervall ab |
| `run_sync.sh` | Cron-Wrapper: schreibt nach `sync.log`, kürzt es auf 1000 Zeilen |
| `install.sh` | Installiert Skripte, Konfiguration und Cron-Eintrag |
| `mirror.env.example` | Konfigurationsvorlage |
| `mirror.env` | Echte Konfiguration mit Secrets — git-ignoriert, `chmod 600` |
| `sync.log` | Die letzten ~2000 Log-Zeilen |

### Fehlersuche

```bash
tail -f ~/gitea-github-mirror/sync.log
```

| Log-Zeile | Ursache |
| --- | --- |
| `GitHub API 401` | Token ungültig oder abgelaufen |
| `GitHub API 403` | Rate Limit, oder Token hat keinen Repository-Zugriff |
| `migrate failed (HTTP 409)` | Name in Gitea bereits vergeben |
| `... exists in Gitea but is not a mirror` | Echtes Repo gleichen Namens — eines umbenennen |
| `Missing config value(s)` | `mirror.env` nicht ausgefüllt |
| `no private repositories` trotz Token | Repository access steht auf „Public repositories", oder es ist gar keine Repository-Permission gesetzt |

**Fine-grained Token zeigt keine privaten Repos.** Repository access und
Permissions sind zwei getrennte Einstellungen, beide werden gebraucht: Der
Access bestimmt, *welche* Repositories der Token betrifft, die Permissions,
*was* er damit darf. Mit „All repositories", aber leerer Permission-Liste sieht
der Token nichts — private Repositories erscheinen dann nicht einmal in der
Auflistung. **Contents: Read-only** setzen, Metadata-Lesen kommt automatisch
dazu.

**Nach dem Wechsel des GitHub-Tokens** genügt es nicht, `mirror.env`
anzupassen. Gitea hinterlegt die Zugangsdaten beim Anlegen eines privaten
Mirrors fest in dessen Remote-URL — bestehende private Mirrors arbeiten also
weiter mit dem alten Token und scheitern still. Entweder pro Repository unter
*Einstellungen → Mirror-Einstellungen* in Gitea nachziehen, oder die privaten
Mirrors löschen und vom nächsten Lauf neu anlegen lassen. Öffentliche Mirrors
sind nicht betroffen, sie klonen ohne Zugangsdaten.
