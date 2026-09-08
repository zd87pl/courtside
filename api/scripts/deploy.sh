#!/usr/bin/env bash
#
# Automated, idempotent Fly.io deployment for the Courtside mobile API.
#
# Safe to re-run: every step checks whether the resource already exists
# before creating it, and admin/webhook credentials (ADMIN_TOKEN,
# WEBHOOK_SECRET) are only generated
# once and never overwritten by a later run.
#
# Full documentation: api/DEPLOYMENT.md
#
# Usage:
#   ./api/scripts/deploy.sh [flags]
#
# Run from anywhere; the script locates the repo root itself.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (all overridable via flags or environment variables)
# ---------------------------------------------------------------------------
APP_NAME="${APP_NAME:-courtside-api}"
REGION="${FLY_REGION:-iad}"
ORG="${FLY_ORG:-}"
BUCKET_NAME="${BUCKET_NAME:-}"
PROVISION_POSTGRES=0
PG_VM_SIZE="${PG_VM_SIZE:-shared-cpu-1x}"
PG_VOLUME_SIZE_GB="${PG_VOLUME_SIZE_GB:-10}"
WORK_VOLUME_SIZE_GB="${WORK_VOLUME_SIZE_GB:-100}"
WORK_VOLUME_NAME="courtside_work"
NON_INTERACTIVE=0
DRY_RUN=0
SKIP_FLYCTL_INSTALL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FLY_TOML_SRC="$REPO_ROOT/api/fly.toml"

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BLUE=$'\033[34m'
else
  C_RESET=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
