# InSlide
A label corrector

## Pipeline API

The versioned pipeline API is available under `/api/v1` and requires HTTPS plus a
scoped personal access token. Create a token for an existing InSlide user:

```bash
flask --app src/app.py api-token create USERNAME --label "integration name"
```

The plaintext token is shown once. It expires after 90 days by default. Use
`api-token list`, `api-token rotate TOKEN_ID`, and `api-token revoke TOKEN_ID` to
manage credentials.

Submit a job using server-visible input and output paths:

```bash
curl --request POST https://inslide.example/api/v1/pipeline/jobs \
  --header "Authorization: Bearer $INSLIDE_TOKEN" \
  --header "Content-Type: application/json" \
  --header "Idempotency-Key: unique-client-request-id" \
  --data '{"input_dir":"/data/incoming","output_dir":"/data/output"}'
```

Inspect the returned job URL and its `/output` subresource to monitor execution.
The authenticated OpenAPI 3.1 contract is served at `/api/v1/openapi.json` and is
also checked in as `src/openapi.json`.

API traffic is limited per token to five submissions and 60 reads per minute.
When TLS terminates at one trusted reverse proxy, set
`API_TRUST_PROXY_HEADERS=true`; otherwise forwarded scheme headers are ignored.
The supplied Compose deployment runs Caddy as that proxy and publishes only
HTTPS port 443. Caddy sets the forwarding headers, while application port 5000
remains private to the Compose network. Network clients therefore cannot bypass
the proxy over plaintext HTTP.

Pipeline paths are restricted after path translation and symbolic-link
resolution. `PIPELINE_INPUT_ROOTS` and `PIPELINE_OUTPUT_ROOTS` contain
platform-path-separator-delimited allowlists. Compose permits input beneath
`/data/gt450-images` or `/data/inslide-batches` and output beneath
`/data/inslide-batches`. Worker fields default to a maximum of 8 and
thumbnail dimensions to 4096 pixels; deployments can lower these limits with
`PIPELINE_MAX_WORKERS` and `PIPELINE_MAX_THUMBNAIL_DIMENSION`.

## Linux container on Docker Desktop

The `linux/amd64` image runs as a Linux container under Docker Desktop. It contains the Flask
application, Python pipeline, utilities, and a native Linux `tq` release binary.
Rust and Python compile in separate stages; the final image contains neither
Cargo nor GCC.

### Prerequisites

- Docker Desktop configured for Linux containers.
- Repository cloned with its pinned TQ submodule:

  ```powershell
  git clone --recurse-submodules <inslide-repository-url>
  ```

- Windows directories shared with Docker Desktop.
- A dedicated, read-only SMB account that can access the GT450 image directory.
  Docker's Linux VM mounts this share directly; no Windows drive mapping is
  required.
- A dedicated TQ configuration directory containing `config.toml` and a
  dedicated SSH directory containing `id_ed25519` or `id_rsa` plus a
  `known_hosts` file with the approved SFTP server host key. TQ rejects unknown
  or changed server keys; it never learns them automatically.
- Python 3.10 or newer and Microsoft ODBC Driver 18 for SQL Server installed on
  the signed-in Windows workstation for the CoPath worker.
- A one-line CoPath ODBC connection string stored outside this repository. The
  Windows worker uses native integrated authentication; it contains no username
  or password.
- A stable machine hostname that coworkers' devices can resolve on the
  organization network.
- Inbound TCP port 443 permitted by the Windows firewall and organization
  network policy.

Copy `.env.example` to `.env`, replace all placeholders, and create the host
directories. `INSLIDE_STATE_HOST` must contain
`Slide_Digitization_Log.xlsx` before SDL workflows run.

Batch workflow stages, queues, and leases are stored in
`INSLIDE_STATE_HOST\instance\batch_catalog.sqlite3`. Batch list pages query
this catalog and load `enriched.csv` only after a batch is selected. A
background reconciliation scans for externally-created batches at startup and
every `BATCH_CATALOG_RECONCILE_SECONDS` seconds (60 by default).

Before the first catalog-aware deployment, stop the InSlide container and
validate legacy state from a PowerShell prompt:

```powershell
python src\migrate_batch_catalog.py `
  --batches-root D:\label_check_batches `
  --state-root D:\label_check_batches\state
```

Resolve every error and review unmatched-queue warnings, then apply migration:

```powershell
python src\migrate_batch_catalog.py `
  --batches-root D:\label_check_batches `
  --state-root D:\label_check_batches\state `
  --apply
