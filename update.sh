#!/usr/bin/env bash
# Update an existing octobots install in place.
#
# Replaces ./octobots/ with the latest framework files. NEVER touches:
#     .env.octobots, .octobots/, .claude/, .mcp.json, .gitignore (except append)
#
# Drift protection: refuses to clobber hand-edits inside ./octobots/ unless
# --force is passed. Drift is detected by comparing the current ./octobots/
# tree against the SHA-256 manifest written at the previous install/update
# time (./octobots/.octobots-manifest).
#
# Usage (run from your project root):
#   curl -fsSL https://raw.githubusercontent.com/arozumenko/octobots/main/update.sh | bash
#   octobots/update.sh                          # use bundled copy
#   octobots/update.sh --branch develop         # different branch
#   octobots/update.sh --from /path/to/checkout # local source
#   octobots/update.sh --no-backup              # skip the .bak snapshot
#   octobots/update.sh --force                  # proceed despite drift
#   octobots/update.sh --dry-run                # report only, change nothing
#   octobots/update.sh --refresh-agents         # also re-init installed agents
#   octobots/update.sh --refresh-skills         # also re-add installed skills
#   octobots/update.sh --reconfigure-env        # also re-run .env.octobots prompts

set -euo pipefail

REPO="arozumenko/octobots"
BRANCH="main"
DEST="octobots"
ENV_FILE=".env.octobots"

FROM_LOCAL=""
NO_BACKUP=0
FORCE=0
DRY_RUN=0
REFRESH_AGENTS=0
REFRESH_SKILLS=0
RECONFIGURE_ENV=0

# ── Argument parsing ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)          BRANCH="$2"; shift 2 ;;
        --from)            FROM_LOCAL="$2"; shift 2 ;;
        --no-backup)       NO_BACKUP=1; shift ;;
        --force)           FORCE=1; shift ;;
        --dry-run)         DRY_RUN=1; shift ;;
        --refresh-agents)  REFRESH_AGENTS=1; shift ;;
        --refresh-skills)  REFRESH_SKILLS=1; shift ;;
        --reconfigure-env) RECONFIGURE_ENV=1; shift ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "Error: unknown flag '$1'. Run with --help." >&2
            exit 2 ;;
    esac
done

TMP_DIR=""
cleanup() { [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]] && rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# ── Helpers ──────────────────────────────────────────────────────────────────

note()   { printf '  %s\n' "$*"; }
ok()     { printf '  ✓ %s\n' "$*"; }
warn()   { printf '  ⚠  %s\n' "$*" >&2; }
fatal()  { printf '✗ %s\n' "$*" >&2; exit 1; }
hr()     { printf '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'; }

# Generate a SHA-256 manifest of every file under $1, written to $1/.octobots-manifest.
# Format is shasum -c compatible: lines starting with `#` are metadata and ignored
# by the verifier. The manifest path itself is excluded from its own contents.
generate_manifest() {
    local dir="$1" ref_label="$2" src_label="$3"
    local manifest="$dir/.octobots-manifest"
    {
        printf '# octobots install manifest — do not edit\n'
        printf '# source: %s\n' "$src_label"
        printf '# ref: %s\n'    "$ref_label"
        printf '# installed-at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$manifest"
    (cd "$dir" && find . -type f \
        ! -name '.octobots-manifest' \
        ! -path './.git/*' \
        -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 shasum -a 256) >> "$manifest"
}

# Verify the current ./$DEST/ tree against its recorded manifest.
# Echoes one line per drifted file, prefixed with FAILED|MISSING|UNTRACKED.
# Returns 0 if no drift, 1 if drift detected.
detect_drift() {
    local manifest="$DEST/.octobots-manifest"
    [[ -f "$manifest" ]] || { echo "NOMANIFEST"; return 1; }

    local drift=0

    # 1. shasum -c catches modified + missing files.
    #    Output format: "<file>: OK" or "<file>: FAILED" or "<file>: FAILED open or read"
    local tmp_out; tmp_out=$(mktemp)
    (cd "$DEST" && shasum -a 256 -c .octobots-manifest 2>&1) > "$tmp_out" || true
    while IFS= read -r line; do
        case "$line" in
            *": FAILED open or read")
                printf 'MISSING    %s\n' "${line%: FAILED open or read}"; drift=1 ;;
            *": FAILED")
                printf 'MODIFIED   %s\n' "${line%: FAILED}"; drift=1 ;;
        esac
    done < "$tmp_out"
    rm -f "$tmp_out"

    # 2. Files present in ./octobots/ but absent from the manifest = user-added.
    local manifest_files; manifest_files=$(mktemp)
    # shasum -a 256 format: "<64hex>  <path>". Strip the hash prefix.
    grep -v '^#' "$manifest" | sed 's/^[^ ]*  //' | LC_ALL=C sort > "$manifest_files"
    local current_files; current_files=$(mktemp)
    (cd "$DEST" && find . -type f \
        ! -name '.octobots-manifest' \
        ! -path './.git/*' \
        | LC_ALL=C sort) > "$current_files"
    while IFS= read -r f; do
        if ! grep -qxF "$f" "$manifest_files"; then
            printf 'UNTRACKED  %s\n' "$f"
            drift=1
        fi
    done < "$current_files"
    rm -f "$manifest_files" "$current_files"

    return $drift
}

