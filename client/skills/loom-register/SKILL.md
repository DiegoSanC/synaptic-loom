---
name: loom-register
description: Register the current project (by its git remote) in Synaptic Loom so its sessions contribute to the team logbook. Use when the user asks to register the project in loom.
---
Run `~/.loom/venv/bin/loom register` in the current directory and show the user the
literal output. Do not register anything by any other means.

Registering is a team-level act, once per project. It also enables capture on this machine
for the user who registers (they have decided; `loom disable` reverses it). Every other
teammate's machine decides for itself: their agent will see `registered_undecided` from
`loom check` and offer them `loom enable` / `loom disable` inside the repo. Tell the user
that.
