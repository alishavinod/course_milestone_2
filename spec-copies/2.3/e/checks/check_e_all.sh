#!/usr/bin/env bash
# Runs the E.1 to E.6 checks in order and prints one total. Exit 0 only if all 38 rules pass.
# Usage: bash check_e_all.sh
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_e_1.sh"; . "$D/check_e_2.sh"; . "$D/check_e_3.sh"; . "$D/check_e_4.sh"; . "$D/check_e_5.sh"; . "$D/check_e_6.sh"
pass=0; fail=0
boot; rules_e_1; rules_e_2; rules_e_3; rules_e_4; rules_e_5; rules_e_6; finish
