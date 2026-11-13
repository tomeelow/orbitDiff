#!/bin/sh
# The first deploy, on the machine: deploy/setup.sh
#
# It fills in deploy/.env (asking for what's missing and generating the two secrets), makes
# the folders the containers write to, starts the stack and checks it. Run it again whenever
# you like: it keeps every value deploy/.env already has. Runbook: docs/deploy.md.
set -eu
cd "$(dirname "$0")"

say() { printf 'setup: %s\n' "$*"; }
die() { printf 'setup: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null || die "git is missing."
command -v openssl >/dev/null || die "openssl is missing."
docker compose version >/dev/null 2>&1 ||
    die "Docker with the compose plugin is missing: https://docs.docker.com/engine/install/"
case "$(uname -m)" in
    x86_64 | aarch64 | arm64) ;;
    *) die "this machine is $(uname -m): the images are built for x86-64 and arm64 only." ;;
esac

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    say "created deploy/.env from the example."
fi

# get NAME: its value in .env, without the quotes a value may be wrapped in.
get() {
    sed -n "s|^$1=||p" .env | head -n 1 | sed "s|^'\(.*\)'\$|\1|"
}

# put NAME VALUE: set it in .env, on its own line where it was, or at the end.
put() {
    if grep -q "^$1=" .env; then
        updated=$(K="$1" V="$2" awk 'index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["K"] "=" ENVIRON["V"]; next } { print }' .env)
        printf '%s\n' "$updated" > .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

# ask NAME QUESTION [secret]: prompt for NAME if .env has no value for it.
ask() {
    [ -z "$(get "$1")" ] || return 0
    [ -t 0 ] || die "$1 is empty in deploy/.env, and there's no terminal to ask on. Fill it in and run this again."
    answer=
    while [ -z "$answer" ]; do
        printf '%s: ' "$2"
        if [ "${3:-}" = secret ]; then
            stty -echo
            read -r answer || true
            stty echo
            printf '\n'
        else
            read -r answer || true
        fi
    done
    case "$1" in
        ORBITDIFF_ADMIN_EMAILS) answer="[\"$answer\"]" ;;
        ORBITDIFF_MAIL_FROM) case "$answer" in *"<"*) ;; *) answer="Orbit Diff <$answer>" ;; esac ;;
        # Quoted, so compose reads a `$` or a `#` in it as part of the password.
        ORBITDIFF_SMTP_PASSWORD) case "$answer" in *"'"*) ;; *) answer="'$answer'" ;; esac ;;
    esac
    put "$1" "$answer"
}

generate() {
    [ -n "$(get "$1")" ] && return 0
    put "$1" "$(openssl rand -hex 32)"
    say "generated $1."
}

[ "$(get ORBITDIFF_MAIL_FROM)" = "Orbit Diff <alerts@your-domain.example>" ] && put ORBITDIFF_MAIL_FROM ""
[ "$(get ORBITDIFF_ADMIN_EMAILS)" = "[]" ] && put ORBITDIFF_ADMIN_EMAILS ""

ask ORBITDIFF_DOMAIN "The domain whose DNS points at this machine (like orbit.example.com)"
ask ORBITDIFF_ADMIN_EMAILS "Your email address (you sign in with it, and see the admin view)"
ask ORBITDIFF_SMTP_HOST "Your email provider's SMTP host (like smtp.resend.com)"
# Only a relay on a private network goes without TLS, and it takes no credentials: the
# application refuses to send them in the clear.
if [ "$(get ORBITDIFF_SMTP_STARTTLS)" != false ]; then
    ask ORBITDIFF_SMTP_USERNAME "SMTP username"
    ask ORBITDIFF_SMTP_PASSWORD "SMTP password or API key (not shown)" secret
fi
ask ORBITDIFF_MAIL_FROM "The address emails come from, on a domain your provider has verified"
generate POSTGRES_PASSWORD
generate ORBITDIFF_SIGNING_KEY

if [ -z "$(get ORBITDIFF_REGISTRY)" ]; then
    # github.com/Owner/repo, over HTTPS or SSH -> ghcr.io/owner
    owner=$(git remote get-url origin 2>/dev/null | sed -n 's|.*github\.com[:/]\([^/]*\)/.*|\1|p' | tr '[:upper:]' '[:lower:]')
    put ORBITDIFF_REGISTRY "ghcr.io/${owner:-orbitdiff-local}"
fi
version=$(get ORBITDIFF_VERSION)
if [ -z "$version" ]; then
    git fetch --tags --quiet origin || true
    version=$(git tag --list 'v*' --sort=-version:refname | head -n 1)
    [ -n "$version" ] || die "this repository has no version tag yet. Tag one first: git tag v1.0.0 && git push origin v1.0.0"
fi

domain=$(get ORBITDIFF_DOMAIN)
if command -v getent >/dev/null && ! getent hosts "$domain" >/dev/null; then
    say "WARNING: $domain doesn't resolve yet. Caddy can't get its certificate until the DNS record points here; it keeps trying."
fi

# The containers run as user 1000. Docker would create missing folders as root, and the
# worker couldn't write to them.
mkdir -p data backups
for folder in data backups ../eval; do
    [ "$(stat -c %u "$folder")" = 1000 ] && continue
    if [ "$(id -u)" = 0 ]; then chown -R 1000:1000 "$folder"; else sudo chown -R 1000:1000 "$folder"; fi
done

say "starting Orbit Diff $version. The first start pulls or builds the images, which takes a while."
./update.sh "$version"

say "waiting for the API..."
tries=0
until [ "$(docker compose ps api --format '{{.Health}}')" = healthy ]; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || die "the API didn't become healthy. Look at: docker compose logs migrate api"
    sleep 5
done

if docker compose exec -T api orbitdiff doctor; then
    say "everything checks out. Open https://$domain and sign in as $(get ORBITDIFF_ADMIN_EMAILS | tr -d '[]"')."
    say "to send yourself a test email first: docker compose exec api orbitdiff doctor --email you@example.com"
else
    die "the stack is up, but a check failed (above). Fix deploy/.env and run deploy/setup.sh again."
fi
