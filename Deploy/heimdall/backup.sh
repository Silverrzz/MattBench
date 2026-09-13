#!/usr/bin/env bash
set -euo pipefail
umask 077
backup_dir=/home/nocturn9x/openbench-postgres/backups
mkdir -p "$backup_dir"
exec 9>"$backup_dir/.backup.lock"
flock -n 9 || exit 0
backup_stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_tmpfile=$(mktemp "$backup_dir/.openbench.XXXXXX")
trap 'rm -f "$backup_tmpfile"' EXIT
docker exec openbench-postgres pg_dump -U postgres -d openbench -Fc > "$backup_tmpfile"
test -s "$backup_tmpfile"
docker exec -i openbench-postgres pg_restore --list < "$backup_tmpfile" > /dev/null
mv "$backup_tmpfile" "$backup_dir/openbench-$backup_stamp.dump"
sha256sum "$backup_dir/openbench-$backup_stamp.dump" > "$backup_dir/openbench-$backup_stamp.dump.sha256"
find "$backup_dir" -maxdepth 1 -type f \( -name 'openbench-*.dump' -o -name 'openbench-*.dump.sha256' \) -mtime +14 -delete
echo "PostgreSQL backup complete: $backup_dir/openbench-$backup_stamp.dump"