```

Migration verifies new SQLite database, archives imported stage and queue CSVs
under `state\instance\legacy_batch_state_archive`, writes SHA-256 manifest, then
removes verified originals. Batches without `completed_stages.csv` are skipped
during migration and registered as new `False,False` batches by reconciliation.

`SECRET_KEY` must contain at least 32 characters and
`ADMIN_DEFAULT_PASSWORD` at least 12. Missing, weak, legacy-default, or example
placeholder values stop application initialization. Rotating `SECRET_KEY`
invalidates existing browser sessions.

Browser sessions use `Secure`, `HttpOnly`, and `SameSite=Lax` cookies. Compose
forces `SESSION_COOKIE_SECURE=true`. Set it to `false` only for local development
served directly over plain HTTP; never use that override for a network-facing
deployment. HSTS and the remaining browser security headers are emitted by the
application when the trusted proxy reports an HTTPS request.

Compose uses Caddy's internal certificate authority because the organization
does not supply the deployment certificate. Each authorized client must trust
the generated root certificate before using the app. Protect and back up the
`inslide-caddy-data` volume: it contains the local CA private key. Deleting
that volume creates a new CA and requires every client to trust the replacement.

New user passwords must contain 12–128 characters. Login failures are stored in
the application SQLite database and limited over a 15-minute window to five per
username/client-address pair and ten per username. Successful login clears both
counters. The limits and window are configurable through the corresponding
`LOGIN_*` variables in `.env.example`.

Application startup creates runtime directories with mode `0700`, files with
mode `0600`, and repairs existing instance state before loading it. Symbolic
links in sensitive instance state are rejected. On Windows bind mounts, NTFS
ACLs remain the security boundary: restrict `INSLIDE_STATE_HOST` to the
service account, Docker Desktop service account, and administrators.

User activity is accumulated in `/data/state/instance/statistics.sqlite3`.
The `statistics-scheduler` Compose service atomically refreshes each active
user's `lifetime_stats.csv` after the server-local date changes. Days without
a successful login or authenticated app activity are omitted. Keep web and
scheduler containers on the same local timezone; set `TZ` consistently when
the container default is not the desired zone.

Obtain the SFTP server's SHA-256 host-key fingerprint through a trusted
out-of-band channel. After verifying it, place the corresponding OpenSSH
`known_hosts` entry in `${SSH_HOME_HOST}\known_hosts`. `ssh-keyscan` output must
not be trusted until its fingerprint has been independently verified.

Only InSlide administrators can view or edit the global TQ connection
configuration. Authenticated operators can continue transferring approved
slides through that administrator-managed destination.

Administrators can open the configuration directly from **Transfers > Edit
Config**. The container reads `${TQ_HOME_HOST}\config.toml` through its
`/home/inslide/.tq/config.toml` bind mount. Write Windows paths as TOML literal
strings, for example `source = 'D:\image_staging'`. InSlide automatically
repairs unambiguous Windows-path escaping during saves and transfer preflight;
each repair retains a timestamped `config.toml.bak-*` copy.

The GT450 CIFS password is stored in the gitignored `.env` file and in local
Docker volume metadata. Restrict Docker access to trusted administrators and do
not reuse a personal account. Because CIFS mount options are comma-delimited,
the dedicated account password must not contain a comma. Quote other special
characters according to Compose `.env` syntax.

Example CoPath secret file content:

```text
DRIVER={ODBC Driver 18 for SQL Server};SERVER=sql-server.example.org;DATABASE=COPLIVE;Trusted_Connection=yes;TrustServerCertificate=yes;
```

Every ODBC property must be separated by a semicolon. The connection string
does not contain the Windows username or password.

### Build and test

```powershell
git submodule update --init --recursive
docker build --target test --tag inslide:test .
docker compose build
```

The image installs PyTorch from its CPU-only wheel index. BuildKit caches pip
downloads, Python dependency layers, and the EasyOCR models independently from
the application source, so normal source edits do not repeat the large OCR
setup. Keep BuildKit enabled and avoid `--no-cache` unless diagnosing a build.

TQ builds and tests natively for Linux:

```text
cargo test --locked --release
cargo build --locked --release
```

EasyOCR is CPU-only and its English models are baked into the image. Obtain the
Linux binary hash when needed with:

```powershell
docker run --rm --entrypoint sha256sum inslide:latest /app/bin/tq
```

### Paths

Inside the container, Windows resources appear at stable Linux paths:

- GT450 images: `/data/gt450-images`
- scanner inventories: `/data/scanner-inventories`
- Label-Check pipeline batches: `/data/inslide-batches`
- deidentified transfer staging: `/data/image-staging`
- CoPath clone: `/data/copath-clone`
- persistent application state: `/data/state`

Persisted UNC GT450 paths and `D:\label_check_batches` paths are translated to
these mounts. New pipeline output records Linux mount paths directly.

The GT450 mount is the named Docker volume `inslide-gt450-images`. It mounts
`//chp.clarian.org/app/Philips_Slide_Images/GT450_Images` through CIFS with
read-only permissions. The remaining paths are ordinary Windows bind mounts.

