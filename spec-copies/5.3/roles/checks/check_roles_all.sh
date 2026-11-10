#!/usr/bin/env bash
# Runs the Roles.1 to Roles.4 checks in order and prints one total.
# Usage: bash /spec/roles/checks/check_roles_all.sh      Exit 0 only if all 51 rules pass.
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_roles_1.sh"; . "$D/check_roles_2.sh"; . "$D/check_roles_3.sh"; . "$D/check_roles_4.sh"
pass=0; fail=0
boot; rule_0_3; rules_roles_1; rules_roles_2; rules_roles_3; rules_roles_4; finish
