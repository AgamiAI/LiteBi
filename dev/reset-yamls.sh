#!/usr/bin/env bash
# Nuke the generated YAML artifacts for a profile so you can re-run
# /agami-connect end-to-end without losing credentials, reviewer config,
# hand-written docs, or the git audit trail.
#
# What gets nuked (default):
#   <artifacts_dir>/<profile>/datasource.yaml                   (model root)
#   <artifacts_dir>/<profile>/datasources/               (storage connections)
#   <artifacts_dir>/<profile>/subject_areas/             (tables/entities/metrics/relationships)
#   <artifacts_dir>/<profile>/prompt_examples/           (NL→SQL example library)
#   <artifacts_dir>/<profile>/agami.config.yaml          (if present)
#   <artifacts_dir>/<profile>/.snapshots/                (immutable past versions)
#   <artifacts_dir>/<profile>/.semantic_v2/              (migrate scratch, if present)
#
# What gets preserved (default):
#   <artifacts_dir>/local/credentials         DB connection details
#   <artifacts_dir>/local/.config             reviewer email, role, etc.
#   <artifacts_dir>/USER_MEMORY.md            cross-DB preferences
#   <artifacts_dir>/<profile>/datasource.md domain context the user wrote
#   <artifacts_dir>/<profile>/.git/           audit trail of past curator edits
#   <artifacts_dir>/<profile>/.legacy_backup/ legacy (v1) model backed up on upgrade
#   <artifacts_dir>/<profile>/curation_log.jsonl
#   <artifacts_dir>/<profile>/corrections.jsonl
#   <artifacts_dir>/local/review/                          rendered review dashboards
#   <artifacts_dir>/local/examples-validation/             rendered validation dashboards
#   <artifacts_dir>/local/model/                           rendered model-explorer dashboards
#   <artifacts_dir>/local/charts/                          rendered query charts
#
# Usage:
#   dev/reset-yamls.sh                                       # default profile, soft reset
#   dev/reset-yamls.sh acme                                # specific profile, soft reset
#   dev/reset-yamls.sh acme --hard                         # also drop datasource.md + .git/ + logs
#   dev/reset-yamls.sh acme --clean-renders                # also drop <artifacts_dir>/local/<kind>/acme/* (this profile only)
#   dev/reset-yamls.sh acme --clean-renders-all            # wipe legacy flat-layout files + every profile
#   dev/reset-yamls.sh acme --hard --clean-renders         # full per-profile wipe
#   dev/reset-yamls.sh acme --dry-run                      # show what would be deleted, do nothing
#
# Env:
#   AGAMI_ARTIFACTS_DIR (default: $HOME/agami-artifacts)

set -euo pipefail

PROFILE=""
HARD=0
DRY_RUN=0
CLEAN_RENDERS=0
CLEAN_RENDERS_ALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hard)               HARD=1; shift ;;
    --dry-run)            DRY_RUN=1; shift ;;
    --clean-renders)      CLEAN_RENDERS=1; shift ;;
    --clean-renders-all)  CLEAN_RENDERS=1; CLEAN_RENDERS_ALL=1; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "unknown flag: $1" >&2
      exit 2
      ;;
    *)
      if [[ -z "$PROFILE" ]]; then
        PROFILE="$1"
      else
        echo "unexpected positional arg: $1" >&2
        exit 2
      fi
      shift
      ;;
  esac
done

PROFILE="${PROFILE:-default}"
ARTIFACTS_DIR="${AGAMI_ARTIFACTS_DIR:-}"
if [ -z "$ARTIFACTS_DIR" ] && [ -f "$HOME/.config/agami/path" ]; then
  ARTIFACTS_DIR="$(tr -d '\n' < "$HOME/.config/agami/path")"
fi
ARTIFACTS_DIR="${ARTIFACTS_DIR:-$HOME/agami-artifacts}"
PROFILE_DIR="$ARTIFACTS_DIR/$PROFILE"

if [[ ! -d "$PROFILE_DIR" ]]; then
  echo "no profile dir at $PROFILE_DIR — nothing to nuke"
  exit 0
fi

action() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] would: $*"
  else
    eval "$@"
  fi
}

echo "Resetting YAMLs in $PROFILE_DIR (profile: $PROFILE)"
echo "  mode: $([[ $HARD -eq 1 ]] && echo 'hard (drops datasource.md, .git/, logs)' || echo 'soft (keeps datasource.md, .git/, logs)')"
if [[ $CLEAN_RENDERS_ALL -eq 1 ]]; then
  echo "  also cleaning <artifacts_dir>/local/{charts,review,examples-validation,model,exports}/ (ALL profiles + legacy)"
elif [[ $CLEAN_RENDERS -eq 1 ]]; then
  echo "  also cleaning <artifacts_dir>/local/{charts,review,examples-validation,model,exports}/$PROFILE/ (this profile only)"