Create and share `IMAGE_STAGING_HOST` (normally `D:\image_staging`) with Docker
Desktop. Before each TQ upload, InSlide copies selected GT450 slides to
`<IMAGE_STAGING_HOST>\<destination directory>\<renamed slide>.svs`, removes the
identifying label and macro from those copies, then gives the staged slide paths
and a generated metadata CSV to TQ. Each transfer uploads that CSV as
`<destination directory>/metadata.csv` before the slides. It contains
`accession_id,pid,num_slides`, with one row per accession
and the number of selected slides for that accession. A staging or
deidentification failure prevents the whole selection from uploading. Files
confirmed successful by TQ are removed; failed files remain for
diagnosis and are replaced from GT450 on retry. Size this directory for the
largest transfer selection and restrict it to the workstation operators and
Docker Desktop service account.

### Windows CoPath worker

CoPath queries are delegated to a worker that you start after signing into
Windows. Docker and the worker communicate only through
`INSLIDE_STATE_HOST\copath-query`; no SQL credentials enter the container.
The queue contains accessions and report data. Restrict the entire
`INSLIDE_STATE_HOST` directory to the signed-in Windows account, the Docker
Desktop service account, and administrators. Do not share it broadly or use a
world-writable network directory.

Renaming uses two CoPath query scopes. Exact-accession requests run first and
make the batch available for review. When an exact result identifies an MRN not
yet present in `/data/copath-clone/all_iuh_identifiers.csv`, a lower-priority
longitudinal request retrieves that patient's other accessions in the
background. Results remain in the batch as `pending_CoPath_history.csv` until
all batch PIDs are approved. Each batch records longitudinal outcomes in
`copath_longitudinal_jobs.csv` with `AccessionID`, `MRN`, and `Error` columns;
errors are `NONE`, `RETRY`, or `ERROR: <details>`.

The clone identifier index has columns `AccessionID`, `Organ`, `MRN`, and
`PID`. PID identity is scoped to `(MRN, Organ)`. Legacy `all_accessions.csv`
data is migrated into this unified index using the organ CoPath files.

Create the worker environment once from the repository root in PowerShell:

```powershell
py -m venv .venv-copath-worker
.\.venv-copath-worker\Scripts\python.exe -m pip install --upgrade pip
.\.venv-copath-worker\Scripts\python.exe -m pip install --require-hashes -r requirements-windows-worker.txt
.\.venv-copath-worker\Scripts\python.exe -m pip check
```

Start the worker manually after Windows sign-in, before preparing or retrying a
batch:

```powershell
.\.venv-copath-worker\Scripts\python.exe src\copath_windows_worker.py `
  --queue "$env:INSLIDE_STATE_HOST\copath-query" `
  --connection-string-file "$env:COPATH_CONNECTION_STRING_FILE_HOST"
```

### Python dependency locks

The `.in` files contain direct dependencies. The corresponding `.txt` files are
generated locks with exact versions and SHA-256 hashes; edit only the inputs.
Regenerate Linux locks in a clean Python 3.12 environment:

```bash
python -m pip install uv==0.8.15
uv pip compile --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --torch-backend cpu --index https://download.pytorch.org/whl/cpu --generate-hashes --emit-index-url --output-file requirements.txt requirements.in
uv pip compile --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes --emit-index-url --output-file requirements-test.txt requirements-test.in
```

Regenerate the worker lock on Windows with Python 3.12:

```powershell
py -3.12 -m pip install uv==0.8.15
py -3.12 -m uv pip compile --python-version 3.12 --python-platform x86_64-pc-windows-msvc --generate-hashes --emit-index-url --output-file requirements-windows-worker.txt requirements-windows-worker.in
```

Review all version changes, commit inputs and generated locks together, then run
the unit tests and Docker test target. Install locks only with
`--require-hashes`; `pip check` must report no broken requirements.

If those values are stored only in `.env`, substitute their actual Windows
paths in the command. The worker writes a heartbeat every five seconds, accepts
nonblank, case-insensitively unique accession lists (up to 10,000), and uses the signed-in user's
native Windows identity through `Trusted_Connection=yes`. Press Ctrl+C for a
clean shutdown; the heartbeat is removed and the renaming page reports the
worker offline instead of waiting for the full query timeout.

To verify the SQL identity during integration testing, have a database
administrator inspect the worker's SQL Server session while a batch query is
active, or temporarily run the equivalent approved identity query through the
same ODBC connection. It should show the signed-in personal domain account.

### Run

```powershell
docker compose up -d
docker compose ps
```

Set `INSLIDE_HOSTNAME` in `.env` to the machine's existing network hostname.
After the first startup, export Caddy's root CA certificate to the current
user's Downloads directory:

