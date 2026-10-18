#!/bin/bash
#
# Pirdfy installer and updater for Raspberry Pi 5.
#
#   Install:  curl -fsSL https://raw.githubusercontent.com/andjar/pirdfy/main/install.sh | sudo bash
#   Update:   sudo pirdfy update          (or the button in Settings)
#
# Safe to run again at any time: it only does what is missing, never overwrites
# your settings or photos, and an update that fails its health check is rolled
# back automatically.
#
# Commands: install (default) | update | doctor | model | uninstall [--purge]
# Options:  --yes            no questions (used by the web updater)
#           --channel C      stable (latest release, default) or edge (main branch)
#           --ref REF        install a specific tag, branch or commit
#           --repo URL       install from another git URL or a local checkout
#           --no-start       do not start the service
#
# Everything is inside functions and `main "$@"` runs last, so bash has read the
# whole file before anything executes: required for `curl | bash`, and for
# updating the very file that is running.

set -Eeuo pipefail

# Paths can be overridden for testing only.
APP=${PIRDFY_APP:-/opt/pirdfy}
ETC=${PIRDFY_ETC:-/etc/pirdfy}
DATA=${PIRDFY_DATA:-/var/lib/pirdfy}
SVC_USER=pirdfy
UNIT=pirdfy
REPO_URL_DEFAULT=https://github.com/andjar/pirdfy.git
MODEL=yolov8n
MODEL_URL=https://github.com/andjar/pirdfy/releases/download/models-v1
STATUS_FILE=$DATA/update/status.json
LOCK=/run/pirdfy-install.lock
HEALTH_TIMEOUT=${PIRDFY_HEALTH_TIMEOUT:-120}

# ---------------------------------------------------------------- output
if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B='' G='' Y='' R='' N=''; fi
step() { CURRENT_STEP="$*"; echo "${B}==>${N} $*"; status_update running "$*"; }
info() { echo "    $*"; }
ok()   { echo "${G} ✓${N} $*"; }
warn() { echo "${Y} !${N} $*" >&2; }
die()  { echo "${R} ✗ $*${N}" >&2; status_update failed "$*"; exit 1; }

# ---------------------------------------------------------------- state
CMD=install
YES=0
CHANNEL=""
REF=""
REPO_URL=""
NO_START=0
PURGE=0
FROM_REQUEST=0
PREVIOUS=""
GENERATED_PASSWORD=""
STATUS_ENABLED=0
APPLYING=0
DB_BACKUP=""
CURRENT_STEP=""

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      install|update|doctor|model|uninstall|__apply) CMD=$1 ;;
      --yes|-y) YES=1 ;;
      --channel) CHANNEL=${2:?}; shift ;;
      --ref) REF=${2:?}; shift ;;
      --repo) REPO_URL=${2:?}; shift ;;
      --no-start) NO_START=1 ;;
      --purge) PURGE=1 ;;
      --from-request) FROM_REQUEST=1 ;;
      --previous) PREVIOUS=${2:?}; shift ;;
      -h|--help) sed -n '2,20p' "$0" 2>/dev/null || true; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
    shift
  done
}

# A terminal for questions, even under `curl | sudo bash` (stdin is the script).
have_tty() { [[ $YES -eq 0 ]] && { true </dev/tty; } 2>/dev/null; }
ask() { # ask "Question" default(y/n); --yes answers yes
  local reply def=$2
  [[ $YES -eq 1 ]] && return 0
  have_tty || { [[ $def == y ]]; return; }
  read -r -p "$1 [$( [[ $def == y ]] && echo Y/n || echo y/N )] " reply </dev/tty || reply=""
  reply=${reply:-$def}
  [[ $reply =~ ^[Yy] ]]
}

