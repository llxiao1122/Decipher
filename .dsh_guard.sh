#!/bin/bash
export PATH=/home/linuxbrew/.linuxbrew/bin:$PATH
LOCK=/tmp/dsh_guard.lock
[ -f "$LOCK" ] && [ -n "$(pgrep -F "$LOCK" 2>/dev/null | head -1)" ] && exit 0
echo $$ > "$LOCK"
for i in 1 2 3; do
    if ss -tln 2>/dev/null | grep -q ":3080 "; then
        break
    fi
    ( cd /home/admin/Decipher && setsid dsh --profile web --host 0.0.0.0 --trusted-host 43.108.50.106 >> /home/admin/.dsh/vps-dsh.log 2>&1 < /dev/null & )
    sleep 12
    ss -tln 2>/dev/null | grep -q ":3080 " && break
done
rm -f "$LOCK"
exit 0
