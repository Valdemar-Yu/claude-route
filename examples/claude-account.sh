#!/bin/sh
# Generic per-account launcher for claude-accounts (POSIX version of
# claude-account.cmd).
# Usage: claude-account.sh <slug> [claude args...]
#
# On macOS the token lives in the login Keychain, written by
# `claude-accounts add`; on Linux and Windows it is tokens.json under the
# claude-accounts root. Either way, let claude-accounts own auth:
# `claude-accounts launch` sets CLAUDE_CONFIG_DIR for the slug and attaches
# the statusline that writes usage/<slug>.json.
if [ "$#" -lt 1 ]; then
    echo "usage: claude-account.sh <slug> [claude args...]" >&2
    exit 2
fi
slug=$1
shift

# Never let an API key or a third-party endpoint win over the subscription.
unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN
unset CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX

exec claude-accounts launch "$slug" "$@"
