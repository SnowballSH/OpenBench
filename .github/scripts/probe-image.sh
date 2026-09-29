#!/usr/bin/env bash
# Serves an image the way production does (read-only root, tmpfs /tmp, no
# capabilities, secret key from a file) and probes it, including a restart.
set -Eeuo pipefail

image="$1"
engine="${CONTAINER_ENGINE:-docker}"
workdir="$(mktemp -d)"
trap '"${engine}" rm -f openbench-probe >/dev/null 2>&1 || true; rm -rf "${workdir}"' EXIT

head -c 48 /dev/urandom | base64 > "${workdir}/secret-key"
chmod 0444 "${workdir}/secret-key"

"${engine}" run -d --name openbench-probe -p 127.0.0.1:18080:8080 \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64M \
    --cap-drop=ALL --security-opt no-new-privileges \
    -v "${workdir}/secret-key:/run/openbench/secret-key:ro" \
    -e OPENBENCH_SECRET_KEY_FILE=/run/openbench/secret-key \
    -e OPENBENCH_ALLOWED_HOSTS=openbench.example,127.0.0.1 \
    -e OPENBENCH_BEHIND_TLS_PROXY=1 \
    "${image}" >/dev/null

status() {
    curl -s -o /dev/null -w '%{http_code}' "$@"
}

wait_healthy() {
    for _ in $(seq 60); do
        [[ "$(status http://127.0.0.1:18080/health/)" == 200 ]] && return 0
        sleep 1
    done
    "${engine}" logs openbench-probe >&2
    echo "health never answered 200" >&2
    return 1
}

expect() {
    local want="$1"; shift
    local got
    got="$(status "$@")"
    [[ "${got}" == "${want}" ]] || { echo "expected ${want}, got ${got}: $*" >&2; exit 1; }
}

wait_healthy
expect 200 -H 'Host: openbench.example' -H 'X-Forwarded-Proto: https' http://127.0.0.1:18080/login/
expect 302 -H 'Host: openbench.example' -H 'X-Forwarded-Proto: https' http://127.0.0.1:18080/index/
expect 200 -H 'Host: openbench.example' http://127.0.0.1:18080/static/style.css
expect 400 -H 'Host: attacker.example' http://127.0.0.1:18080/login/

"${engine}" restart openbench-probe >/dev/null
wait_healthy

if "${engine}" logs openbench-probe 2>&1 | grep -E 'Traceback|\[ERROR\]'; then
    echo "errors in the container log" >&2
    exit 1
fi
echo "image probe passed"
