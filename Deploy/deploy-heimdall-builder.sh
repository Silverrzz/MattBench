#!/usr/bin/env bash
# Run as nocturn9x over ssh -t; sudo prompts once before changing production.
# Usage: bash deploy-heimdall-builder.sh [--check] TARGET_COMMIT EXPECTED_HEAD
# Both revisions must already be available in the production Git repository.
set -Eeuo pipefail
umask 077

check_only=false
if [[ ${1:-} == --check ]]; then check_only=true; shift; fi
if [[ $# -ne 2 || $EUID -eq 0 || $(id -un) != nocturn9x ]]; then
    echo 'Run as nocturn9x: bash deploy-heimdall-builder.sh [--check] TARGET_COMMIT EXPECTED_HEAD' >&2
    exit 1
fi
site=/opt/mattbench-prod
static=/var/www/OpenBench/static
environment=/etc/mattbench-prod/production.env
services=(OpenBench.service mattbench-prod-training.service)
cd "$site"
exec 9> .git/builder-deploy.lock
flock -n 9 || { echo 'Another builder deployment is running.' >&2; exit 1; }
target=$(git rev-parse --verify "$1^{commit}")
before=$(git rev-parse --verify "$2^{commit}")
[[ $(git rev-parse HEAD) == "$before" ]] || { echo 'Production HEAD changed; review before deploying.' >&2; exit 1; }
[[ -z $(git status --porcelain --untracked-files=no) ]] || { echo 'Production has tracked changes; preserve and review them first.' >&2; exit 1; }
git merge-base --is-ancestor "$before" "$target"
[[ $before != "$target" ]] || { echo 'Target already deployed.'; exit 0; }

# This deployment intentionally has no database, dependency or service-config changes.
while IFS= read -r path; do
    case "$path" in
        OpenBench/data/builder_ranger.rs|Scripts/verify_ranger_resume.py|Scripts/verify_ranger_resume.rs) ;;
        OpenBench/tests/builder_fixtures.py) ;;
        Deploy/update-builder-schedule.py|OpenBench/tests/test_builder_update.py) ;;
        OpenBench/schedule_builder.py|OpenBench/builder_export.py|OpenBench/data/builder_main.rs|OpenBench/static/schedule_builder.js|OpenBench/training_checkpoints.py|Templates/OpenBench/schedule_builder.html|OpenBench/tests/test_builder.py|OpenBench/tests/test_gpu_continuation.py|Scripts/prepare_builder_gpu.py|Scripts/test_builder_browser.cjs|Scripts/verify_builder_rust.py|Scripts/verify_builder_export.py|docs/builder-workloads.md|Deploy/deploy-heimdall-builder.sh) ;;
        *) echo "Unexpected release change: $path. Review a separate deployment." >&2; exit 1 ;;
    esac
done < <(git diff --name-only "$before" "$target")
for service in "${services[@]}"; do
    [[ $(systemctl show "$service" -p WorkingDirectory --value) == "$site" ]]
    [[ $(systemctl show "$service" -p User --value) == nocturn9x ]]
    systemctl is-active --quiet "$service"
done
git diff --check "$before" "$target"
git diff --stat "$before" "$target"
echo "Ready to deploy $target over $before in $site."
if "$check_only"; then exit 0; fi

sudo -v
install -d -m 0700 /home/nocturn9x/mattbench-deploy-records
record=$(mktemp -d /home/nocturn9x/mattbench-deploy-records/builder.XXXXXX)
printf '%s\n' "$before" > "$record/previous-commit"
printf '%s\n' "$target" > "$record/target-commit"
git diff --binary "$before" "$target" > "$record/changes.patch"
sudo tar -C "$static" -cpf "$record/static-before.tar" .
echo "Deployment record and static backup: $record"
stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
advanced=false
published=false

rollback() {
    local status=$1
    trap - ERR INT TERM
    set +e
    echo "Deployment failed; rolling back. Record: $record" >&2
    sudo systemctl stop "${services[@]}"
    local failed=0
    if "$advanced"; then
        # Only undo this deployment, and refuse to overwrite intervening edits.
        if [[ $(git rev-parse HEAD) == "$target" && -z $(git status --porcelain --untracked-files=no) ]]; then
            git reset --keep "$before" || failed=1
        else
            echo 'Source changed during deployment; automatic source rollback refused.' >&2
            failed=1
        fi
    fi
    if "$published"; then sudo tar -C "$static" -xpf "$record/static-before.tar" || failed=1; fi
    if [[ $failed -eq 0 ]]; then
        sudo systemctl start "${services[@]}"
        echo "Restored previous source and static files. Check service health; details in $record." >&2
    else
        echo "Rollback needs attention; services remain stopped. Inspect $record." >&2
    fi
    exit "$status"
}
trap 'rollback $?' ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM

manage() {
    local task=$1
    shift
    sudo systemd-run --quiet --wait --pipe --collect --unit="mattbench-builder-$stamp-$task" \
        --property=User=nocturn9x --property=Group=nocturn9x \
        --property=WorkingDirectory="$site" --property=UMask=0022 \
        --property="EnvironmentFile=$environment" \
        /usr/bin/env OPENBENCH_DEBUG=0 OPENBENCH_DISABLE_WATCHERS=1 \
        OPENBENCH_STATIC_ROOT="$record/static" \
        "$site/venv/bin/python" manage.py "$@"
}

sudo systemctl stop "${services[@]}"
git merge --ff-only "$target"
advanced=true
manage check check
manage migrations migrate --check
manage static collectstatic --noinput
published=true
sudo rsync -rt --chmod=D755,F644 "$record/static/" "$static/"
sudo systemctl start "${services[@]}"
curl --fail --silent --show-error --retry 12 --retry-all-errors --retry-delay 2 \
    --connect-timeout 5 --max-time 15 https://chess.n9x.co/login/ > /dev/null
curl --fail --silent --show-error --retry 3 --retry-all-errors --retry-delay 2 \
    --connect-timeout 5 --max-time 15 \
    "https://chess.n9x.co/static/schedule_builder.js?v=$target" > "$record/served-schedule-builder.js"
cmp OpenBench/static/schedule_builder.js "$record/served-schedule-builder.js"
for service in "${services[@]}"; do systemctl is-active "$service"; done
[[ $(git rev-parse HEAD) == "$target" ]]
trap - ERR INT TERM
echo "Deployed $target to https://chess.n9x.co. Record: $record"