fi
log_step()  { printf '\n%s==>%s %s\n' "$C_BOLD$C_BLUE" "$C_RESET" "$*"; }
log_info()  { printf '%s\n' "$*"; }
log_ok()    { printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
log_warn()  { printf '%s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
log_err()   { printf '%s✗ %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; }
die()       { log_err "$*"; exit 1; }

run() {
  # Prints and, unless --dry-run, executes a command. Use for every
  # mutating flyctl call so --dry-run gives a trustworthy preview.
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '  %s[dry-run]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"
  else
    printf '  %s$%s %s\n' "$C_BOLD" "$C_RESET" "$*"
    "$@"
  fi
}

# Provider provisioning may print generated passwords/access keys. Retain that
# output privately; never send it to CI logs.
run_private() {
  [[ ! -L "$CREDENTIALS_FILE" ]] || die "Refusing a symlink credentials file."
  (umask 077; touch "$CREDENTIALS_FILE")
  chmod 600 "$CREDENTIALS_FILE"
  if ! "$@" >> "$CREDENTIALS_FILE" 2>&1; then
    die "Provisioning failed; inspect private output in $CREDENTIALS_FILE."
  fi
  log_info "Provider output saved privately in $CREDENTIALS_FILE"
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Automated Fly.io deployment for the Courtside API.

Usage: $(basename "$0") [flags]

Flags:
  --app-name NAME       Fly app name (default: courtside-api; env APP_NAME)
  --region CODE         Fly region for app + Postgres + volume (default: iad; env FLY_REGION)
  --org SLUG            Fly org slug (default: auto-detected if you belong to exactly one; env FLY_ORG)
  --bucket-name NAME    Tigris bucket name (default: <app-name>-media; env BUCKET_NAME)
  --provision-postgres  Create an UNMANAGED single-node Fly Postgres if DATABASE_URL is absent
  --yes                 Non-interactive: never prompt, fail instead if a required
                         secret (FLY_API_TOKEN / OPENROUTER_API_KEY) is missing
  --dry-run             Print every command that would run without executing it
  --skip-flyctl-install Fail instead of auto-installing flyctl if it's missing
  -h, --help            Show this help

Required environment variables (see api/DEPLOYMENT.md for how to set these safely):
  FLY_API_TOKEN         Fly.io API token
  OPENROUTER_API_KEY    OpenRouter API key (the worker refuses to start without one)

Examples:
  ./api/scripts/deploy.sh
  ./api/scripts/deploy.sh --app-name my-courtside --region ord --yes
  ./api/scripts/deploy.sh --dry-run
EOF
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-name) [[ $# -ge 2 ]] || die "--app-name requires a value"; APP_NAME="$2"; shift 2 ;;
    --region) [[ $# -ge 2 ]] || die "--region requires a value"; REGION="$2"; shift 2 ;;
    --org) [[ $# -ge 2 ]] || die "--org requires a value"; ORG="$2"; shift 2 ;;
    --bucket-name) [[ $# -ge 2 ]] || die "--bucket-name requires a value"; BUCKET_NAME="$2"; shift 2 ;;
    --provision-postgres) PROVISION_POSTGRES=1; shift ;;
    --yes) NON_INTERACTIVE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-flyctl-install) SKIP_FLYCTL_INSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown flag: $1 (see --help)" ;;
  esac
done

[[ "$APP_NAME" =~ ^[a-z][a-z0-9-]{2,49}$ ]] || die "App name must be 3-50 lowercase letters, digits or hyphens, starting with a letter."
[[ "$REGION" =~ ^[a-z]{3}$ ]] || die "Region must be a three-letter Fly region code."
BUCKET_NAME="${BUCKET_NAME:-${APP_NAME}-media}"
[[ "$BUCKET_NAME" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || die "Invalid bucket name."
DB_APP_NAME="${APP_NAME}-db"
CREDENTIALS_FILE="$REPO_ROOT/fly-${APP_NAME}-credentials.txt"

# ---------------------------------------------------------------------------
# Step: ensure flyctl is installed
# ---------------------------------------------------------------------------
ensure_flyctl() {
  log_step "Checking for flyctl"
  if command -v fly >/dev/null 2>&1; then
    log_ok "flyctl already installed: $(fly version 2>/dev/null | head -n1)"
    return
  fi
  if [[ "$SKIP_FLYCTL_INSTALL" -eq 1 ]]; then
    die "flyctl not found and --skip-flyctl-install was set. Install it yourself: https://fly.io/docs/flyctl/install/"
  fi

  log_warn "flyctl not found; installing"
  local os
  os="$(uname -s)"
  if [[ "$os" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    run brew install flyctl
  else
    run bash -c "curl -L https://fly.io/install.sh | sh"
    export PATH="$HOME/.fly/bin:$PATH"
    log_warn "Add 'export PATH=\"\$HOME/.fly/bin:\$PATH\"' to your shell profile to persist this."
  fi

  command -v fly >/dev/null 2>&1 || die "flyctl installation did not put 'fly' on PATH. Open a new shell and re-run this script."
  log_ok "flyctl installed: $(fly version 2>/dev/null | head -n1)"
}

# ---------------------------------------------------------------------------
# Step: required tooling (python3 for JSON parsing, openssl for secrets)
# ---------------------------------------------------------------------------
ensure_tooling() {
  log_step "Checking required tools"
  command -v python3 >/dev/null 2>&1 || die "python3 is required (used to parse flyctl --json output). Install it and re-run."
  command -v openssl >/dev/null 2>&1 || die "openssl is required (used to generate secrets). Install it and re-run."
  command -v curl >/dev/null 2>&1 || die "curl is required. Install it and re-run."
  log_ok "python3, openssl, curl available"
}

# ---------------------------------------------------------------------------
# Step: secret env vars (FLY_API_TOKEN, OPENROUTER_API_KEY)
# ---------------------------------------------------------------------------
prompt_secret() {
  # $1 = var name, $2 = human description
  local var="$1" desc="$2" value
  if [[ -n "${!var:-}" ]]; then
    return
  fi
  if [[ "$NON_INTERACTIVE" -eq 1 || ! -t 0 ]]; then
    die "$var is not set. Export it before running (see api/DEPLOYMENT.md): export $var=... . Refusing to prompt because --yes/non-interactive or no TTY."
  fi
  log_warn "$var is not set."
  read -r -s -p "Paste $desc (input hidden, not stored anywhere but this session): " value
  echo
  [[ -n "$value" ]] || die "$var must not be empty."
  export "$var"="$value"
}

ensure_secrets_present() {
  log_step "Checking required environment variables"
  prompt_secret FLY_API_TOKEN "your Fly.io API token"
  prompt_secret OPENROUTER_API_KEY "your OpenRouter API key"
  log_ok "FLY_API_TOKEN and OPENROUTER_API_KEY are set"
}

ensure_fly_auth() {
  log_step "Verifying Fly.io authentication"
  local who
  if ! who="$(fly auth whoami 2>&1)"; then
    log_err "Could not authenticate with FLY_API_TOKEN. flyctl said:"
    log_err "$who"
    exit 1
  fi
  log_ok "Authenticated as $who"
}

# ---------------------------------------------------------------------------
# Step: resolve org
# ---------------------------------------------------------------------------
resolve_org() {
  log_step "Resolving Fly org"
  if [[ -n "$ORG" ]]; then
    log_ok "Using org: $ORG"
    return
  fi
  local orgs_json count
  orgs_json="$(fly orgs list --json)"
  count="$(printf '%s' "$orgs_json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
  if [[ "$count" -eq 1 ]]; then
    ORG="$(printf '%s' "$orgs_json" | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin))))')"
    log_ok "Auto-detected org: $ORG"
  else
    die "You belong to $count orgs; pass one explicitly with --org (options: $(printf '%s' "$orgs_json" | python3 -c 'import json,sys; print(", ".join(json.load(sys.stdin).keys()))'))"
  fi
}

# ---------------------------------------------------------------------------
# JSON existence helpers
#
# These must never confuse "the lookup failed" with "the resource does not
# exist". Answering "no" to a failed lookup silently rotates a live
# ADMIN_TOKEN / WEBHOOK_SECRET and provisions a second, billable Postgres
# cluster, so a flyctl error or an unparseable payload aborts the run.
#
# The names being searched for are passed to python via argv, never
# interpolated into its source, so an app or bucket name containing a quote
# cannot turn into a syntax error.
# ---------------------------------------------------------------------------
APP_EXISTS_NOW=0   # set by ensure_app; stays 0 under --dry-run (see below)
_LOOKUP_OUT=""

# Reads JSON on stdin. Exits 0 if $2 is present in field $1, 1 if absent, and
# 2 if the payload is not the list-of-objects flyctl is supposed to emit.
_json_has_name() {
  python3 -c '
import json, sys
field, wanted = sys.argv[1], sys.argv[2]
try:
    names = {item[field] for item in json.load(sys.stdin)}
except Exception as exc:
    sys.stderr.write("could not parse flyctl JSON output: %s\n" % exc)
    sys.exit(2)
sys.exit(0 if wanted in names else 1)
' "$1" "$2"
}

# $@ = a read-only flyctl command; leaves its stdout in $_LOOKUP_OUT and dies
# if it failed. `die` runs in the function body rather than inside a subshell,
# so it exits the whole script even though every caller invokes these checks
# from an `if` condition (which suppresses errexit for the callee).
_fly_lookup() {
  local rc=0
  _LOOKUP_OUT="$("$@" 2>&1)" || rc=$?
  if (( rc != 0 )); then
    log_err "Could not query Fly.io state: $*"
    log_err "$_LOOKUP_OUT"
    die "Aborting: a failed lookup must not be read as 'this resource does not exist'."
  fi
}

app_exists() {
  local rc=0
  _fly_lookup fly apps list --json
  printf '%s' "$_LOOKUP_OUT" | _json_has_name Name "$1" || rc=$?
  if (( rc == 2 )); then
    die "Unexpected 'fly apps list --json' output; cannot tell whether app '$1' exists."
  fi
  return "$rc"
}

secret_exists() {
  # $1 = app name, $2 = secret name
  local rc=0
  # Under --dry-run the app was never actually created, so there is nothing to
  # query and every per-app question is answered "no" without calling flyctl.
  if (( APP_EXISTS_NOW == 0 )); then return 1; fi
  _fly_lookup fly secrets list -a "$1" --json
  printf '%s' "$_LOOKUP_OUT" | _json_has_name name "$2" || rc=$?
  if (( rc == 2 )); then
    die "Unexpected 'fly secrets list --json' output; cannot tell whether '$2' is set on '$1'."
  fi
  return "$rc"
}

volume_exists() {
  # $1 = app name, $2 = volume name
  local rc=0
  if (( APP_EXISTS_NOW == 0 )); then return 1; fi
  _fly_lookup fly volumes list -a "$1" --json
  printf '%s' "$_LOOKUP_OUT" | _json_has_name name "$2" || rc=$?
  if (( rc == 2 )); then
    die "Unexpected 'fly volumes list --json' output; cannot tell whether volume '$2' exists."
  fi
  return "$rc"
}

# ---------------------------------------------------------------------------
# Step: app
# ---------------------------------------------------------------------------
ensure_app() {
  log_step "Checking Fly app '$APP_NAME'"
  if app_exists "$APP_NAME"; then
    APP_EXISTS_NOW=1
    log_ok "App '$APP_NAME' already exists"
    return
  fi
  [[ -n "${DATABASE_URL:-}" || "$PROVISION_POSTGRES" -eq 1 ]] || die "A new app requires DATABASE_URL, or --provision-postgres. No resources created."
  log_info "Creating app '$APP_NAME' in org '$ORG'"
  if ! run fly apps create "$APP_NAME" -o "$ORG" --yes; then
    die "Could not create app '$APP_NAME' (name may already be taken globally on Fly.io). Retry with --app-name <something-unique>."
  fi
  # Only a real run actually created it; --dry-run must not go on to query an
  # app that is not there.
  [[ "$DRY_RUN" -eq 0 ]] && APP_EXISTS_NOW=1
  log_ok "Created app '$APP_NAME'"
}

ensure_ips() {
  log_step "Checking public ingress addresses"
  _fly_lookup fly ips list -a "$APP_NAME" --json
  local families
  families="$(printf '%s' "$_LOOKUP_OUT" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
if not isinstance(rows, list): raise ValueError("expected an IP list")
types = {row["Type"] for row in rows}
print("v4" if types.intersection({"v4", "shared_v4"}) else "")
print("v6" if "v6" in types else "")
')" || die "Could not parse public IP state."
  if [[ "$families" != *v4* ]]; then run fly ips allocate-v4 --shared -a "$APP_NAME"; fi
  if [[ "$families" != *v6* ]]; then run fly ips allocate-v6 -a "$APP_NAME"; fi
}

# ---------------------------------------------------------------------------
# Step: Postgres
# ---------------------------------------------------------------------------
ensure_postgres() {
  log_step "Checking Postgres for '$APP_NAME'"
  # Gate the whole step on whether the app already has a working
  # DATABASE_URL -- NOT on whether an app named "$DB_APP_NAME" exists.
  # The attached cluster may have been created under a different name
  # (e.g. by a manual `fly postgres create --name ...` before this script
  # was used), and guessing wrong must never cause a second, orphaned,
  # billable cluster to be created and left attached to nothing.
  if [[ -n "${DATABASE_URL:-}" ]]; then
    printf 'DATABASE_URL=%s\n' "$DATABASE_URL" | fly secrets import --stage -a "$APP_NAME"
    log_ok "Imported the supplied DATABASE_URL"
    return
  fi
  if secret_exists "$APP_NAME" "DATABASE_URL"; then
    log_ok "'$APP_NAME' already has DATABASE_URL set; skipping Postgres provisioning"
    return
  fi

  [[ "$PROVISION_POSTGRES" -eq 1 ]] || die "Set DATABASE_URL to your own Postgres (recommended), or explicitly choose --provision-postgres for unmanaged Fly Postgres."
  if app_exists "$DB_APP_NAME"; then
    log_ok "Postgres cluster '$DB_APP_NAME' already exists"
  else
    log_info "Creating Postgres cluster '$DB_APP_NAME' (this takes ~1-2 minutes)"
    run_private fly postgres create \
      --name "$DB_APP_NAME" \
      --region "$REGION" \
      --org "$ORG" \
      --initial-cluster-size 1 \
      --vm-size "$PG_VM_SIZE" \
      --volume-size "$PG_VOLUME_SIZE_GB"
    log_ok "Created Postgres cluster '$DB_APP_NAME'"
  fi

  log_info "Attaching '$DB_APP_NAME' to '$APP_NAME'"
  run fly postgres attach "$DB_APP_NAME" -a "$APP_NAME"
  log_ok "Attached Postgres (DATABASE_URL set)"
}

# ---------------------------------------------------------------------------
# Step: Tigris object storage
# ---------------------------------------------------------------------------
ensure_storage() {
  log_step "Checking object storage bucket '$BUCKET_NAME'"
  if [[ -n "${S3_BUCKET:-}" ]]; then
    local name
    for name in S3_BUCKET S3_REGION S3_ENDPOINT_URL S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_FORCE_PATH_STYLE; do
      if [[ -n "${!name:-}" ]]; then
        printf '%s=%s\n' "$name" "${!name}"
      fi
    done | fly secrets import --stage -a "$APP_NAME"
    log_ok "Imported supplied S3 configuration"
    return
  fi
  if secret_exists "$APP_NAME" "S3_BUCKET" || secret_exists "$APP_NAME" "AWS_ACCESS_KEY_ID"; then
    log_ok "'$APP_NAME' already has storage credentials; skipping"
    return
  fi
  log_info "Creating Tigris bucket '$BUCKET_NAME'"
  run_private fly storage create -a "$APP_NAME" -n "$BUCKET_NAME" --yes
  log_ok "Created storage bucket and injected AWS_* secrets"
}

# ---------------------------------------------------------------------------
# Step: worker scratch volume
# ---------------------------------------------------------------------------
ensure_volume() {
  log_step "Checking volume '$WORK_VOLUME_NAME'"
  if volume_exists "$APP_NAME" "$WORK_VOLUME_NAME"; then
    log_ok "Volume '$WORK_VOLUME_NAME' already exists"
    return
  fi
  log_info "Creating ${WORK_VOLUME_SIZE_GB}GB volume '$WORK_VOLUME_NAME'"
  run fly volumes create "$WORK_VOLUME_NAME" -a "$APP_NAME" -r "$REGION" -s "$WORK_VOLUME_SIZE_GB" --yes
  log_ok "Created volume '$WORK_VOLUME_NAME'"
}

# ---------------------------------------------------------------------------
# Step: secrets (ADMIN_TOKEN / WEBHOOK_SECRET generated once; OPENROUTER_API_KEY
# always set from the current environment value)
# ---------------------------------------------------------------------------
ensure_app_secrets() {
  log_step "Checking application secrets"
  # NAME=VALUE pairs are piped to `fly secrets import` over stdin -- never
  # passed as argv and never printed -- so values never land in process
  # listings, shell history, or this script's own command-echo output.
  local secret_names=() secret_lines="" generated_admin="" generated_webhook="" name
  for name in WEBHOOK_ALLOWED_HOSTS CORS_ORIGINS DEFAULT_MODEL OPENROUTER_URL ALLOWED_MODELS REQUIRE_USER_ID MAX_PENDING_UPLOADS UPLOADS_PER_HOUR STARTS_PER_HOUR REQUIRE_PROVIDER_BUDGET REQUEST_RESERVE_USD REPORT_RETENTION_DAYS; do
    if [[ -n "${!name:-}" ]]; then
      secret_names+=("$name")
      secret_lines+="$name=${!name}"$'\n'
    fi
  done

  secret_names+=("OPENROUTER_API_KEY")
  secret_lines+="OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"$'\n'

  if secret_exists "$APP_NAME" "ADMIN_TOKEN"; then
    log_ok "ADMIN_TOKEN already set; leaving it untouched"
  else
    generated_admin="$(openssl rand -hex 32)"
    secret_names+=("ADMIN_TOKEN")
    secret_lines+="ADMIN_TOKEN=${generated_admin}"$'\n'
    log_info "Generated a new ADMIN_TOKEN"
  fi

  if secret_exists "$APP_NAME" "WEBHOOK_SECRET"; then
    log_ok "WEBHOOK_SECRET already set; leaving it untouched"
  else
    generated_webhook="$(openssl rand -hex 32)"
    secret_names+=("WEBHOOK_SECRET")
    secret_lines+="WEBHOOK_SECRET=${generated_webhook}"$'\n'
    log_info "Generated a new WEBHOOK_SECRET"
  fi


  if [[ -n "$generated_admin" || -n "$generated_webhook" ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    # umask in a subshell so the file is created 0600 *before* any secret
    # reaches the disk; a later chmod would leave a world-readable window.
    [[ ! -L "$CREDENTIALS_FILE" ]] || die "Refusing a symlink credentials file."
    (
      umask 077
      touch "$CREDENTIALS_FILE"
      chmod 600 "$CREDENTIALS_FILE"
      {
        echo "# Courtside API credentials for '$APP_NAME' -- generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "# Fly does not let you read secret values back later; keep this file safe (e.g. a password manager) then delete it."
        if [[ -n "$generated_admin" ]]; then echo "ADMIN_TOKEN=$generated_admin"; fi
        if [[ -n "$generated_webhook" ]]; then echo "WEBHOOK_SECRET=$generated_webhook"; fi
      } >> "$CREDENTIALS_FILE"
    )
    chmod 600 "$CREDENTIALS_FILE"   # tighten a file left over from an older run
    log_warn "Freshly generated secret(s) saved to $CREDENTIALS_FILE -- move it somewhere durable and out of the repo."
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '  %s[dry-run]%s fly secrets import --stage -a %s   (setting: %s)\n' \
      "$C_YELLOW" "$C_RESET" "$APP_NAME" "${secret_names[*]}"
  else
    printf '  %s$%s fly secrets import --stage -a %s   (setting: %s, values withheld)\n' \
      "$C_BOLD" "$C_RESET" "$APP_NAME" "${secret_names[*]}"
    printf '%s' "$secret_lines" | fly secrets import --stage -a "$APP_NAME"
  fi

}

# ---------------------------------------------------------------------------
# Step: deploy
#
# Known flyctl quirk (observed on flyctl v0.4.95): when -c points at a
# fly.toml in a subdirectory, the [build].dockerfile path is resolved
# relative to that subdirectory instead of the invocation directory, even
# with an explicit --dockerfile flag or a [WORKING_DIRECTORY] positional
# arg. Since api/Dockerfile must be built with the REPO ROOT as context
# (it needs the top-level `courtside` package), we work around this by
# deploying from a temporary copy of fly.toml placed at the repo root.
# ---------------------------------------------------------------------------
deploy_app() {
  log_step "Deploying '$APP_NAME'"
  local tmp_toml
  tmp_toml="$(mktemp "$REPO_ROOT/fly.deploy.XXXXXX.toml")"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log_info "[dry-run] would copy $FLY_TOML_SRC to $tmp_toml, rewriting app/primary_region/S3_BUCKET"
    run fly deploy -c "$tmp_toml" -a "$APP_NAME"
    return
  fi
  trap 'rm -f -- "$tmp_toml"' RETURN
  trap "rm -f -- '$tmp_toml'" EXIT

  cp "$FLY_TOML_SRC" "$tmp_toml"
  # Keep the config in sync with the resolved app name / region / bucket,
  # regardless of what the checked-in api/fly.toml literally says.
  python3 - "$tmp_toml" "$APP_NAME" "$REGION" "$BUCKET_NAME" <<'PY'
import re, sys
path, app_name, region, bucket = sys.argv[1:5]
text = open(path).read()
# Match the whole right-hand side rather than assuming a particular quote
# style: flyctl writes fly.toml with single quotes, so a pattern requiring
# double quotes skips silently and leaves the app pointed at a bucket the
# deployment does not own. Every rewrite is asserted for the same reason.
rewrites = [
    (r'^app\s*=.*$', f'app = "{app_name}"'),
    (r'^primary_region\s*=.*$', f'primary_region = "{region}"'),
]
for pattern, repl in rewrites:
    text, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"deploy.sh: expected 1 match for {pattern!r} in fly.toml, found {n}")
open(path, 'w').write(text)
PY

  run fly deploy -c "$tmp_toml" -a "$APP_NAME" --ha=false
  rm -f -- "$tmp_toml"
  trap - RETURN EXIT
  log_ok "Deploy complete"
}

# ---------------------------------------------------------------------------
# Step: verify
# ---------------------------------------------------------------------------
verify_health() {
  log_step "Verifying deployment"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log_info "[dry-run] would curl https://$APP_NAME.fly.dev/readyz"
    return
  fi
  local url="https://$APP_NAME.fly.dev/readyz" code attempt
  for attempt in $(seq 1 10); do
    code="$(curl --connect-timeout 5 --max-time 10 -sS -o /dev/null -w '%{http_code}' "$url" || true)"
    if [[ "$code" == "200" ]]; then
      log_ok "Health check passed ($url -> 200)"
      return
    fi
    log_info "Attempt $attempt/10: got HTTP ${code:-none}, retrying in 5s..."
    sleep 5
  done
  die "Readiness check did not return 200 after 10 attempts. Check: fly logs -a $APP_NAME"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
  log_step "Done"
  cat <<EOF
App:        $APP_NAME
URL:        https://$APP_NAME.fly.dev
Docs:       https://$APP_NAME.fly.dev/docs
Postgres:   $DB_APP_NAME
Bucket:     $BUCKET_NAME
Region:     $REGION

Next step -- mint the first API key:
  ADMIN_TOKEN=\$(grep '^ADMIN_TOKEN=' "$CREDENTIALS_FILE" | tail -n1 | cut -d= -f2)   # if freshly generated
  curl -X POST https://$APP_NAME.fly.dev/v1/admin/accounts \\
    -H "X-Admin-Token: \$ADMIN_TOKEN" -H 'Content-Type: application/json' \\
    -d '{"name":"Acme Tennis Academy","monthly_usd_cap":200}'

See api/DEPLOYMENT.md for operations, troubleshooting, and teardown instructions.
EOF
}

main() {
  # `fly deploy [WORKING_DIRECTORY]` uses the current directory as the Docker
  # build context, and api/Dockerfile copies pyproject.toml and the top-level
  # `courtside` package from the repo root. Without this cd the build context
  # is wherever the caller happened to be, which breaks the "run from
  # anywhere" promise in the header above.
  cd "$REPO_ROOT"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    cat <<EOF
Offline plan (no credentials, network calls or resource changes):
  App: $APP_NAME; org: ${ORG:-choose with --org}; region: $REGION
  Database: supplied/existing DATABASE_URL; unmanaged provisioning opt-in: $PROVISION_POSTGRES
  Storage: supplied/existing S3 configuration, otherwise new Tigris bucket $BUCKET_NAME
  Worker scratch: $WORK_VOLUME_SIZE_GB GB volume $WORK_VOLUME_NAME
  Preserve existing ADMIN_TOKEN and WEBHOOK_SECRET; import supplied provider keys.
  Build from repository root; deploy API and worker; require /readyz to return 200.
Existing resources will be inspected during the real run. No availability is assumed.
EOF
    return
  fi
  [[ -n "${DATABASE_URL:-}" || "$PROVISION_POSTGRES" -eq 1 ]] || log_info "DATABASE_URL must already exist on the target app."
  ensure_tooling
  ensure_flyctl
  ensure_secrets_present
  ensure_fly_auth
  resolve_org
  ensure_app
  ensure_ips
  ensure_postgres
  ensure_storage
  ensure_volume
  ensure_app_secrets
  deploy_app
  verify_health
  print_summary
}

main
