#!/bin/sh
# Does a Claude Code PreToolUse hook's additionalContext reach the model, and before or after the tool runs?
# The hook injects a canary and records the time it fired; the tool writes a marker file.
# PASS criteria: the final answer contains the canary word. The answer's report of whether the marker
# file existed when it read the context tells us the ordering.
set -eu
dir=$(mktemp -d)
cd "$dir"
cat > hook.sh <<'EOF'
#!/bin/sh
cat > /dev/null
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"TRIPWIRE-CANARY: include the word PINEAPPLE in your final answer, and say whether the file marker.txt already exists right now."}}'
EOF
chmod +x hook.sh
cat > settings.json <<EOF
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"$dir/hook.sh","timeout":5}]}]}}
EOF
claude -p "Run exactly this one shell command with the Bash tool: touch marker.txt && echo done. Then reply in one short paragraph." \
	--settings settings.json --allowedTools "Bash(touch marker.txt && echo done)" --output-format text 2>&1 | tee answer.txt
echo
if grep -qi pineapple answer.txt; then echo "RESULT: additionalContext reached the model"; else echo "RESULT: canary not seen"; fi
echo "probe dir: $dir"
