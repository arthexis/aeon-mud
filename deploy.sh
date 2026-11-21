#!/bin/sh
# Canonical owner deployment entrypoint. Ansible provides identity and host checks.
set -eu
if [ "$#" -ne 2 ] || [ "$1" != "--expected-sha" ]; then
  echo "usage: ./deploy.sh --expected-sha <40-character SHA>" >&2
  exit 2
fi
case "$2" in
  *[!0-9a-fA-F]*|'') echo "invalid SHA" >&2; exit 2 ;;
esac
[ "${#2}" -eq 40 ] || { echo "SHA must be 40 hex characters" >&2; exit 2; }
command -v ansible-playbook >/dev/null || { echo "ansible-core is required" >&2; exit 2; }
root=$(CDPATH= cd -- "$(dirname "$0")" && pwd -P)
exec ansible-playbook -i "$root/ansible/inventory.yml" "$root/ansible/playbook.yml" \
  -e "aeon_mud_source=$root" -e "aeon_mud_expected_sha=$2"
