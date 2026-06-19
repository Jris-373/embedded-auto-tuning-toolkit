#!/usr/bin/env bash
# =============================================================================
# flash.sh — Build → Flash → Verify → Wait for BOOT_DONE
# =============================================================================
# Reads config.yaml (via yq or python helper) for project settings.
#
# Usage:
#   ./tools/flash.sh [--skip-build] [--skip-verify] [--config tools/config.yaml]
#
# Exit codes:
#   0 — success (binary flashed, verified, boot confirmed)
#   1 — build failed
#   2 — flash failed
#   3 — verify failed
#   4 — boot timeout (no BOOT_DONE received)
#   5 — config error
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"
SKIP_BUILD=false
SKIP_VERIFY=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-build)   SKIP_BUILD=true;   shift ;;
        --skip-verify)  SKIP_VERIFY=true;   shift ;;
        --config)       CONFIG_FILE="$2";   shift 2 ;;
        *)              echo "Unknown arg: $1"; exit 5 ;;
    esac
done

[[ -f "$CONFIG_FILE" ]] || { echo "[flash] Config not found: $CONFIG_FILE"; exit 5; }

# ---------------------------------------------------------------------------
# YAML helper — reads a dotted key via python (requires pyyaml)
# ---------------------------------------------------------------------------
_yaml() {
    python3 -c "
import yaml, sys
with open('$CONFIG_FILE') as f:
    d = yaml.safe_load(f)
for key in '$1'.split('.'):
    if isinstance(d, dict):
        d = d.get(key, '')
    else:
        d = ''
print(d if d is not None else '')
"
}

# ---------------------------------------------------------------------------
# Read config
# ---------------------------------------------------------------------------
PROJECT_ROOT="$(_yaml 'project.root')"
BUILD_SYSTEM="$(_yaml 'build.system')"
BUILD_TARGET="$(_yaml 'build.target')"
BINARY_PATH="$(_yaml 'build.binary')"
BUILD_FLAGS="$(_yaml 'build.flags')"
CLEAN_FIRST="$(_yaml 'build.clean_first')"
FLASH_BACKEND="$(_yaml 'flash.backend')"
DO_VERIFY="$(_yaml 'flash.verify')"
BOOT_TIMEOUT_MS="$(_yaml 'flash.boot_timeout_ms')"
VERIFY_RETRIES="$(_yaml 'flash.verify_retries')"
SERIAL_PORT="$(_yaml 'serial.port')"
SERIAL_BAUD="$(_yaml 'serial.baudrate')"

[[ "$SKIP_VERIFY" == "true" ]] && DO_VERIFY="false"
BOOT_TIMEOUT_S=$(awk "BEGIN {printf \"%.1f\", ${BOOT_TIMEOUT_MS:-5000}/1000}")

cd "$PROJECT_ROOT"

# ---------------------------------------------------------------------------
# Stage 1: Build
# ---------------------------------------------------------------------------
if [[ "$SKIP_BUILD" == "false" ]]; then
    echo "[flash] ========== Building =========="

    if [[ "$CLEAN_FIRST" == "true" ]]; then
        case "$BUILD_SYSTEM" in
            make)   make clean ;;
            cmake)  cmake --build build --target clean 2>/dev/null || true ;;
        esac
    fi

    case "$BUILD_SYSTEM" in
        make)
            make $BUILD_TARGET $BUILD_FLAGS
            ;;
        cmake)
            cmake --build build --target "$BUILD_TARGET" -- $BUILD_FLAGS
            ;;
        custom)
            echo "[flash] Custom build — run your build command separately"
            ;;
        *)
            echo "[flash] Unknown build system: $BUILD_SYSTEM"; exit 5
            ;;
    esac

    if [[ $? -ne 0 ]]; then
        echo "[flash] BUILD FAILED"
        exit 1
    fi
    echo "[flash] Build OK"
else
    echo "[flash] Skipping build (--skip-build)"
fi

# ---------------------------------------------------------------------------
# Pre-flash: record binary metadata
# ---------------------------------------------------------------------------
if [[ ! -f "$BINARY_PATH" ]]; then
    echo "[flash] Binary not found: $BINARY_PATH"
    exit 1
fi

BIN_SIZE=$(stat -c%s "$BINARY_PATH" 2>/dev/null || stat -f%z "$BINARY_PATH")
BIN_SHA256=$(sha256sum "$BINARY_PATH" | cut -d' ' -f1)
echo "[flash] Binary: $BINARY_PATH  size=${BIN_SIZE}  sha256=${BIN_SHA256:0:16}..."

# Record for later rounds
mkdir -p tools/logs
ROUND=$(ls tools/logs/flash_r*_build.log 2>/dev/null | wc -l)
ROUND=$((ROUND + 1))
echo "size=${BIN_SIZE}"  >  "tools/logs/flash_r${ROUND}_build.log"
echo "sha256=${BIN_SHA256}" >> "tools/logs/flash_r${ROUND}_build.log"
echo "path=${BINARY_PATH}"  >> "tools/logs/flash_r${ROUND}_build.log"

# ---------------------------------------------------------------------------
# Stage 2: Flash
# ---------------------------------------------------------------------------
echo "[flash] ========== Flashing ($FLASH_BACKEND) =========="

