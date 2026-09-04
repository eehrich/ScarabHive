---
name: gamedev-loop
description: The working loop for a change to a Godot 4 project — read the project before editing, plan as a task list, edit files, parse-check after every script edit, run headless, playtest in the editor with a frozen clock, land assets, and have the finished change attacked before calling it done. Use for any request to build, change or debug a game.
metadata:
  version: '1.0.0'
---

# The loop

Cheap in this order, expensive in any other.

## 1. Read the project first

`project.godot` says what the game is: main scene, autoloads, input map,
window size. Then the scene the task is about (`.tscn` is text — read it),
its scripts, and who instances it (`coder_explorer` for "which scene
instances X"). A request names a symptom; the scene tree says what happens.

**Which channel is open?** `godot_status` once. Editor answering → you can
look and play. Not answering → headless only; say so in the report.

## 2. Task list before you start

More than one edit → steps into the task tool first, updated as you go.
One step in progress at a time.

## 3. Edit in files

Scripts and scenes are text. New nodes, new scenes, new resources: write
the `.tscn`/`.tres` (conventions in `godot-conventions`) or generate them
with `godot_script`. The addon edits **existing** nodes only.

Two writers, one rule: after a file edit, `godot_scene reload` if the
editor has that scene open; after a `godot_node update`, `godot_scene save`
before any file edit of that scene. Skipping either loses one side's work
silently.

## 4. Check after every script edit

`godot_check` — seconds. A parse error that is not caught here shows up as
a node that does nothing, and the next hour goes into the wrong question.

## 5. Run headless

`godot_run` on the touched scene, 60–120 frames. The verdict is the
`errors` list with `file:line`; the exit code is 0 either way. Fix the
first error, run again. "It printed the right thing" is a result; "no
errors" alone is not, when the task was behaviour.

## 6. Playtest, deterministically

Editor open: `godot_play run` (frozen) → `step frames=1` →
`godot_observe screenshot` → `media_ops_load` and **look** → `step` with
`inputs` for the behaviour under test → look again → `godot_observe logs`
since the last cursor. `step_until` with an expression when you wait for a
state, `exec` to read a variable or set one up. Stop with `stop`.

Nothing moves between your calls. If you `thaw`, you gave that up.

A screenshot that looks right proves that frame. A `report` expression
(`step ... report=["root.get_node('Player').position"]`) proves the number.
Use both when the task is "the player should be at Y".

## 7. Assets

A model, a texture, a sprite: brief a sub-agent (`asset-pipeline`), get a
path back, land it in `assets/`, `godot_import_assets`, reference it.
Never describe an asset you did not load and look at.

## 8. One runnable check, proven to bite

Behaviour worth keeping gets the smallest thing that fails when it breaks:
a `godot_run` of a test scene that `push_error`s on the wrong state, or a
GUT test if the project has GUT. Then break the guarded line, watch it go
red, restore.

## 9. Have it attacked

`coder_reviewer` on the finished change with the diff and the scene paths.
Findings are claims: verify each in the code; roughly a third do not
survive. Detail in `adversarial-review`.

# When to delegate

| Ask | Because |
|---|---|
| `coder_explorer`: "which scenes instance Enemy.tscn, who connects `died`" | The sweep costs thousands of tokens, the answer is three lines |
| `gamedev_tester`: "check + run scene X, run GUT" | A failing run is mostly noise |
| `blender_agent` / `image_agent`: an asset by brief | Their loop is visual and long; their screenshots stay in their context |
| `coder_reviewer`: attack the finished change | Fresh eyes |

Keep: every judgement about whether the game is right, every edit, every
playtest. A sub-agent has none of your conversation — name paths, paste
the diff, state the question.

# Reporting

What changed, what you ran, what you saw. Scenes as `res://`, files as
`path:line`. Close with, last thing in your answer:

```
Ran:      <godot_check / godot_run results>
Played:   <what you stepped and what the screenshot / report showed — or "headless only: <why>">
Check:    <the check left behind — or why this is too trivial for one>
Reviewed: <coder_reviewer instance + findings — or "skipped: <reason>">
```