status_update() { # state message
  [[ $STATUS_ENABLED -eq 1 ]] || return 0
  mkdir -p "$DATA/update"
  local now; now=$(date +%s)
  local msg=${2//\\/\\\\}; msg=${msg//\"/\\\"}
  printf '{"state":"%s","message":"%s","from":"%s","to":"%s","started_at":%s,"finished_at":%s}\n' \
    "$1" "$msg" "${PREVIOUS:-}" "$(git -C "$APP" describe --tags --always 2>/dev/null || true)" \
    "${STARTED_AT:-$now}" "$( [[ $1 == running ]] && echo null || echo "$now")" > "$STATUS_FILE.tmp" || return 0
  chmod 644 "$STATUS_FILE.tmp"; mv -f "$STATUS_FILE.tmp" "$STATUS_FILE"
}

on_error() {
  local code=$? line=${1:-?}
  [[ -n ${ERROR_REPORTED:-} || $BASH_SUBSHELL -gt 0 ]] && return 0
  ERROR_REPORTED=1
  echo "${R} ✗ Failed at line $line (exit $code).${N} Run 'sudo pirdfy doctor' or see 'pirdfy logs -n 100'." >&2
  status_update failed "Failed at step: ${CURRENT_STEP:-unknown}"
}

need_root() { [[ $EUID -eq 0 ]] || die "Run as root: sudo bash install.sh $CMD"; }

lock() {
  exec 9>"$LOCK"
  flock -n 9 || die "Another Pirdfy install or update is running."
}

# ---------------------------------------------------------------- checks
check_system() {
  step "Checking the system"
  local model=""
  [[ -r /proc/device-tree/model ]] && model=$(tr -d '\0' </proc/device-tree/model)
  if [[ $model == *"Raspberry Pi 5"* || -n ${PIRDFY_SKIP_HW_CHECK:-} ]]; then ok "${model:-hardware check skipped}"
  else
    warn "Pirdfy is built for the Raspberry Pi 5 (found: ${model:-unknown})."
    ask "Continue anyway?" n || die "Stopped."
  fi
  . /etc/os-release
  case "${VERSION_CODENAME:-}" in
    bookworm|trixie) ok "$PRETTY_NAME" ;;
    *) warn "Untested OS: ${PRETTY_NAME:-unknown}. Raspberry Pi OS Bookworm or Trixie is recommended." ;;
  esac
  [[ $(uname -m) == aarch64 ]] || warn "64-bit Raspberry Pi OS is required for the detector (found $(uname -m))."
  local free_mb; free_mb=$(df -Pm /var | awk 'NR==2{print $4}')
  (( free_mb > 1500 )) || die "At least 1.5 GB of free disk space is needed (have ${free_mb} MB)."
}

check_python() {
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or newer is required (Raspberry Pi OS Bookworm or later)."
}

# ---------------------------------------------------------------- packages
APT_PACKAGES=(git ca-certificates curl python3 python3-venv python3-picamera2 python3-libcamera
              python3-numpy python3-opencv python3-pil python3-av ffmpeg rpicam-apps sqlite3)

