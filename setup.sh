#!/usr/bin/env bash
# courtside interactive setup + demo launcher.
#
# One command to go from a fresh clone to a running demo:
#   ./setup.sh
#
# It detects your platform and RAM, installs prerequisites (ffmpeg, a venv, the
# package with the right extras), helps you pick + pre-fetch a model, and then
# launches either the instant sample demo (no model needed) or a live run on
# your own footage. Safe to re-run; every step is idempotent.

set -euo pipefail

# ---------- pretty output ----------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YLW=$'\033[33m'; BLU=$'\033[34m'; CYN=$'\033[36m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YLW=""; BLU=""; CYN=""; RST=""
fi
say()  { printf "%s\n" "$*"; }
step() { printf "\n${BOLD}${BLU}==>${RST} ${BOLD}%s${RST}\n" "$*"; }
ok()   { printf "${GRN}  ok${RST} %s\n" "$*"; }
warn() { printf "${YLW}  ! ${RST}%s\n" "$*"; }
die()  { printf "${RED}error:${RST} %s\n" "$*" >&2; exit 1; }

ASSUME_YES="${COURTSIDE_ASSUME_YES:-0}"

ask() { # ask "question" "default(Y/N)"  -> returns 0 for yes
  local q="$1" def="${2:-Y}" ans
  # In assume-yes mode, accept each prompt's own default (so the expensive
  # prefetch, which defaults to N, is not blindly triggered).
  if [ "$ASSUME_YES" = "1" ]; then [ "$def" != "N" ]; return; fi
  local hint="[Y/n]"; [ "$def" = "N" ] && hint="[y/N]"
  read -r -p "$(printf "${CYN}?${RST} %s %s " "$q" "$hint")" ans || ans=""
  ans="${ans:-$def}"
  [[ "$ans" =~ ^[Yy] ]]
}

ask_value() { # ask_value "question" "default" -> echoes chosen value
  local q="$1" def="${2:-}" ans
  if [ "$ASSUME_YES" = "1" ]; then echo "$def"; return; fi
  read -r -p "$(printf "${CYN}?${RST} %s ${DIM}[%s]${RST} " "$q" "$def")" ans || ans=""
  echo "${ans:-$def}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

say "${BOLD}courtside${RST} - local tennis video analysis (Apple Silicon / MLX)"
say "${DIM}segment rallies -> sample frames -> VLM stroke JSON -> coaching report${RST}"

# ---------- 1. platform + memory ----------
step "Detecting platform"
OS="$(uname -s)"; ARCH="$(uname -m)"
APPLE_SILICON=0
RAM_GB=0
case "$OS" in
  Darwin)
    [ "$ARCH" = "arm64" ] && APPLE_SILICON=1
    RAM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    RAM_GB=$(( RAM_BYTES / 1024 / 1024 / 1024 ))
    ok "macOS on ${ARCH} (${RAM_GB} GB RAM)"
    [ "$APPLE_SILICON" = 1 ] || warn "Intel Mac: local MLX inference is unavailable; use the sample demo or server mode."
    ;;
  Linux)
    RAM_KB="$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    RAM_GB=$(( RAM_KB / 1024 / 1024 ))
    ok "Linux on ${ARCH} (${RAM_GB} GB RAM)"
    warn "Local model inference needs Apple Silicon + MLX. On Linux you can still:"
    say  "    - open the bundled sample report (no model), or"
    say  "    - run against an OpenAI-compatible server with --server-url."
    ;;
  *) warn "Unrecognized OS '$OS' - proceeding best-effort." ;;
esac

# ---------- 2. python ----------
step "Checking Python (>= 3.11)"
PYTHON=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    major="${ver%%.*}"; minor="${ver##*.}"
    if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then PYTHON="$cand"; break; fi
  fi
done
if [ -z "$PYTHON" ]; then
  if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1 && ask "Install python@3.12 via Homebrew?"; then
    brew install python@3.12 && PYTHON=python3.12
  else
    die "need Python >= 3.11. Install it (e.g. 'brew install python@3.12') and re-run."
  fi
fi
ok "using $($PYTHON --version)"

