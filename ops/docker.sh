# Sourced by backup.sh and restore.sh: find the docker CLI.
#
# $DOCKER if set; otherwise `docker` on PATH; otherwise the CLI bundled inside
# Docker Desktop for macOS, whose bin directory also holds the credential
# helper the CLI needs, so that directory goes on PATH too. Fails with a
# message rather than a "command not found" half way through a backup.
if [ -z "${DOCKER:-}" ]; then
    if command -v docker >/dev/null 2>&1; then
        DOCKER="$(command -v docker)"
    elif [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
        DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
    else
        echo "docker not found: install it, or set DOCKER=/path/to/docker" >&2
        exit 1
    fi
fi
PATH="$(dirname "$DOCKER"):$PATH"
export DOCKER PATH
