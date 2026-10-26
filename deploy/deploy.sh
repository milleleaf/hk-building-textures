#!/usr/bin/env bash
# deploy.sh <dir> <project> [message]: make sure the Pages project exists, record its real subdomain on the bucket, deploy <dir> to it.
# Run from deploy/ (pinned wrangler in node_modules). Needs CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID (repo secrets) and HF_TOKEN.
# Prints no secret: the API calls only keep the status code or the subdomain.
set -euo pipefail
dir=$1; proj=$2; msg=${3:-deploy}
api="https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/pages/projects"
auth="Authorization: Bearer $CLOUDFLARE_API_TOKEN"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "$auth" "$api/$proj")
if [ "$code" = 404 ]; then
  echo "creating Pages project $proj"; ./node_modules/.bin/wrangler pages project create "$proj" --production-branch main
elif [ "$code" != 200 ]; then echo "project lookup for $proj: HTTP $code"; exit 1; fi
sub=$(curl -s -H "$auth" "$api/$proj" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['subdomain'])")
python3 ../pages.py project "$proj" "$sub"
echo "serving at https://$sub"
./node_modules/.bin/wrangler pages deploy "$dir" --project-name "$proj" --branch main --commit-dirty=true --commit-message "$msg"
