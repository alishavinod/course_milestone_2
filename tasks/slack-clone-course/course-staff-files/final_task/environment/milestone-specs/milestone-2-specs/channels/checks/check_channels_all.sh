#!/usr/bin/env bash
# Runs the Channels.1 to Channels.4 checks in order and prints one total.
# Usage: bash /spec/channels/checks/check_channels_all.sh      Exit 0 only if all 66 rules pass.
set -u
CHECK_ALL=1
D="$(dirname "${BASH_SOURCE[0]}")"
. "$D/check_channels_1.sh"; . "$D/check_channels_2.sh"; . "$D/check_channels_3.sh"; . "$D/check_channels_4.sh"
pass=0; fail=0
boot; rules_channels_1; rules_channels_2; rules_channels_3; rules_channels_4; finish
