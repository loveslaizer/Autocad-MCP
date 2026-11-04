# Isolated projects and staged DWG edits

Live work uses an agent-owned AutoCAD process by default. The default
`ISOLATED_INSTANCES=true` uses `DispatchEx`; do not use `GetActiveObject` or
plain `Dispatch` to select an isolated project backend. The only supported way
to use the operator's existing seat while isolation remains enabled is an
explicit `project_session_attach` call.

## 1. Create the project's single window once

```text
project_session_create(project_id="drawing-a", backend="com",
                       window_mode="virtual_desktop")
project_session_switch(project_id="drawing-a")   # only to change the default
```

**One OpenCode project/workspace has exactly one agent-owned AutoCAD window.**
The default `SINGLE_WINDOW_PER_PROJECT=true` shares the COM Application, PID,
HWND and STA executor across session IDs in that workspace. Do not create a new
`project_id` for each drawing. Even if a caller does so, the manager reuses the
existing agent-owned window instead of spawning another `acad.exe`.

Open every additional drawing in the same project with `project_stage_dwg`,
`drawing_open` or `drawing_new`. AutoCAD starts with `SDI=0` and `TASKBAR=0`, so
each drawing is an MDI tab in that window and previous tabs remain open. Opening
an already-open path activates its tab instead of duplicating it.

```text
drawing_list_tabs(project_id="drawing-a")
drawing_switch_tab(target="Floor2.dwg", project_id="drawing-a")
drawing_switch_tab(target="0", project_id="drawing-a")  # zero-based tab index
drawing_close_tab(target="Floor2.staged.dwg", save=True,
                  project_id="drawing-a")
```

Switching accepts a staged or source file name, full path or zero-based index;
it updates both the bound document and active staging record. Revision and
operator-edit counters remain per tab. Tab activation reapplies background
placement without foregrounding the agent window. Closing a tab never closes
the window or the other tabs.

`project_session_list()` reports project IDs, the shared owned PID, window mode,
active staging and watchdog events. Closing the last session that uses the
shared backend closes its agent-owned AutoCAD process. A PID or HWND that is
not registered as agent-owned is protected, including at cleanup.

Windows are placed on the configured Virtual Desktop when available, with
`bottom`, `offscreen` and `hidden` fallbacks. Screenshots use
`PrintWindow(..., PW_RENDERFULLCONTENT)` so occlusion does not decide what the
agent sees. A failed or blank bound-document capture falls back to a temporary
DXF snapshot and headless rendering; it does not capture another application's
window.

### Explicitly attach to the current AutoCAD window

```text
project_session_attach(project_id="drawing-a", target_pid=1234)
# Or select by a unique partial window title or an open DWG name:
project_session_attach(project_id="drawing-a", dwg_name="Floor1.dwg")
```

Attach only when the operator explicitly asks to use an existing window. A PID,
title or DWG selector must identify one running seat; an ambiguous selection is
refused. The backend authorizes that PID for COM without acquiring ownership,
activates the selected already-open document and keeps the window in place. It
does not call `Documents.Open`, remove the operator's unsaved tabs or register a
`Quit` cleanup. Closing the session releases COM references only and reports
`window_closed=false`; attached AutoCAD remains running at server exit too.

## 2. Existing DWGs: choose read, direct edit or staged edit

For inspection, call `drawing_open(path=..., read_only=True)` in the selected
session, **or** pass `source_dwg=...` to the one initial
`project_session_create` call. Do not repeat `drawing_open` after a create that
already opened the source. Read-only open never creates a staging copy.

For editing, open an unlocked source directly. Use staging for a locked source
or whenever a shadow copy was explicitly requested:

```text
project_session_create(project_id="drawing-a", backend="com",
                       source_dwg="C:/work/source.dwg", use_staging=True)
# Add another staged drawing as a tab in the same session/window:
project_stage_dwg(project_id="drawing-a", source_dwg_path="C:/work/Floor2.dwg")
# Do not call drawing_open on the source afterward: the staged tab is already open.
```

Each staged source is copied to
`.mcp_staging/<project_id>/<stem>.staged.dwg`; multiple staged files remain open
simultaneously. Edit only the active tab's returned
`staging.staged_dwg_path`. Sources stay untouched. The source directory is added
to the project search path so relative XREFs still resolve; references are not
rewritten to point into the staging folder.

The safe sequence for each staged active tab is:

```text
stage -> drawing_preflight -> geometry -> drawing_verify_requirements
      -> drawing_critique(focus=None) -> project_diff_staged
      -> project_commit_staged -> drawing_deliver
```

Use the measured-drafting gates in the main skill. A critique or verification
that cannot complete is not a pass.

## 3. Diff and commit each tab deliberately

`project_diff_staged(project_id=..., target_dwg=None)` compares the active
staged tab with its untouched source. Set `target_dwg` to a source path, source
or staged file name to select another staged tab. Review changed, added and
removed entities before publishing.

`project_commit_staged(project_id=..., output_path=..., target_dwg=None)` saves
the selected open staged document and atomically copies it to the output. With
no output path the default is `<source-stem>_AI_v2.dwg` beside that tab's
source. Source overwrite is refused unless `overwrite_if_unlocked=true`, and
even then a `.dwl` / `.dwl2` sidecar or Windows write lock refuses the commit.
A separate locked output is also refused.

A commit is not the delivery check: `drawing_deliver(requirements=...)` still
has to export, reopen and measure the saved artifact.

## 4. Heavy batch work

Prefer `project_run_coreconsole(project_id=..., dwg_path=...,
script_or_lisp=..., timeout=...)` for heavy AutoLISP, audit, purge and plot
batches. It runs `accoreconsole.exe` asynchronously with no window. Staging
follows `STAGING_MODE`; inspect the returned `staging` and stdout/stderr, and
verify the resulting file rather than treating exit code zero as geometric
acceptance.

The modal-dialog watchdog reads and closes only `#32770` dialogs belonging to
the session's agent-owned PID. Protected or unknown processes are skipped.
Never broaden it to dismiss the operator's dialogs.