# ── Sanity checks ────────────────────────────────────────────────────────────

[[ -d "./$DEST" ]] || fatal "./$DEST/ not found. Run install.sh first."
[[ -f "./$DEST/start.sh" ]] || fatal "./$DEST/ exists but doesn't look like an octobots install."

echo "Updating octobots in $(pwd)"
echo ""

# ── Drift check ──────────────────────────────────────────────────────────────

echo "Checking for local edits in ./$DEST/..."
drift_report=$(mktemp)
if detect_drift > "$drift_report"; then
    ok "no drift — clean install"
else
    if grep -q '^NOMANIFEST' "$drift_report"; then
        warn "no manifest from previous install — first update will write one"
        warn "skipping drift check; future updates will be guarded"
    else
        warn "drift detected in ./$DEST/:"
        sed 's/^/    /' "$drift_report" >&2
        if [[ $FORCE -eq 0 ]]; then
            rm -f "$drift_report"
            cat >&2 <<EOF

Refusing to overwrite local edits. Options:

  1. Copy the listed files out of ./$DEST/ before re-running.
  2. Run again with --force to proceed (the backup at octobots.bak.<ts>/
     will still let you recover them).
  3. Run with --dry-run to see what would change without touching anything.
EOF
            exit 1
        else
            warn "proceeding despite drift (--force)"
        fi
    fi
fi
rm -f "$drift_report"
echo ""

# ── Source: download or local ────────────────────────────────────────────────

if [[ -n "$FROM_LOCAL" ]]; then
    [[ -d "$FROM_LOCAL" && -f "$FROM_LOCAL/start.sh" ]] \
        || fatal "--from path '$FROM_LOCAL' is not an octobots checkout"
    SRC="$FROM_LOCAL"
    REF_LABEL="local:$FROM_LOCAL"
    SRC_LABEL="$FROM_LOCAL"
    echo "Source: local checkout at $FROM_LOCAL"
else
    TARBALL_URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
    TMP_DIR=$(mktemp -d)
    echo "Downloading $REPO@$BRANCH..."
    curl -fsSL "$TARBALL_URL" -o "$TMP_DIR/octobots.tar.gz"
    tar -xzf "$TMP_DIR/octobots.tar.gz" -C "$TMP_DIR"
    SRC="$TMP_DIR/octobots-$BRANCH"
    REF_LABEL="$BRANCH"
    SRC_LABEL="https://github.com/$REPO"
fi
echo ""

# ── Diff summary (what's about to change) ────────────────────────────────────

echo "Files that will change:"
diff_tmp=$(mktemp)
(diff -rq "./$DEST" "$SRC" 2>/dev/null || true) > "$diff_tmp"
changed=$(wc -l < "$diff_tmp" | tr -d ' ')
if [[ "$changed" == "0" ]]; then
    ok "framework already up to date"
else
    head -50 "$diff_tmp" | sed 's|^|    |'
    [[ "$changed" -gt 50 ]] && note "… and $((changed - 50)) more"
fi
rm -f "$diff_tmp"
echo ""

if [[ $DRY_RUN -eq 1 ]]; then
    note "--dry-run: stopping here. Nothing was changed."
    exit 0
fi

# ── Backup ───────────────────────────────────────────────────────────────────

