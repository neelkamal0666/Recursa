#!/usr/bin/env bash
# End-to-end v1 demo on the reference triage agent (offline, simulated model).
# Usage: bash examples/triage/demo.sh   (from the repo root, with recursa installed)
set -euo pipefail
cd "$(dirname "$0")"
rm -rf .recursa
export RECURSA_TOKEN_FILE="$PWD/.recursa/demo.token"
export RECURSA_PASSPHRASE="${RECURSA_PASSPHRASE:-demo-passphrase-123}"   # demo only; omit to be prompted

python generate.py pool --n 800 --out pool.jsonl
recursa init --reviewer alice
recursa data split pool.jsonl                 # train/dev open; gate/confirm sealed + encrypted
recursa auth add-reviewer alice

echo "== baseline on dev"; recursa eval --split dev
echo "== learning cycle";  recursa cycle
CAND=$(recursa review list | awk 'NR==1{print $1}')
recursa review show "$CAND"
HASH=$(recursa review show "$CAND" --json | python -c "import json,sys;print(json.load(sys.stdin)['packet_hash'])")

echo "== approve (human, authenticated, bound to packet ${HASH:0:16})"
recursa auth login alice
recursa approve "$CAND" --packet "${HASH:0:16}"
recursa versions
echo "== v2 on dev"; recursa eval --split dev

echo "== rollback in one command, then re-run the eval"
recursa rollback v1 --reason "demo rollback"
recursa eval --split dev --no-cache
recursa audit --verify