# ---------- 3. ffmpeg ----------
step "Checking ffmpeg + ffprobe"
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  ok "ffmpeg + ffprobe on PATH"
else
  warn "ffmpeg/ffprobe not found."
  if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    ask "Install ffmpeg via Homebrew?" && brew install ffmpeg || die "ffmpeg is required."
  elif [ "$OS" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
    ask "Install ffmpeg via apt (sudo)?" && sudo apt-get update && sudo apt-get install -y ffmpeg || die "ffmpeg is required."
  else
    die "install ffmpeg manually (https://ffmpeg.org) and re-run."
  fi
fi

# ---------- 4. venv + package ----------
step "Creating virtual environment (.venv)"
if [ ! -d .venv ]; then "$PYTHON" -m venv .venv; ok "created .venv"; else ok ".venv already exists"; fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip

EXTRAS="dev,server"
if [ "$APPLE_SILICON" = 1 ]; then EXTRAS="local,dev,server"; fi
step "Installing courtside  ${DIM}(extras: ${EXTRAS})${RST}"
if pip install --quiet -e ".[${EXTRAS}]"; then
  ok "installed"
else
  warn "install with extras failed (mlx-vlm may be unavailable here); installing core only."
  pip install --quiet -e ".[dev,server]"
fi

# quick smoke test of the model-free core
if python -m pytest -q >/dev/null 2>&1; then ok "self-test passed"; else warn "self-test skipped/failed (non-fatal)"; fi

# ---------- 5. model selection (local only) ----------
MODEL_KEY="qwen3-vl-32b"
if [ "$APPLE_SILICON" = 1 ]; then
  step "Choosing a model for ${RAM_GB} GB of unified memory"
  # recommend by RAM headroom
  if   [ "$RAM_GB" -ge 96 ]; then REC="qwen3-vl-32b";     REC_NOTE="best quality/headroom";
  elif [ "$RAM_GB" -ge 40 ]; then REC="qwen3-vl-30b-a3b"; REC_NOTE="MoE, fastest decode";
  else                           REC="qwen3-vl-8b";       REC_NOTE="fits smaller machines"; fi
  say "  ${DIM}key                  ~weights  notes${RST}"
  say "  1) qwen3-vl-32b        ~35 GB   default dense 32B, best quality"
  say "  2) qwen3-vl-30b-a3b    ~32 GB   MoE, fastest decode - great for live demos"
  say "  3) qwen3-vl-8b          ~9 GB   smoke tests / low-RAM machines"
  say "  4) qwen3-vl-32b-thinking ~35 GB harder tactical reasoning, slower"
  say "  ${DIM}recommended for this machine: ${REC} (${REC_NOTE})${RST}"
  choice="$(ask_value "Pick 1-4" "$(case $REC in qwen3-vl-32b)echo 1;;qwen3-vl-30b-a3b)echo 2;;*)echo 3;;esac)")"
  case "$choice" in
    1) MODEL_KEY="qwen3-vl-32b";;
    2) MODEL_KEY="qwen3-vl-30b-a3b";;
    3) MODEL_KEY="qwen3-vl-8b";;
    4) MODEL_KEY="qwen3-vl-32b-thinking";;
    *) MODEL_KEY="$REC";;
  esac
  ok "selected ${MODEL_KEY}"

  if ask "Pre-fetch ${MODEL_KEY} weights now? (needed for an offline/airplane-mode demo)" "N"; then
    step "Downloading weights (this can be tens of GB the first time)"
    courtside --prefetch "$MODEL_KEY" || warn "prefetch failed - you can retry later."
  fi
fi

# ---------- 6. launch a demo ----------
open_report() { # open_report <path>
  local p="$1"
  case "$OS" in
    Darwin) open "$p" >/dev/null 2>&1 || true ;;
    Linux)  xdg-open "$p" >/dev/null 2>&1 || true ;;
  esac
  ok "report: $p"
}

step "Launch a demo"
say "  1) Instant sample demo    ${DIM}- open the bundled report, no model, always works${RST}"
say "  2) Analyze my own video   ${DIM}- run the full pipeline now${RST}"
say "  3) Finish                 ${DIM}- just print the commands${RST}"
demo="$(ask_value "Pick 1-3" "1")"

case "$demo" in
  1)
    SAMPLE="examples/demo_session/report.html"
    if [ -f "$SAMPLE" ]; then
      # rebuild from cached data so it reflects the installed code, then open it
      courtside --from-dir examples/demo_session >/dev/null 2>&1 || true
      open_report "$SAMPLE"
      say "  ${DIM}This is synthetic footage illustrating the output. See examples/README.md.${RST}"
    else
      warn "sample not found at $SAMPLE"
    fi
    ;;
  2)
    vid="$(ask_value "Path to a tennis video (mp4/mov)" "")"
    [ -n "$vid" ] || { warn "no path given - skipping"; vid=""; }
    if [ -n "$vid" ] && [ -f "$vid" ]; then
      quick=""
      ask "Quick pass (first 3 clips) for a fast first result?" && quick="--max-clips 3"
      stream=""
      ask "Stream model tokens live during analysis?" && stream="--stream"
      step "Running: courtside $vid --model $MODEL_KEY $quick $stream"
      if [ "$APPLE_SILICON" = 1 ]; then
        courtside "$vid" --model "$MODEL_KEY" $quick $stream || warn "run failed - see output above."
      else
        warn "Not Apple Silicon: local inference unavailable. Start an OpenAI-compatible server, then:"
        say  "    courtside \"$vid\" --server-url http://localhost:8080/v1 --server-model <name>"
      fi
      out="${vid%.*}_courtside/report.html"
      [ -f "$out" ] && open_report "$out"
    elif [ -n "$vid" ]; then
      warn "file not found: $vid"
    fi
    ;;
  *) : ;;
esac

# ---------- done ----------
step "Done"
say "Activate the environment in new shells with:  ${BOLD}source .venv/bin/activate${RST}"
say "Common commands:"
say "  ${DIM}# instant demo, no model${RST}"
say "  courtside --from-dir examples/demo_session"
say "  ${DIM}# analyze footage${RST}"
say "  courtside match.mp4 --model ${MODEL_KEY}"
say "  ${DIM}# fast smoke test${RST}"
say "  courtside match.mp4 --model qwen3-vl-8b --max-clips 3"
say "  ${DIM}# provably offline (weights pre-cached)${RST}"
say "  courtside match.mp4 --offline"
say "  ${DIM}# re-render a previous run's report (no model)${RST}"
say "  courtside --from-dir match_courtside"
