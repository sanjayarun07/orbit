# Staging on Google Cloud, step by step

One Compute Engine VM running the repository's Compose stack unchanged:
the API image from GHCR, Postgres (pgvector), Redis and Caddy for TLS. This
is the smallest change from the runbook in [operations.md](product/operations.md)
("Staging"), and everything there still applies. Cloud Run is the later
path, not this one: the API runs background workers (holder ledger, task
scheduler, reconcilers) that need an always-on instance, Caddy would be
replaced by a load balancer, and Postgres and Redis would move to Cloud SQL
and Memorystore with a VPC connector. Start on the VM; move when there is a
reason.

Everything below runs from your laptop unless it says "on the VM". Replace
`<...>` values. Nothing here prints a secret; never paste one into a chat.

## 0. Before you start

- A Google Cloud account with billing enabled, and the `gcloud` CLI
  installed and signed in (`gcloud auth login`).
- A domain you control, with access to its DNS records.
- CI on `main` green and the image tag you will deploy known:
  `gh run list --limit 1` shows both jobs passed, and the tag is
  `sha-<7-char commit>` (for example `sha-1907b05`). The package is public;
  no registry login is needed.
- Your provider keys at hand (OpenAI, Mobula, Resend, and the rest from
  `deploy/staging.env.example`). Use separate keys from production where the
  provider offers a second one.

Cost, for the sizing below: about $60 a month for the VM and disk, plus
egress. Stop the VM when not testing (`gcloud compute instances stop`).

## 1. Project and APIs

```bash
gcloud projects create <project-id> --name="Dopamint staging"
gcloud config set project <project-id>
gcloud billing projects link <project-id> --billing-account=<billing-account-id>   # gcloud billing accounts list
gcloud services enable compute.googleapis.com
gcloud config set compute/region us-central1
gcloud config set compute/zone us-central1-a
```

Pick the region nearest your testers; the provider APIs are worldwide.

## 2. Static address and firewall

The address must exist before the DNS record, and the record before Caddy
starts, or the certificate request fails and retries with backoff.

```bash
gcloud compute addresses create orbit-staging-ip --region=us-central1
gcloud compute addresses describe orbit-staging-ip --region=us-central1 --format='value(address)'
```

The default network already has rules named `default-allow-http` and
`default-allow-https` that apply to instances tagged `http-server` and
`https-server`; step 4 tags the VM. If your project uses a custom network,
create the rules:

```bash
gcloud compute firewall-rules create orbit-allow-web --network=<network> --allow=tcp:80,tcp:443 --target-tags=http-server,https-server
```

SSH goes through `gcloud compute ssh` (IAP or the default SSH rule); do not
open port 22 to the world.

## 3. DNS

At your DNS provider, create an `A` record for the staging hostname
pointing at the address from step 2, TTL 300:

```
staging.<yourdomain>   A   <address>
```

Wait until it resolves from your laptop: `dig +short staging.<yourdomain>`.

## 4. The VM

Ubuntu 24.04 LTS, 2 vCPU, 8 GB (the API container is capped at 2 GB and
runs two workers; Postgres and Redis take the rest), a 50 GB persistent
disk, the static address, and the web tags.

```bash
gcloud compute instances create orbit-staging \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB --boot-disk-type=pd-balanced \
  --address=orbit-staging-ip \
  --tags=http-server,https-server \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
gcloud compute ssh orbit-staging
```

## 5. On the VM: Docker

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
docker compose version      # 2.24 or later: the production overlay uses !reset
```

## 6. On the VM: the checkout and the env file

The checkout is for the compose files, the Caddyfile and the scripts; the
application itself comes from the image.

```bash
git clone https://github.com/sanjayarun07/orbit.git ~/orbit && cd ~/orbit
git checkout <commit>                        # the commit CI built; the image tag is sha-<its first 7 chars>
cp deploy/staging.env.example .env
chmod 600 .env
nano .env                                    # fill every <...>
```

In `.env`: `ORBIT_DOMAIN=staging.<yourdomain>`, `PUBLIC_BASE_URL=https://staging.<yourdomain>`,
`ORBIT_IMAGE_TAG=sha-<7 chars>`, and generate each secret on the VM with
`openssl rand -hex 32` (`POSTGRES_PASSWORD`, `ADMIN_API_KEY`, `MCP_API_KEY`).
Leave `DEPLOYMENT_MODE=research` and `LIVE_TRADING=false` for the first
deploy. `SOLANA_PRIVATE_KEY` stays absent.