fi
[[ $DRY_RUN -eq 1 ]] && echo "  dry-run: nothing will actually be deleted"
echo ""

# Snapshots are intentionally chmod 444 (immutable). Make writable first so
# rm can delete them — same as agami-connect's snapshot-cleanup contract.
if [[ -d "$PROFILE_DIR/.snapshots" ]]; then
  action "chmod -R u+w '$PROFILE_DIR/.snapshots'"
fi

# 1. Top-level YAML files (datasource.yaml, etc.)
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  action "rm -f '$f'"
done < <(find "$PROFILE_DIR" -maxdepth 1 -name "*.yaml" -o -name "*.yml" 2>/dev/null)

# 2. Model directories (datasources/, subject_areas/, prompt_examples/, and any
#    other generated subdir). Preserve .git / .snapshots / .rejected (handled
#    separately) and .legacy_backup (the one-time legacy-model backup — never regenerable).
for sub in "$PROFILE_DIR"/*/ "$PROFILE_DIR"/.*/; do
  [[ -d "$sub" ]] || continue
  bn="$(basename "$sub")"
  case "$bn" in
    .|..|.git|.snapshots|.rejected|.legacy_backup) continue ;;
  esac
  action "rm -rf '$sub'"
done

# 3. Snapshots directory (immutable copies of past introspect runs).
if [[ -d "$PROFILE_DIR/.snapshots" ]]; then
  action "rm -rf '$PROFILE_DIR/.snapshots'"
fi

# 4. Hard reset extras: datasource.md, the audit trail (.git/), and logs.
if [[ $HARD -eq 1 ]]; then
  action "rm -f '$PROFILE_DIR/datasource.md'"
  action "rm -rf '$PROFILE_DIR/.git'"
  action "rm -f '$PROFILE_DIR/curation_log.jsonl'"
  action "rm -f '$PROFILE_DIR/corrections.jsonl'"
fi

# 5. Optional: nuke accumulated render outputs under <artifacts_dir>/local/. These pile up
#    over time (each dashboard re-render writes a new timestamp). They are
#    regenerable from source, so safe to delete.
#
#    --clean-renders        wipes only this profile's renders:
#                             <artifacts_dir>/local/<kind>/<profile>/*
#    --clean-renders-all    additionally wipes legacy flat-layout files
#                           at <artifacts_dir>/local/<kind>/*.{html,csv} (predate the
#                           per-profile subdir change) AND every other
#                           profile's renders.
if [[ $CLEAN_RENDERS -eq 1 ]]; then
  for render_dir in charts review examples-validation model exports prune; do
    base="$ARTIFACTS_DIR/local/$render_dir"
    [[ -d "$base" ]] || continue

    if [[ $CLEAN_RENDERS_ALL -eq 1 ]]; then
      # Full wipe: every file and every per-profile subdir under <kind>/.
      while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        action "rm -rf '$f'"
      done < <(find "$base" -mindepth 1 -maxdepth 1 2>/dev/null)
    else
      # Profile-scoped wipe: only files inside <kind>/<profile>/.
      target="$base/$PROFILE"
      [[ -d "$target" ]] || continue
      while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        action "rm -f '$f'"
      done < <(find "$target" -mindepth 1 -maxdepth 1 2>/dev/null)
    fi
  done
fi

echo ""
echo "✓ Done."
echo ""
echo "Always preserved (this script never touches these regardless of flags):"
echo "  <artifacts_dir>/local/credentials  DB connection"
echo "  <artifacts_dir>/local/.config      reviewer email, role"
echo "  ~/agami-artifacts/USER_MEMORY.md  cross-DB preferences"
if [[ $CLEAN_RENDERS -eq 0 ]]; then
  echo "  <artifacts_dir>/local/review/, charts/, ...    rendered dashboards / charts (pass --clean-renders to nuke)"
fi
if [[ $HARD -eq 0 ]]; then
  echo ""
  echo "Preserved in soft mode (would be dropped on --hard):"
  [[ -f "$PROFILE_DIR/datasource.md" ]] && echo "  $PROFILE_DIR/datasource.md"
  [[ -d "$PROFILE_DIR/.git" ]] && echo "  $PROFILE_DIR/.git                       (audit trail)"
  [[ -f "$PROFILE_DIR/curation_log.jsonl" ]] && echo "  $PROFILE_DIR/curation_log.jsonl"
  [[ -f "$PROFILE_DIR/corrections.jsonl" ]] && echo "  $PROFILE_DIR/corrections.jsonl"
fi
echo ""
echo "Now run /agami-connect in Claude Code to regenerate the YAMLs."
[[ -d "$PROFILE_DIR/.git" ]] && \
  echo "(Or 'git -C $PROFILE_DIR reset --hard HEAD' to undo this reset.)"