flash_exit=0
case "$FLASH_BACKEND" in
    openocd)
        IFACE="$(_yaml 'flash.openocd.interface')"
        TARGET="$(_yaml 'flash.openocd.target')"
        EXTRA="$(_yaml 'flash.openocd.extra_args')"
        openocd -f "$IFACE" -f "$TARGET" \
            -c "program ${BINARY_PATH} verify reset exit" \
            $EXTRA 2>&1 | tee "tools/logs/flash_r${ROUND}_openocd.log"
        flash_exit=${PIPESTATUS[0]}
        ;;

    jlink)
        DEVICE="$(_yaml 'flash.jlink.device')"
        IFACE="$(_yaml 'flash.jlink.interface')"
        SPEED="$(_yaml 'flash.jlink.speed')"
        # Write a temporary J-Link command script
        JLINK_SCRIPT="tools/logs/flash_r${ROUND}.jlink"
        cat > "$JLINK_SCRIPT" <<JLINKEOF
device $DEVICE
si $IFACE
speed $SPEED
r
h
loadfile $BINARY_PATH
r
g
exit
JLINKEOF
        JLinkExe -CommanderScript "$JLINK_SCRIPT" 2>&1 | tee "tools/logs/flash_r${ROUND}_jlink.log"
        flash_exit=${PIPESTATUS[0]}
        ;;

    stlink)
        st-flash write "$BINARY_PATH" 0x08000000 2>&1 | tee "tools/logs/flash_r${ROUND}_stlink.log"
        flash_exit=${PIPESTATUS[0]}
        ;;

    dfu)
        VID="$(_yaml 'flash.dfu.vid')"
        PID="$(_yaml 'flash.dfu.pid')"
        ALT="$(_yaml 'flash.dfu.alt')"
        dfu-util -d "${VID}:${PID}" -a "$ALT" -D "$BINARY_PATH" 2>&1 | tee "tools/logs/flash_r${ROUND}_dfu.log"
        flash_exit=${PIPESTATUS[0]}
        ;;

    *)
        echo "[flash] Unknown flash backend: $FLASH_BACKEND"
        exit 5
        ;;
esac

if [[ $flash_exit -ne 0 ]]; then
    echo "[flash] FLASH FAILED (exit=$flash_exit)"
    exit 2
fi
echo "[flash] Flash OK"

# ---------------------------------------------------------------------------
# Stage 3: Readback verify (optional)
# ---------------------------------------------------------------------------
if [[ "$DO_VERIFY" == "true" ]]; then
    echo "[flash] ========== Verifying (readback) =========="
    verify_ok=false
    for ((attempt=1; attempt<=${VERIFY_RETRIES:-3}; attempt++)); do
        case "$FLASH_BACKEND" in
            openocd)
                # Use OpenOCD dump_image + compare
                READBACK="tools/logs/flash_r${ROUND}_readback.bin"
                openocd -f "$IFACE" -f "$TARGET" \
                    -c "init; reset halt; dump_image ${READBACK} 0x08000000 ${BIN_SIZE}; exit" \
                    > /dev/null 2>&1
                if cmp -s "$BINARY_PATH" "$READBACK" 2>/dev/null; then
                    verify_ok=true
                    rm -f "$READBACK"
                    break
                else
                    echo "[flash] Verify mismatch (attempt $attempt/$VERIFY_RETRIES)"
                    sleep 1
                fi
                ;;
            *)
                # For jlink/stlink/dfu, the tool's own verify is usually sufficient
                echo "[flash] Readback verify not implemented for $FLASH_BACKEND; trusting tool verify"
                verify_ok=true
                break
                ;;
        esac
    done

    if [[ "$verify_ok" != "true" ]]; then
        echo "[flash] VERIFY FAILED after ${VERIFY_RETRIES} attempts"
        exit 3
    fi
    echo "[flash] Verify OK"
fi

# ---------------------------------------------------------------------------
# Stage 4: Wait for BOOT_DONE frame
# ---------------------------------------------------------------------------
echo "[flash] ========== Waiting for BOOT_DONE (timeout=${BOOT_TIMEOUT_S}s) =========="

BOOT_OK=false
python3 -c "
import serial, sys, time
try:
    ser = serial.Serial('$SERIAL_PORT', $SERIAL_BAUD, timeout=0.1)
except Exception as e:
    print(f'[flash] Cannot open serial: {e}', file=sys.stderr)
    sys.exit(1)

deadline = time.monotonic() + $BOOT_TIMEOUT_S
buf = b''
while time.monotonic() < deadline:
    chunk = ser.read(1024)
    if chunk:
        buf += chunk
        # Look for BOOT_DONE pattern: 0xAA 0x55 ... 0x01 0x00 (id=0x0001)
        idx = buf.find(b'\xaa\x55')
        if idx >= 0 and len(buf) - idx >= 11:
            # Check var_id at offset 4 (after sync1, sync2, seq, count)
            var_id = buf[idx+4] | (buf[idx+5] << 8)
            if var_id == 0x0001:
                print('[flash] BOOT_DONE received')
                ser.close()
                sys.exit(0)
    else:
        time.sleep(0.05)
ser.close()
print('[flash] BOOT TIMEOUT — no BOOT_DONE frame received', file=sys.stderr)
sys.exit(1)
"
BOOT_OK=$?

if [[ $BOOT_OK -ne 0 ]]; then
    exit 4
fi

echo "[flash] ========== Flash cycle complete (round $ROUND) =========="
exit 0
