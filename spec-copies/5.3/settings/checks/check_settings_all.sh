#!/usr/bin/env bash
# Runs the Settings.1 check and prints one total.
# Usage: bash /spec/settings/checks/check_settings_all.sh      Exit 0 only if all 6 rules pass.
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_settings_1.sh"
pass=0; fail=0
boot; rules_settings_1; finish
