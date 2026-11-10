#!/usr/bin/env bash
# Runs the C.1 to C.5 checks in order and prints one total. Exit 0 only if all 40 rules pass.
# Usage: bash check_c_all.sh
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_c_1.sh"; . "$D/check_c_2.sh"; . "$D/check_c_3.sh"; . "$D/check_c_4.sh"; . "$D/check_c_5.sh"
pass=0; fail=0
boot; rules_c_1; rules_c_2; rules_c_3; rules_c_4; rules_c_5; finish
