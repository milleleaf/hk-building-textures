#!/usr/bin/env bash
# Runs of the cbd and bake workflows that have not completed, other than this run: "<id> <status> <event>" per line (empty = none).
# `activeruns.sh ignore-young-cron` also ignores a scheduled run younger than 3 min (a backup tick that is just deciding to exit).
# Needs GH_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID.
set -euo pipefail
for w in cbd.yml bake.yml; do
  gh run list --repo "$GITHUB_REPOSITORY" --workflow "$w" --limit 30 --json databaseId,status,event,createdAt
done | jq -r --arg self "$GITHUB_RUN_ID" --argjson now "$(date +%s)" --arg young "${1:-}" '
  .[] | select(.status != "completed") | select((.databaseId | tostring) != $self)
  | select(($young == "ignore-young-cron" and .event == "schedule" and ($now - (.createdAt | fromdateiso8601)) < 180) | not)
  | "\(.databaseId) \(.status) \(.event)"'