Run the preflight from the image, so nothing else is installed on the VM.
It runs the same audit the app runs at startup on the file you just wrote,
names every provider that is or is not configured, and never prints a value:

```bash
docker run --rm -v "$PWD/.env:/env:ro" ghcr.io/sanjayarun07/orbit:sha-<7 chars> python scripts/preflight.py /env
```

`startup audit: boots` with no fatal lines is the pass. Fix anything
fatal in `.env` and run it again.

## 7. On the VM: up

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f caddy   # until "certificate obtained"
```

Then, from your laptop:

```bash
curl -s https://staging.<yourdomain>/readyz | jq '{status, failed, degraded, config_warnings}'
```

`status: ready`, `failed: []`. `degraded` may list `knowledge_snapshot`
until step 8 and `polymarket` if the VM's network cannot reach it. If the
API keeps restarting, `docker compose ... logs api` names every fatal
setting at once.

## 8. On the VM: the knowledge base, once

Tens of minutes and a few dollars of embedding calls; run it in its own
process, not the background worker:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python scripts/kb_ingest.py --parallel 4 -v
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python scripts/kb_ingest.py --derive
```

`knowledge_snapshot` reports its entity count in `/readyz` within two minutes.

## 9. Verify as a user

1. Open `https://staging.<yourdomain>/ui/`, sign in with your email; the
   magic link arrives from the staging sender; the plan reads Beta.
2. Connect a wallet and ask for its portfolio; ask "top holders of BONK on
   solana"; ask for a deep dive on a meme token.
3. Read the turn log: the admin page's Turns panel (`/ui/admin`, the admin
   key from `.env`, typed into a browser you alone use), or on the VM
   `docker compose ... exec api python scripts/turn_review.py`.
4. Only after that, rehearse swaps: set `DEPLOYMENT_MODE=execution` and
   `LIVE_TRADING=true` together in `.env`, keep `MAX_TRADE_USD=5`,
   `docker compose ... up -d`, and confirm one small Jupiter swap and one
   Relay swap signed in your own wallet.

## 10. Backups and monitoring

Disk snapshots, daily, kept two weeks:

```bash
gcloud compute resource-policies create snapshot-schedule orbit-daily --region=us-central1 \
  --max-retention-days=14 --on-source-disk-delete=keep-auto-snapshots \
  --daily-schedule --start-time=03:00
gcloud compute disks add-resource-policies orbit-staging --resource-policies=orbit-daily --zone=us-central1-a
```

A logical Postgres dump as well, because a snapshot restores the whole VM
and a dump restores one table. On the VM, nightly by cron:

```bash
mkdir -p ~/backups && (crontab -l 2>/dev/null; echo '15 3 * * * cd ~/orbit && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres pg_dump -U orbit orbit | gzip > ~/backups/orbit-$(date +\%F).sql.gz && find ~/backups -mtime +14 -delete') | crontab -
```

An uptime check on readiness, alerting to your email:

```bash
gcloud services enable monitoring.googleapis.com
gcloud monitoring uptime create orbit-staging-readyz --resource-type=uptime-url \
  --resource-labels=host=staging.<yourdomain>,project_id=<project-id> --protocol=https --path=/readyz --port=443 --period=5
```

Watch the VM's memory in the console the first day; if the API container
is killed at its 2 GB cap, drop `UVICORN_WORKERS` to 1 in `.env`.

## 11. Upgrade, roll back, stop

On the VM, every change is a new pinned tag:

```bash
cd ~/orbit && git fetch && git checkout <new commit>
sed -i 's/^ORBIT_IMAGE_TAG=.*/ORBIT_IMAGE_TAG=sha-<new 7 chars>/' .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Roll back with the previous tag and the same two commands; schema
statements are additive, so an older image starts against a newer schema.
To stop paying while idle: `gcloud compute instances stop orbit-staging`;
`start` brings everything back, the static address included.

## 12. Promote

The tag that passed on staging, with `deploy/beta.env.example` and its own
secrets on the production host, following the same steps. Never a tag
staging did not run.

## Shape A: managed Postgres and Redis (Cloud SQL and Memorystore)

The same VM, the same API container and Caddy, with the two stores moved
to Google's managed services. No application change: the app creates its
own schema at startup, including `CREATE EXTENSION IF NOT EXISTS vector`,
and needs a Redis with eviction off. The compose file for this shape is
`docker-compose.managed.yml`, used **alone** (the base file insists on a
password for the Postgres it would run itself). Steps 1 to 5 above are
unchanged; this replaces steps 6 and 7 and adds the stores first. About
$120 to $150 a month all in.

### A1. Private connectivity, once

Both stores get private addresses in the `default` network; the VM reaches
them because it is in that network, and nothing else can.

```bash
gcloud services enable sqladmin.googleapis.com redis.googleapis.com servicenetworking.googleapis.com
gcloud compute addresses create google-managed-services-default --global --purpose=VPC_PEERING --prefix-length=16 --network=default
gcloud services vpc-peerings connect --service=servicenetworking.googleapis.com --ranges=google-managed-services-default --network=default
```

### A2. Cloud SQL for PostgreSQL 16

Smallest dedicated shape, private IP only, daily backups with point-in-time
recovery. The `vector` extension is available on Cloud SQL and the user you
create below may install it, which the app does on first start.

```bash
gcloud sql instances create orbit-pg --database-version=POSTGRES_16 --tier=db-custom-1-3840 \
  --region=us-central1 --network=projects/<project-id>/global/networks/default --no-assign-ip \
  --storage-size=20GB --storage-auto-increase --backup-start-time=03:00 --enable-point-in-time-recovery \
  --deletion-protection
