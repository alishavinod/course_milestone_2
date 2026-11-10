#!/usr/bin/env bash
# Runs the D.1 to D.3 checks in order and prints one total. Exit 0 only if all 14 rules pass.
# Usage: bash check_d_all.sh
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_d_1.sh"; . "$D/check_d_2.sh"; . "$D/check_d_3.sh"
pass=0; fail=0
boot; rules_d_1; rules_d_2; rules_d_3; finish