install_packages() {
  step "Installing system packages"
  local missing=()
  for p in "${APT_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || missing+=("$p")
  done
  if [[ ${#missing[@]} -eq 0 ]]; then ok "All present"; return; fi
  info "Installing: ${missing[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # python3-av and rpicam-apps have older names on some images; don't fail on those two.
  apt-get install -y -qq --no-install-recommends "${missing[@]}" || {
    warn "Retrying without optional packages"
    local core=()
    for p in "${missing[@]}"; do [[ $p == python3-av || $p == rpicam-apps ]] || core+=("$p"); done
    apt-get install -y -qq --no-install-recommends "${core[@]}"
  }
  ok "System packages installed"
}

# ---------------------------------------------------------------- user & dirs
create_user() {
  if ! id "$SVC_USER" &>/dev/null; then
    useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    ok "Created system user $SVC_USER"
  fi
  local g
  for g in video render; do
    if getent group "$g" >/dev/null; then usermod -aG "$g" "$SVC_USER"; fi
  done
}

create_dirs() {
  install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$DATA" "$DATA/models" "$DATA/update" "$DATA/backups"
  # The web UI saves settings here, so the service may write this directory.
  install -d -m 0770 -o root -g "$SVC_USER" "$ETC"
  if [[ ! -f $ETC/config.yaml ]]; then
    cat > "$ETC/config.yaml" <<'EOF'
# Pirdfy settings. Only values that differ from the defaults belong here.
# All options with explanations: /opt/pirdfy/config/config.example.yaml
# Most settings can also be changed in the web interface (Settings).
{}
EOF
    ok "Created $ETC/config.yaml"
  fi
  chown root:"$SVC_USER" "$ETC/config.yaml"; chmod 0660 "$ETC/config.yaml"
}

# ---------------------------------------------------------------- legacy v1
migrate_v1() {
  [[ -f $APP/src/main.py ]] || return 0
  step "Moving the old Pirdfy (v1) out of the way"
  systemctl disable --now "$UNIT" 2>/dev/null || true
  local backup
  backup="/opt/pirdfy-v1-backup-$(date +%Y%m%d-%H%M%S)"
  mv "$APP" "$backup"
  rm -f /etc/udev/rules.d/99-camera.rules
  [[ -L /usr/local/bin/pirdfy ]] || rm -f /usr/local/bin/pirdfy
  ok "Old installation kept in $backup (photos in $backup/data). Delete it when you no longer need it."
}

# ---------------------------------------------------------------- code
resolve_ref() { # prints the commit/tag to check out
  if [[ -n $REF ]]; then echo "$REF"; return; fi
  if [[ ${CHANNEL:-stable} == stable ]]; then
    local tag
    tag=$(git -C "$APP" tag -l 'v*' --sort=-v:refname | head -n1)
    if [[ -n $tag ]]; then echo "$tag"; return; fi
  fi
  echo "origin/main"
}

fetch_code() {
  step "Getting Pirdfy"
  local url=${REPO_URL:-$REPO_URL_DEFAULT}
  git config --global --get-all safe.directory 2>/dev/null | grep -qx "$APP" || git config --global --add safe.directory "$APP"
  if [[ -d $APP/.git ]]; then
    if [[ -n $REPO_URL ]]; then git -C "$APP" remote set-url origin "$url"; fi
    git -C "$APP" fetch --quiet --tags --force --prune origin || die "Could not reach $url. Are you online?"
  else
    rm -rf "$APP.new"
    git clone --quiet "$url" "$APP.new" || die "Could not download $url. Are you online?"
    [[ -e $APP ]] && rm -rf "$APP"
    mv "$APP.new" "$APP"
  fi
  local target; target=$(resolve_ref)
  git -C "$APP" -c advice.detachedHead=false checkout --quiet --force "$target"
  git -C "$APP" clean -fdq -e venv -e venv.prev
  chown -R root:root "$APP"
  chmod -R go-w "$APP"
  ok "Version $(cat "$APP/VERSION") ($(git -C "$APP" describe --tags --always))"
}

# ---------------------------------------------------------------- python
venv_hash() { cat "$APP/requirements.txt" <(python3 -V 2>&1) | sha256sum | cut -c1-16; }

setup_venv() {
  step "Setting up Python packages"
  local want; want=$(venv_hash)
  if [[ -f $APP/venv/.pirdfy-hash && $(cat "$APP/venv/.pirdfy-hash") == "$want" ]]; then
    ok "Up to date"; return
  fi
  rm -rf "$APP/venv.prev"
  [[ -d $APP/venv ]] && mv "$APP/venv" "$APP/venv.prev"
  # System site packages: picamera2, libcamera, numpy and OpenCV come from apt.
  python3 -m venv --system-site-packages "$APP/venv"
  "$APP/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --no-deps \
    -r "$APP/requirements.txt" || die "Installing Python packages failed (see above)."
  "$APP/venv/bin/python" - <<'EOF' || die "Python environment check failed."
import importlib, sys
mods = ["fastapi", "uvicorn", "onnxruntime", "cv2", "numpy", "PIL", "yaml", "apprise", "psutil"]
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        bad.append(f"{m}: {e}")
try:
    import picamera2  # noqa: F401
except Exception as e:  # noqa: BLE001
    print(f"warning: picamera2 not importable: {e}", file=sys.stderr)
if bad:
    print("\n".join(bad), file=sys.stderr)
    sys.exit(1)
EOF
  echo "$want" > "$APP/venv/.pirdfy-hash"
  ok "Python packages installed"
}

# ---------------------------------------------------------------- model
model_ok() { [[ -s $DATA/models/$MODEL.onnx ]]; }

fetch_model() {
  model_ok && [[ $CMD != model ]] && return 0
  step "Getting the bird detection model"
  local tmp; tmp=$(mktemp -d)
  if curl -fsSL --retry 3 -o "$tmp/m.onnx" "$MODEL_URL/$MODEL.onnx" \
     && curl -fsSL --retry 3 -o "$tmp/m.sha256" "$MODEL_URL/$MODEL.onnx.sha256" \
     && [[ $(sha256sum "$tmp/m.onnx" | cut -d' ' -f1) == $(cut -d' ' -f1 "$tmp/m.sha256") ]]; then
    install -m 0644 -o "$SVC_USER" -g "$SVC_USER" "$tmp/m.onnx" "$DATA/models/$MODEL.onnx"
    rm -rf "$tmp"; ok "Downloaded $MODEL.onnx"; return
  fi
  rm -rf "$tmp"
  info "No ready-made model available; building it on the Pi (one-time, about 10 minutes)."
  export_model
}

export_model() {
  local work=/var/tmp/pirdfy-export
  rm -rf "$work"; mkdir -p "$work"
  python3 -m venv "$work/venv"
  "$work/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir ultralytics onnx onnxslim \
    || die "Could not install the model export tools."
  (cd "$work" && "$work/venv/bin/python" - <<EOF) || die "Model export failed."
from ultralytics import YOLO
YOLO("$MODEL.pt").export(format="onnx", imgsz=(384, 640), opset=17, simplify=True, dynamic=False)
EOF
  install -m 0644 -o "$SVC_USER" -g "$SVC_USER" "$work/$MODEL.onnx" "$DATA/models/$MODEL.onnx"
  rm -rf "$work"
  ok "Built $MODEL.onnx"
}

# ---------------------------------------------------------------- password
pirdfy_py() {
  env PYTHONPATH="$APP" PIRDFY_DATA_DIR="$DATA" PIRDFY_CONFIG="$ETC/config.yaml" \
    runuser -u "$SVC_USER" -- "$APP/venv/bin/python" -m pirdfy "$@"
}

ensure_password() {
  if [[ -f $DATA/auth.json ]] && grep -q '"password"' "$DATA/auth.json"; then return; fi
  step "Setting the dashboard password"
  local pw="" pw2=""
  if have_tty; then
    while true; do
      read -r -s -p "    Choose a password for the dashboard (8+ characters, empty = generate one): " pw </dev/tty; echo
      [[ -z $pw ]] && break
      (( ${#pw} >= 8 )) || { warn "Too short."; continue; }
      read -r -s -p "    Repeat it: " pw2 </dev/tty; echo
      [[ $pw == "$pw2" ]] && break
      warn "The passwords do not match."
    done
  fi
  if [[ -z $pw ]]; then
    pw=$(python3 -c 'import secrets,string; a=string.ascii_lowercase+string.digits; print("-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(3)))')
    GENERATED_PASSWORD=$pw
  fi
  printf '%s\n' "$pw" | pirdfy_py passwd --stdin >/dev/null
  ok "Password set (change it any time in Settings or with 'pirdfy passwd')"
}

# ---------------------------------------------------------------- systemd
install_units() {
  step "Installing the service"
  local changed=0 f
  for f in pirdfy.service pirdfy-update.service pirdfy-update.path; do
    if ! cmp -s "$APP/deploy/$f" "/etc/systemd/system/$f"; then
      install -m 0644 "$APP/deploy/$f" "/etc/systemd/system/$f"; changed=1
    fi
  done
  ln -sfn "$APP/deploy/pirdfy-cli" /usr/local/bin/pirdfy
  chmod 0755 "$APP/deploy/pirdfy-cli"
  if (( changed )); then systemctl daemon-reload; fi
  systemctl enable --quiet "$UNIT" pirdfy-update.path
  systemctl start pirdfy-update.path
  ok "Service installed ('pirdfy help' lists the commands)"
}

web_port() { pirdfy_py get web.port 2>/dev/null || echo 8080; }

wait_healthy() {
  local port; port=$(web_port)
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while (( SECONDS < deadline )); do
    if curl -fsS -m 3 -k "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 \
       || curl -fsS -m 3 -k "https://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
      return 0
    fi
    systemctl is-failed --quiet "$UNIT" && return 1
    sleep 2
  done
  return 1
}

restart_service() {
  [[ $NO_START -eq 1 ]] && { info "Not starting (--no-start)"; return 0; }
  step "Starting Pirdfy"
  systemctl restart "$UNIT"
  if wait_healthy; then ok "Pirdfy is running"; return 0; fi
  return 1
}

# ---------------------------------------------------------------- database backup
backup_db() {
  [[ -f $DATA/pirdfy.db ]] || return 0
  local dest
  dest="$DATA/backups/pirdfy-$(date +%Y%m%d-%H%M%S).db"
  sqlite3 "$DATA/pirdfy.db" ".backup '$dest'" 2>/dev/null || cp "$DATA/pirdfy.db" "$dest"
  chown "$SVC_USER:$SVC_USER" "$dest"
  ls -1t "$DATA"/backups/pirdfy-*.db 2>/dev/null | tail -n +4 | xargs -r rm -f
  echo "$dest"
}

# ---------------------------------------------------------------- commands
do_install() {
  need_root; lock
  echo "${B}Pirdfy installer${N}"
  check_system
  install_packages
  check_python
  create_user
  migrate_v1
  create_dirs
  fetch_code
  setup_venv
  fetch_model
  ensure_password
  install_units
  if restart_service; then
    rm -rf "$APP/venv.prev"
  else
    warn "Pirdfy did not report healthy within ${HEALTH_TIMEOUT}s. Check: pirdfy logs -n 100"
  fi
  summary
}

do_update() {
  need_root; lock
  STATUS_ENABLED=1; STARTED_AT=$(date +%s)
  [[ $FROM_REQUEST -eq 1 ]] && rm -f "$DATA/update/request"
  [[ -d $APP/.git ]] || die "Pirdfy is not installed in $APP. Run the installer first."
  PREVIOUS=$(git -C "$APP" rev-parse HEAD)
  [[ -z $CHANNEL ]] && CHANNEL=$(pirdfy_py get updates.channel 2>/dev/null || echo stable)
  step "Checking for updates ($CHANNEL)"
  git -C "$APP" fetch --quiet --tags --force --prune origin || die "Could not reach GitHub. Are you online?"
  local target; target=$(resolve_ref)
  if [[ $(git -C "$APP" rev-parse "$target^{commit}") == "$PREVIOUS" ]]; then
    ok "Already up to date ($(git -C "$APP" describe --tags --always))"
    status_update success "Already up to date"
    return 0
  fi
  git -C "$APP" -c advice.detachedHead=false checkout --quiet --force "$target"
  chown -R root:root "$APP"
  # Continue with the *new* installer, so new steps and fixes apply immediately.
  local extra=()
  [[ $NO_START -eq 1 ]] && extra+=(--no-start)
  trap - EXIT
  exec bash "$APP/install.sh" __apply --previous "$PREVIOUS" --yes "${extra[@]}"
}

do_apply() { # second half of an update, run by the new version of this script
  need_root
  # The install lock (fd 9) is inherited from the first half of the update.
  STATUS_ENABLED=1; STARTED_AT=$(date +%s)
  DB_BACKUP=$(backup_db)
  APPLYING=1   # from here on, any failure rolls back (see on_exit)
  install_packages
  create_dirs
  setup_venv
  install_units
  restart_service || die "The new version did not start correctly"
  APPLYING=0
  rm -rf "$APP/venv.prev"
  local now; now=$(git -C "$APP" describe --tags --always)
  ok "Updated to $now"
  status_update success "Updated to $now"
}

on_exit() {
  local code=$?
  if [[ $APPLYING -eq 1 && $code -ne 0 ]]; then
    APPLYING=0
    rollback "$DB_BACKUP"
    local back; back=$(git -C "$APP" describe --tags --always)
    echo "${R} ✗ Update failed; rolled back to $back.${N} See 'pirdfy logs -n 200'." >&2
    status_update rolled_back "Update failed at '${CURRENT_STEP}'; rolled back to $back"
  fi
}

rollback() {
  warn "Rolling back to ${PREVIOUS:0:7}"
  set +e
  systemctl stop "$UNIT"
  git -C "$APP" -c advice.detachedHead=false checkout --quiet --force "$PREVIOUS"
  if [[ -d $APP/venv.prev ]]; then rm -rf "$APP/venv"; mv "$APP/venv.prev" "$APP/venv"; fi
  [[ -n ${1:-} && -f $1 ]] && install -m 0640 -o "$SVC_USER" -g "$SVC_USER" "$1" "$DATA/pirdfy.db" \
    && rm -f "$DATA/pirdfy.db-wal" "$DATA/pirdfy.db-shm"
  local f
  for f in pirdfy.service pirdfy-update.service pirdfy-update.path; do
    install -m 0644 "$APP/deploy/$f" "/etc/systemd/system/$f"
  done
  systemctl daemon-reload
  systemctl start "$UNIT"
  set -e
}

do_model() { need_root; CMD=model; rm -f "$DATA/models/$MODEL.onnx.bak"
  [[ -f $DATA/models/$MODEL.onnx ]] && mv "$DATA/models/$MODEL.onnx" "$DATA/models/$MODEL.onnx.bak"
  fetch_model || { mv "$DATA/models/$MODEL.onnx.bak" "$DATA/models/$MODEL.onnx" 2>/dev/null; exit 1; }
  rm -f "$DATA/models/$MODEL.onnx.bak"; systemctl restart "$UNIT"; }

do_doctor() {
  local bad=0
  check() { if eval "$2" &>/dev/null; then ok "$1"; else warn "$1: $3"; bad=1; fi; }
  echo "${B}Pirdfy doctor${N}"
  check "Installed in $APP" "[[ -d $APP/.git ]]" "not installed; run the installer"
  check "Python environment" "[[ -x $APP/venv/bin/python ]]" "missing; run: sudo bash $APP/install.sh"
  check "Configuration valid" "pirdfy_py check-config" "run 'pirdfy config' to fix"
  check "Detection model present" "model_ok" "run: sudo pirdfy model"
  check "Dashboard password set" "grep -q password $DATA/auth.json" "run: sudo pirdfy passwd"
  check "Service running" "systemctl is-active --quiet $UNIT" "see: pirdfy logs -n 100"
  check "Web interface answers" "curl -fsS -m 5 -k http://127.0.0.1:$(web_port)/healthz || curl -fsS -m 5 -k https://127.0.0.1:$(web_port)/healthz" "see: pirdfy logs -n 100"
  if command -v rpicam-hello &>/dev/null; then
    check "Camera connected" "rpicam-hello --list-cameras 2>&1 | grep -q '^[0-9] :'" "check the ribbon cable, then reboot"
  fi
  local health; health=$(curl -fsS -m 5 "http://127.0.0.1:$(web_port)/healthz" 2>/dev/null || true)
  [[ -n $health ]] && info "Health: $health"
  if command -v vcgencmd &>/dev/null; then
    local t; t=$(vcgencmd get_throttled | cut -d= -f2)
    [[ $t == 0x0 ]] && ok "Power supply OK" || warn "Power or temperature problems since boot ($t). Use the official 27 W supply and a cooler."
  fi
  local free_mb; free_mb=$(df -Pm "$DATA" | awk 'NR==2{print $4}')
  (( free_mb > 2048 )) && ok "Free space: $((free_mb / 1024)) GB" || warn "Low free space: ${free_mb} MB"
  return $bad
}

do_uninstall() {
  need_root
  ask "Remove Pirdfy$( [[ $PURGE -eq 1 ]] && echo ', including ALL photos, videos and settings' )?" n || die "Cancelled."
  systemctl disable --now "$UNIT" pirdfy-update.path 2>/dev/null || true
  rm -f /etc/systemd/system/pirdfy.service /etc/systemd/system/pirdfy-update.service \
        /etc/systemd/system/pirdfy-update.path /usr/local/bin/pirdfy
  systemctl daemon-reload
  rm -rf "$APP" "$APP.new"
  if [[ $PURGE -eq 1 ]]; then
    rm -rf "$DATA" "$ETC"; userdel "$SVC_USER" 2>/dev/null || true
    ok "Pirdfy and all its data were removed"
  else
    ok "Pirdfy removed. Photos and settings were kept in $DATA and $ETC (use --purge to delete them)."
  fi
}

summary() {
  local port host ip
  port=$(web_port); host=$(hostname); ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo
  echo "${G}${B}Pirdfy is installed.${N}"
  echo "  Open: http://${host}.local:${port}${ip:+   or   http://${ip}:${port}}"
  if [[ -n $GENERATED_PASSWORD ]]; then
    echo "  Password: ${B}${GENERATED_PASSWORD}${N}   (shown only now; change it in Settings)"
  fi
  echo "  Commands: pirdfy status | logs | update | doctor | help"
  echo "  Settings file: $ETC/config.yaml     Data: $DATA"
}

main() {
  parse_args "$@"
  trap 'on_error $LINENO' ERR
  trap on_exit EXIT
  case "$CMD" in
    install) do_install ;;
    update) do_update ;;
    __apply) do_apply ;;
    doctor) do_doctor ;;
    model) do_model ;;
    uninstall) do_uninstall ;;
  esac
}

main "$@"
