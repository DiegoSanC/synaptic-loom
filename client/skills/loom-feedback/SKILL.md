---
name: loom-feedback
description: Record whether items from the Synaptic Loom logbook helped — useful, not applicable, or stale. Use when you finish a task in which something retrieved with /loom changed your approach or turned out wrong or outdated.
---
Feedback is per ITEM: use the ids the /loom output printed in front of each item,
`<entry>.<n>` (`17.2` is entry 17's second item). Entries with no ids cannot receive
feedback; skip them.

1. List the items you actually relied on or checked in this task, and for each one propose
   a verdict:
   - `useful` — it changed or confirmed what you did;
   - `not_applicable` — it looked relevant but did not apply here;
   - `stale` — it is no longer true (the code or a later decision contradicts it).
2. Show the list to the user with a one-line note for each (what it changed, or why it did
   not apply) and ask them to confirm or correct it. Record nothing they did not confirm.
3. For each confirmed item, from the repo's directory, run:

   `~/.loom/venv/bin/loom feedback --item <id> --verdict <verdict> --note "<one line>"`

   `Already recorded` is fine. If it exits 1, show the user the error (an unknown id, or a
   server that does not support feedback yet) and move on.

The totals are team-wide and never per person (`~/.loom/venv/bin/loom stats`).
