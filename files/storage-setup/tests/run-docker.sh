#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_directory="$(dirname -- "${script_directory}")"
image="$(docker build --quiet --file "${script_directory}/Dockerfile" "${project_directory}")"

if [[ ! "${image}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  printf 'docker build returned an invalid image id: %s\n' "${image}" >&2
  exit 1
fi

docker run --rm \
  --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --pids-limit=64 --memory=1g --cpus=2 \
  --tmpfs /tmp:rw,nosuid,nodev,size=768m \
  --tmpfs /etc/storage-setup.d:rw,nosuid,nodev,noexec,size=1m,uid=65534,gid=65534,mode=0700 \
  "${image}"
