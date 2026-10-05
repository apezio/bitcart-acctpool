#!/bin/bash
# Download the wheel files that the signer image needs into signer/wheels/ (SPEC 7.8).
# Run it before the image build. The build itself has no network (podman build --network none).
#
# Each file has an exact name and a SHA-256 that is written here. A file with another hash is refused
# and does not stay in wheels/. The same hash is in requirements.txt; the install checks it again.
# WHEEL_URL_<n> in the environment can name a mirror. The hash check is the same for a mirror.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DIR="$HERE/wheels"

# name | sha256 | url
WHEELS=(
"coincurve-21.0.0-cp312-cp312-musllinux_1_2_x86_64.whl|bcc0831f07cb75b91c35c13b1362e7b9dc76c376b27d01ff577bec52005e22a8|https://files.pythonhosted.org/packages/ca/22/7ec3ec4c8e7764daa25767d6674cb5741ea2d9b39ff758e9918d22a4b49b/coincurve-21.0.0-cp312-cp312-musllinux_1_2_x86_64.whl"
)

hash_of() { sha256sum "$1" | cut -d' ' -f1; }

mkdir -p "$DIR"
number=0
for entry in "${WHEELS[@]}"; do
    number=$((number + 1))
    IFS='|' read -r name sha url <<<"$entry"
    mirror="WHEEL_URL_$number"
    url="${!mirror:-$url}"
    grep -q -- "--hash=sha256:$sha" "$HERE/requirements.txt" || {
        echo "fetch-wheels: the hash of $name is not the hash in requirements.txt" >&2; exit 1; }

    if [ -e "$DIR/$name" ]; then
        if [ "$(hash_of "$DIR/$name")" = "$sha" ]; then
            echo "fetch-wheels: ok, in wheels/ with the correct SHA-256: $name"
            continue
        fi
        echo "fetch-wheels: REFUSED: wheels/$name has a wrong SHA-256. Remove the file and run this script again." >&2
        exit 1
    fi

    tmp=$(mktemp "$DIR/.download.XXXXXX")
    trap 'rm -f "$tmp"' EXIT
    echo "fetch-wheels: download $name"
    curl --fail --silent --show-error --location --proto '=https,file' --max-time 120 --output "$tmp" "$url"
    got=$(hash_of "$tmp")
    if [ "$got" != "$sha" ]; then
        rm -f "$tmp"
        echo "fetch-wheels: REFUSED: the download of $name has SHA-256 $got, not $sha. Nothing is kept." >&2
        exit 1
    fi
    chmod 0644 "$tmp"
    mv "$tmp" "$DIR/$name"
    trap - EXIT
    echo "fetch-wheels: ok, SHA-256 is correct: $name"
done
