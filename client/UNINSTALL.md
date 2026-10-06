# Uninstalling the loom client

Reverts what `client/install.sh` did (PLAN.md §T5). Four steps; the order does not matter.

> Every path below assumes the default installation (`LOOM_HOME=~/.loom`, settings in
> `~/.claude/settings.json`). If you installed with a different `LOOM_HOME`, substitute
> it below.

## 1. Remove the SessionEnd hook from `~/.claude/settings.json`

You have two equivalent options.

### Option A — restore from the backup (simplest if you just installed)

`install.sh` leaves a timestamped backup every time it modifies the file:

```bash
ls -t ~/.claude/settings.json.bak-* | head   # the newest one predates the loom install
cp "$(ls -t ~/.claude/settings.json.bak-* | head -1)" ~/.claude/settings.json
```

Be careful if you have changed other settings since installing loom: the backup is from
that moment and you would lose those changes. Use option B in that case.

### Option B — remove only the loom entry with jq (keeps everything else)

```bash
# Safety backup and surgical removal of the SessionEnd entries that point at loom.
cp ~/.claude/settings.json ~/.claude/settings.json.bak-uninstall-$(date +%s)
jq '
  if .hooks.SessionEnd then
    .hooks.SessionEnd |= map(
      .hooks |= map(select((.command // "") | contains("/.loom/hooks/sessionend.sh") | not))
    )
    # drop the SessionEnd groups left without any command hooks
    | .hooks.SessionEnd |= map(select((.hooks | length) > 0))
    | if (.hooks.SessionEnd | length) == 0 then del(.hooks.SessionEnd) else . end
  else . end
' ~/.claude/settings.json > ~/.claude/settings.json.tmp && mv ~/.claude/settings.json.tmp ~/.claude/settings.json
```

Check that it is still valid JSON: `jq empty ~/.claude/settings.json && echo OK`.

## 1b. Remove the loom block from `~/.claude/CLAUDE.md`

`install.sh` added one block between `<!-- loom:start -->` and `<!-- loom:end -->` (the
instruction that makes the agent run `loom check` at the start of a task) and backed the
file up as `CLAUDE.md.bak-<ts>` when it already existed. Delete just the block, keeping
everything else:

```bash
cp ~/.claude/CLAUDE.md ~/.claude/CLAUDE.md.bak-uninstall-$(date +%s)
awk '/^<!-- loom:start -->/{skip=1; next} /^<!-- loom:end -->/{skip=0; next} !skip' \
  ~/.claude/CLAUDE.md > ~/.claude/CLAUDE.md.tmp && mv ~/.claude/CLAUDE.md.tmp ~/.claude/CLAUDE.md
```

If the file held nothing but the block (install.sh created it), delete it instead.

## 2. Delete the local state

```bash
rm -rf ~/.loom
```

This removes the marker queue, the pointer (`state.json`), the registration cache, the
quarantine, the `failed/` directory, the logs, the copied prompt, the copied hook and the
venv. It does not affect your Claude Code transcripts (`~/.claude/projects/`) nor the data
already uploaded to the central server.

## 3. The venv

The venv lives inside `~/.loom/venv`, so the `rm -rf ~/.loom` from step 2 already removes
it completely — nothing is installed system-wide or in your global Python (the install was
editable inside the venv). If you only want to recreate the venv without uninstalling
everything, delete `~/.loom/venv` and run `install.sh` again.

## Verification

```bash
jq '.hooks.SessionEnd' ~/.claude/settings.json   # must not list any .../.loom/hooks/sessionend.sh path
test -d ~/.loom && echo "~/.loom still present" || echo "~/.loom removed"
```
