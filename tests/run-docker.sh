#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_directory="$(git -C "${script_directory}" rev-parse --show-toplevel)"
image="$(docker build --quiet --file "${repository_directory}/tests/Dockerfile" "${repository_directory}")"

if [[ ! "${image}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  printf 'docker build returned an invalid image id: %s\n' "${image}" >&2
  exit 1
fi

docker run --rm \
  --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --pids-limit=64 --memory=1g --cpus=2 \
  --tmpfs /tmp:rw,nosuid,nodev,size=768m \
  --tmpfs /etc/filesystems.d:rw,nosuid,nodev,noexec,size=1m,uid=65534,gid=65534,mode=0700 \
  "${image}"
