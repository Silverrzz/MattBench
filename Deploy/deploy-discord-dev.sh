#!/usr/bin/env bash
# Run as root on heimdall.tpc.local with the reviewed notification patch directory.
set -Eeuo pipefail
umask 077

if [[ $EUID -ne 0 ]]; then
    echo 'Run this script with sudo.' >&2
    exit 1
fi
stage=$(realpath "${1:-$(dirname "$0")}")
site=/opt/mattbench-dev
stamp=$(date -u +%Y%m%dT%H%M%SZ)
release=/var/lib/mattbench-dev-deploy/discord-$stamp
expected_head=4776a8266c1c79831114af8e9dce682ec966e33a

cd "$stage"
sha256sum --check SHA256SUMS
[[ $(git -c safe.directory="$site" -C "$site" rev-parse HEAD) == "$expected_head" ]] || {
    echo 'The dev base revision changed. Rebase and review the patch before deploying.' >&2
    exit 1
}
git -c safe.directory="$site" -C "$site" apply --check --whitespace=error-all "$stage/changes.patch"

install -d -m 0700 "$release"
cp "$stage/changes.patch" "$stage/changed-files.txt" "$release/"
echo "Deployment record and source backup: $release"
trap 'echo "Deployment failed. Inspect $release and the dev service journal before restarting services." >&2' ERR

# Keep the existing Config/config.json and all environment/credential files.
cd "$site"
while IFS= read -r file; do
    if [[ -f $file ]]; then printf '%s\n' "$file"; fi
done < "$release/changed-files.txt" > "$release/existing-files.txt"
tar -czf "$release/source-before.tar.gz" -T "$release/existing-files.txt"

systemctl stop mattbench-dev-web.service mattbench-dev-training.service
if systemctl cat mattbench-dev-discord.service >/dev/null 2>&1; then
    systemctl stop mattbench-dev-discord.service
fi
systemctl start mattbench-dev-backup.service
git -c safe.directory="$site" -C "$site" apply --whitespace=error-all "$release/changes.patch"
while IFS= read -r file; do
    chmod 0644 "$site/$file"
done < "$release/changed-files.txt"
chmod 0755 "$site/Deploy/deploy-discord-dev.sh"

manage() {
    local task=$1
    shift
    systemd-run --quiet --wait --pipe --collect --unit="mattbench-dev-discord-deploy-$stamp-$task" \
        --property=User=mattbench-dev --property=Group=mattbench-dev \
        --property=WorkingDirectory="$site" \
        --property=UMask=0022 \
        --property=EnvironmentFile=/etc/mattbench-dev/production.env \
        /usr/bin/env OPENBENCH_DEBUG=0 OPENBENCH_DISABLE_WATCHERS=1 \
        OPENBENCH_STATIC_ROOT=/var/www/MattBenchDev/static \
        "$site/venv/bin/python" manage.py "$@"
}
manage migrate migrate --noinput
manage check check
manage static collectstatic --noinput
manage initialize shell -c "from OpenBench.models import DiscordConfiguration; c, created = DiscordConfiguration.objects.get_or_create(pk=1, defaults={'canonical_url': 'https://devbench.nocturn9x.space'}); print('Discord configuration created:', created, '; platform enabled:', c.enabled)"

sed -e 's|/opt/openbench|/opt/mattbench-dev|g' \
    -e 's|/etc/openbench|/etc/mattbench-dev|g' \
    -e 's|/var/lib/openbench|/var/lib/mattbench-dev|g' \
    -e 's|User=openbench|User=mattbench-dev|' \
    -e 's|Group=openbench|Group=mattbench-dev|' \
    -e 's|StateDirectory=openbench|StateDirectory=mattbench-dev|' \
    -e 's|postgresql.service|openbench-db.service|' \
    "$site/Deploy/openbench-discord.service" > /etc/systemd/system/mattbench-dev-discord.service
chmod 0644 /etc/systemd/system/mattbench-dev-discord.service
systemctl daemon-reload
systemctl start mattbench-dev-training.service mattbench-dev-web.service
systemctl enable --now mattbench-dev-discord.service
systemctl is-active mattbench-dev-web.service mattbench-dev-training.service mattbench-dev-discord.service

curl --fail --silent --show-error --retry 12 --retry-all-errors --retry-delay 2 \
    https://devbench.nocturn9x.space/login/ > /dev/null
curl --fail --silent --show-error \
    "https://devbench.nocturn9x.space/static/notifications.js?v=$stamp" > "$release/served-notifications.js"
cmp "$site/OpenBench/static/notifications.js" "$release/served-notifications.js"
echo 'Dev deployment complete. Configure Discord under Manage → Notifications and queue the mention-free test message.'
