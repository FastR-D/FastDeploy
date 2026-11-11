#!/usr/bin/env bash
set -euo pipefail
# Installs fixed commands only; does not stop/restart any application service.
test "$(id -u)" -eq 0
source_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
install -o root -g root -m 755 "$source_dir/system_services.py" /usr/local/sbin/fastdeploy-system-services
sudoers_file="$(mktemp)"
trap 'rm -f -- "$sudoers_file"' EXIT
cat > "$sudoers_file" <<'RULES'
dev ALL=(root) NOPASSWD: /usr/local/sbin/fastdeploy-system-services check fastread, /usr/local/sbin/fastdeploy-system-services quiesce fastread, /usr/local/sbin/fastdeploy-system-services restore fastread, /usr/local/sbin/fastdeploy-system-services check fastwrite, /usr/local/sbin/fastdeploy-system-services quiesce fastwrite, /usr/local/sbin/fastdeploy-system-services restore fastwrite
RULES
visudo -cf "$sudoers_file"
install -o root -g root -m 440 "$sudoers_file" /etc/sudoers.d/fastdeploy-system-services
printf '%s\n' 'Installed fixed FastRead/FastWrite service permissions; applications unchanged.'