gcloud sql databases create orbit --instance=orbit-pg
gcloud sql users create orbit --instance=orbit-pg --password=<generate with: openssl rand -hex 24>
gcloud sql instances describe orbit-pg --format='value(ipAddresses[0].ipAddress)'     # the private address
```

(The password passes through your shell history with `--password`; run
`history -d` on that line, or set it afterwards with
`gcloud sql users set-password orbit --instance=orbit-pg --prompt-for-password`.)

### A3. Memorystore for Redis 7

One gigabyte is generous for sessions, turn locks, retained history and the
counters. Eviction off is required, not a preference: a held turn lock that
gets evicted lets a second caller into the same conversation. Snapshots
every hour are the persistence Memorystore offers (the local stack uses an
append-only log); a restart loses at most an hour of retained history.

```bash
gcloud redis instances create orbit-redis --region=us-central1 --tier=basic --size=1 \
  --redis-version=redis_7_0 --network=projects/<project-id>/global/networks/default \
  --redis-config maxmemory-policy=noeviction --persistence-mode=rdb --rdb-snapshot-period=1h
gcloud redis instances describe orbit-redis --region=us-central1 --format='value(host,port)'
```

Use `--tier=standard` for a replicated instance with automatic failover
when the beta grows past a handful of people.

### A4. On the VM: the env file and up

```bash
cd ~/orbit && cp deploy/staging.env.example .env && chmod 600 .env && nano .env
```

Leave `POSTGRES_PASSWORD` blank and set, from A2 and A3:

```
DATABASE_URL=postgresql://orbit:<password>@<cloud-sql-private-ip>:5432/orbit?sslmode=require
REDIS_URL=redis://<memorystore-host>:6379/0
```

Then the preflight from the image, and up with the managed file alone:

```bash
docker run --rm -v "$PWD/.env:/env:ro" ghcr.io/sanjayarun07/orbit:sha-<7 chars> python scripts/preflight.py /env
docker compose -f docker-compose.managed.yml pull
docker compose -f docker-compose.managed.yml up -d
docker compose -f docker-compose.managed.yml logs -f caddy      # until "certificate obtained"
curl -s https://staging.<yourdomain>/readyz | jq '{status, failed, degraded}'
```

`postgres` and `redis` in the readiness checks now describe the managed
instances. Steps 8 to 12 are unchanged, with `-f docker-compose.managed.yml`
in place of the two-file form; the nightly `pg_dump` in step 10 is replaced
by Cloud SQL's own backups (`gcloud sql backups list --instance=orbit-pg`),
and the disk snapshot then only protects the VM itself.

### What to check once, after the first start

- `docker compose -f docker-compose.managed.yml logs api | grep -i vector`
  should not say the extension was unavailable; if it does, connect once
  as the `orbit` user and run `CREATE EXTENSION vector;` by hand.
- `gcloud redis instances describe orbit-redis --region=us-central1 --format='value(redisConfigs)'`
  shows `maxmemory-policy=noeviction`.

## What is deliberately not here

- No Secret Manager: for one VM, a `chmod 600 .env` on a disk only you can
  reach is the simpler control. Move secrets to Secret Manager when a second
  person or a second machine needs them.
- No Cloud Run: the API's background workers, its two local state files and
  its proxy trust all need work before it can run there (the assessment of
  2026-09-22 lists them). Managed stores alone are Shape A above.
- No custom service account or Workload Identity: the VM makes no Google
  API calls.