if [[ $NO_BACKUP -eq 0 ]]; then
    BACKUP="$DEST.bak.$(date +%Y%m%d-%H%M%S)"
    echo "Backing up ./$DEST/ → ./$BACKUP/"
    cp -r "./$DEST" "./$BACKUP"
    ok "backup created"
    echo ""
fi

# ── Swap framework ──────────────────────────────────────────────────────────

echo "Replacing ./$DEST/..."
rm -rf "./$DEST"
cp -r "$SRC" "./$DEST"
generate_manifest "./$DEST" "$REF_LABEL" "$SRC_LABEL"
ok "framework updated"
echo ""

# ── Re-run idempotent setup ─────────────────────────────────────────────────

echo "Refreshing dependencies..."
if command -v pip3 &>/dev/null; then
    pip3 install -q -r "$DEST/requirements.txt" || warn "pip install failed"
elif command -v pip &>/dev/null; then
    pip install -q -r "$DEST/requirements.txt" || warn "pip install failed"
fi
ok "python deps"

if [[ -f "$DEST/scripts/apply-skill-deps.py" ]]; then
    DEST="$DEST" python3 "$DEST/scripts/apply-skill-deps.py" >/dev/null 2>&1 || warn "apply-skill-deps failed"
    ok "skill deps applied"
fi

if [[ -f "$DEST/scripts/init-project.sh" ]]; then
    bash "$DEST/scripts/init-project.sh" >/dev/null 2>&1 || warn "init-project failed"
    ok "runtime dirs refreshed"
fi
echo ""

# ── Optional refreshes ──────────────────────────────────────────────────────

if [[ $REFRESH_AGENTS -eq 1 ]]; then
    echo "Refreshing installed agents..."
    if [[ -d ".claude/agents" ]] && command -v npx &>/dev/null; then
        # Map each .claude/agents/<name>/ back to its source repo via agents.json.
        # Roles without a registry entry are assumed to be local-only and skipped.
        python3 - <<PY
import json, os, subprocess, sys
try:
    reg = json.load(open("$DEST/agents.json"))
except Exception:
    sys.exit(0)
by_id = {a["id"]: a["repo"] for a in reg.get("agents", [])}
for name in sorted(os.listdir(".claude/agents")):
    if name in by_id:
        repo = by_id[name]
        print(f"  → {name} ({repo})")
        subprocess.run(["npx", f"github:{repo}", "init", "--all"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
PY
        ok "agents refreshed"
    else
        warn "no .claude/agents/ or npx unavailable — skipped"
    fi
    echo ""
fi

if [[ $REFRESH_SKILLS -eq 1 ]]; then
    echo "Refreshing installed skills..."
    if [[ -d ".claude/skills" ]] && command -v npx &>/dev/null; then
        python3 - <<PY
import json, os, subprocess, sys
try:
    reg = json.load(open("$DEST/skills.json"))
except Exception:
    sys.exit(0)
by_id = {s["id"]: s["repo"] for s in reg.get("skills", [])}
for name in sorted(os.listdir(".claude/skills")):
    if name in by_id:
        repo = by_id[name]
        print(f"  → {name} ({repo})")
        subprocess.run(["npx", "skills", "add", repo, "--yes"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
PY
        ok "skills refreshed"
    else
        warn "no .claude/skills/ or npx unavailable — skipped"
    fi
    echo ""
fi

if [[ $RECONFIGURE_ENV -eq 1 ]]; then
    note "--reconfigure-env: re-running install.sh's interactive prompts..."
    note "(your existing .env.octobots values are shown as defaults — press Enter to keep)"
    # Delegate to install.sh's prompt block. Easiest is to source the new install.sh
    # from a marker, but install.sh isn't structured that way. For v1, point the
    # user at it explicitly.
    note "run:  bash $DEST/install.sh   then exit when prompts are done"
    echo ""
fi

# ── Done ────────────────────────────────────────────────────────────────────

hr
echo "  octobots updated"
hr
echo ""
echo "Source: $SRC_LABEL ($REF_LABEL)"
[[ $NO_BACKUP -eq 0 ]] && echo "Backup: ./${BACKUP:-octobots.bak.<skipped>}/"
echo ""
echo "Restart workers to pick up the new framework:"
echo "  tmux kill-session -t octobots 2>/dev/null; python3 $DEST/scripts/supervisor.py"
echo ""
