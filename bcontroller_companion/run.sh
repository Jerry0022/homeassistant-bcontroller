#!/usr/bin/env bash
# BController Companion Add-on — Smart Payload Installer
#
# Based on ha-customapps template.
# Copies the bundled integration + frontend assets to HA config,
# but only if the versions differ (avoids unnecessary restarts).

set -e

# ─── Configuration ──────────────────────────────────────────────────
DOMAIN="bcontroller"
ADDON_NAME="BController"
# ────────────────────────────────────────────────────────────────────

INTEGRATION_SOURCE="/payload/custom_components/${DOMAIN}"
INTEGRATION_TARGET="/config/custom_components/${DOMAIN}"
LOVELACE_SOURCE="/payload/www/community/${DOMAIN//_/-}"
LOVELACE_TARGET="/config/www/community/${DOMAIN//_/-}"
INSTALL_STATE_PATH="/config/.storage/${DOMAIN}_installer.json"
RESTART_MARKER_PATH="/config/.storage/${DOMAIN}_restart_needed.json"

# --- Helper functions ---

get_version_from_manifest() {
    local manifest_path="$1/manifest.json"
    if [ -f "$manifest_path" ]; then
        grep -o '"version": *"[^"]*"' "$manifest_path" | head -1 | sed 's/.*"\([^"]*\)"/\1/'
    else
        echo "0.0.0"
    fi
}

has_init_marker() {
    local file="$1"
    grep -q "HomeAssistant\|config_entries\|DOMAIN" "$file" 2>/dev/null
}

write_install_state() {
    local bundled_version="$1"
    local installed_version="$2"
    local action="$3"
    cat > "$INSTALL_STATE_PATH" << EOF
{
    "bundled_version": "$bundled_version",
    "installed_version": "$installed_version",
    "last_action": "$action",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "has_lovelace": $([ -d "$LOVELACE_TARGET" ] && echo "true" || echo "false")
}
EOF
    echo "[${ADDON_NAME}] Install state written: $action"
}

write_restart_marker() {
    local version="$1"
    cat > "$RESTART_MARKER_PATH" << EOF
{
    "version": "$version",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    echo "[${ADDON_NAME}] Restart marker written for version $version"
}

# --- Main logic ---

echo "========================================"
echo "  ${ADDON_NAME} Companion Add-on"
echo "========================================"

BUNDLED_VERSION=$(get_version_from_manifest "$INTEGRATION_SOURCE")
INSTALLED_VERSION=$(get_version_from_manifest "$INTEGRATION_TARGET")

echo "[${ADDON_NAME}] Bundled version:   $BUNDLED_VERSION"
echo "[${ADDON_NAME}] Installed version:  $INSTALLED_VERSION"

if [ "$BUNDLED_VERSION" = "$INSTALLED_VERSION" ]; then
    echo "[${ADDON_NAME}] Versions match — no update needed."
    write_install_state "$BUNDLED_VERSION" "$INSTALLED_VERSION" "skipped_same_version"
    exit 0
fi

echo "[${ADDON_NAME}] Version mismatch — updating integration..."

# Create target directories
mkdir -p "$INTEGRATION_TARGET"
mkdir -p "$(dirname "$LOVELACE_TARGET")"

# Copy integration files
echo "[${ADDON_NAME}] Copying integration files..."
cp -r "$INTEGRATION_SOURCE/." "$INTEGRATION_TARGET/"

# Verify copy
if has_init_marker "$INTEGRATION_TARGET/__init__.py"; then
    echo "[${ADDON_NAME}] Integration files verified."
else
    echo "[${ADDON_NAME}] WARNING: Verification failed — files may be incomplete."
fi

# Copy Lovelace assets (if bundled)
if [ -d "$LOVELACE_SOURCE" ]; then
    echo "[${ADDON_NAME}] Copying Lovelace assets..."
    mkdir -p "$LOVELACE_TARGET"
    cp -r "$LOVELACE_SOURCE/." "$LOVELACE_TARGET/"
    echo "[${ADDON_NAME}] Lovelace assets copied."
fi

# Write install state
write_install_state "$BUNDLED_VERSION" "$BUNDLED_VERSION" "updated"

# Signal restart needed
write_restart_marker "$BUNDLED_VERSION"

# Fallback: persistent notification via Supervisor API
if [ -n "$SUPERVISOR_TOKEN" ]; then
    curl -s -X POST \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"title\": \"${ADDON_NAME} Updated\", \"message\": \"Version ${BUNDLED_VERSION} installed. Please restart Home Assistant.\", \"notification_id\": \"${DOMAIN}_update\"}" \
        "http://supervisor/core/api/services/persistent_notification/create" \
        > /dev/null 2>&1 || true
    echo "[${ADDON_NAME}] Persistent notification sent."
fi

echo "[${ADDON_NAME}] Update complete. Restart Home Assistant to apply."
echo "========================================"