```powershell
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt `
  "$env:USERPROFILE\Downloads\inslide-local-ca.crt"
```

On the host and each authorized coworker's Windows device, import
`inslide-local-ca.crt` into **Trusted Root Certification Authorities** for
the current user. Treat the certificate as trusted software and distribute it
through an authenticated channel. Do not distribute anything else from the
Caddy data volume, especially `root.key`.

```powershell
Import-Certificate `
  -FilePath "$env:USERPROFILE\Downloads\inslide-local-ca.crt" `
  -CertStoreLocation Cert:\CurrentUser\Root
```

Restart the browser after importing the certificate. If a browser uses its own
certificate store, import the same root certificate there as well.

Confirm that the hostname resolves to the Docker host and that HTTPS is
reachable, then open the app:

```powershell
$inSlideHostname = "replace-with-configured-hostname"
Resolve-DnsName $inSlideHostname
Test-NetConnection $inSlideHostname -Port 443
Start-Process "https://$inSlideHostname"
```

Use the exact hostname configured in `INSLIDE_HOSTNAME`; an IP address or a
different alias will fail certificate validation. Port 80 is intentionally not
published, so include `https://` in the URL. If name resolution or inbound port
443 is blocked, ask IT to add the hostname/firewall allowance. Application port
5000 should remain unreachable from the host and other network devices.

Verify that the SMB mount contains the expected scanner directories and is
read-only for the application user:

```powershell
docker compose exec inslide ls -la /data/gt450-images
docker inspect $(docker compose ps -q inslide) `
  --format '{{range .Mounts}}{{if eq .Destination "/data/gt450-images"}}{{println "RW:" .RW "Name:" .Name}}{{end}}{{end}}'
```

The inspection output must report `RW: false` and
`Name: inslide-gt450-images`.

Docker volume options are fixed when the volume is created. After changing the
SMB password or any `GT450_SMB_*` mount setting, recreate only this mount:

```powershell
docker compose down
docker volume rm inslide-gt450-images
docker compose up -d
```

Removing this Docker volume unmounts the share; it does not delete files from
the SMB server.

The default command initializes persistent state and starts Waitress on port
5000. Other applications use the same image:

```powershell
docker compose run --rm inslide pipeline `
  --input-dir /data/gt450-images/SS12797 `
  --output-dir /data/inslide-batches/SS12797/2026-07-31 `
  --end-at name --ocr-use-cpu

docker compose run --rm inslide nightly
docker compose run --rm inslide python /app/src/deidentify_anonymize.py --help
```

Schedule `nightly` externally; it performs one cycle and exits. State, SDL,
backups, TQ configuration, and transfer logs survive container replacement.

### GT450 troubleshooting

- **GT450 volume fails to mount:** verify `GT450_SMB_SERVER`, the dedicated
  account credentials and domain, SMB 3.0 connectivity, and access to the
  `app` share. Docker should fail container startup on a mount error instead of
  substituting an empty local directory. After changing mount options, remove
  and recreate `inslide-gt450-images` as described above.

### CoPath troubleshooting

- **Worker offline:** confirm the worker console is still running and that both
  Windows and Docker can access `INSLIDE_STATE_HOST\copath-query`. Check the
  Windows clock if `worker.json` is present but considered stale.
- **Query timeout:** the default is 300 seconds. Inspect the worker console and
  SQL connectivity before increasing `COPATH_QUERY_TIMEOUT_SECONDS`.
- **ODBC or login failure:** confirm ODBC Driver 18 is installed, the connection
  file has semicolon-delimited properties including `Trusted_Connection=yes`,
  and the signed-in Windows account is authorized for CoPath.
- **Queue permission failure:** restore ACL access for the signed-in user and
  Docker Desktop. Keep `requests`, `processing`, `results`, `errors`, `work`,
  and `worker.json` beneath the configured queue root.
- **Crash recovery:** restart the worker. It returns claims older than ten
  minutes to the request queue and removes terminal artifacts after 24 hours.

### Optional direct Linux/Kerberos mode

The previous in-container query path remains available as a fallback. Set
`KRB5_CONFIG_HOST` and `COPATH_CONNECTION_STRING_FILE_HOST`, then merge the
direct-mode override:

```powershell
docker compose -f compose.yaml -f compose.direct.yaml up -d
docker compose -f compose.yaml -f compose.direct.yaml exec inslide `
  kinit YOUR_USERNAME@YOUR.AD.REALM
docker compose -f compose.yaml -f compose.direct.yaml exec inslide klist
```

In direct mode, use the SQL Server DNS name associated with its `MSSQLSvc`
service principal name. Renew `kinit` after ticket expiry or container
recreation. The base `docker compose up -d` deployment always uses the Windows
queue.
