"""Security utilities for AutoCAD MCP Pro.

Path traversal protection and command injection prevention.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastmcp.exceptions import ToolError

import config

log = logging.getLogger("autocad_mcp.security")


class UserInstanceProtectedError(RuntimeError):
    """A COM/window operation targeted a process this server does not own."""


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def normalize_cad_path(raw_path: str) -> str:
    r"""Return a canonical path spelling accepted by Python and AutoCAD COM.

    UNC paths are kept UNC: resolving them through :class:`Path` can add the
    Win32 extended-length prefixes that ``Documents.Open`` rejects. A single
    leading slash is repaired only on Windows when it cannot name an existing
    drive-rooted path and its first segment looks like a host or the restored
    UNC path exists.
    """
    path = str(raw_path).strip().strip("\"'").strip()
    if not path:
        return ""

    # Both slash spellings are common after JSON/tool-call transport. Only an
    # existing leading pair is unconditionally UNC; drive-relative paths such
    # as `\project\plan.dwg` need the existence/hostname checks below.
    if path.startswith(("\\\\", "//")):
        path = "\\\\" + path.lstrip("\\/").replace("/", "\\")

    lower = path.lower()
    if lower.startswith("\\\\?\\unc\\"):
        path = "\\\\" + path[len("\\\\?\\UNC\\") :]
    elif lower.startswith("\\\\?\\"):
        path = path[len("\\\\?\\") :]
    elif re.match(r"^\\\\\.\\[A-Za-z]:", path):
        # \\.\C:\... is another Win32 alias for the ordinary drive path.
        # Preserve the existing system-directory checks for this spelling.
        path = path[len("\\\\.\\") :]

    if os.name == "nt" and path.startswith(("\\", "/")) and not path.startswith("\\\\"):
        path = path.replace("/", "\\")
        first_seg = path.lstrip("\\").split("\\", 1)[0]
        unc_candidate = "\\\\" + path.lstrip("\\")
        try:
            local_exists = os.path.exists(os.path.abspath(path))
        except OSError:
            local_exists = False
        if not local_exists and "." in first_seg:
            path = unc_candidate
        elif not local_exists:
            try:
                if os.path.exists(unc_candidate):
                    path = unc_candidate
            except OSError:
                pass

    return os.path.normpath(path)


def _is_unc_path(path: str | Path) -> bool:
    return str(path).startswith("\\\\")


def _unc_parts(path: str | Path) -> list[str]:
    return [part for part in str(path).lstrip("\\").split("\\") if part]


_BLOCKED_PATH_PATTERNS = re.compile(
    r"(?:^|[\\/])\.\.(?:[\\/]|$)|(%2e%2e)|(\x00)",
    re.IGNORECASE,
)


_DRIVE_ANCHOR = re.compile(r"^[A-Za-z]:$")


def _reject_non_local_anchor(original: str, resolved: Path) -> None:
    r"""Reject device paths that can bypass plain ``X:\`` security checks.

    Extended-length prefixes have already been removed by
    :func:`normalize_cad_path`; UNC paths take their own validated branch.
    """
    drive = resolved.drive
    if _DRIVE_ANCHOR.match(drive):
        return
    log.warning("Blocked non-local path anchor: %s -> %s", original, resolved)
    raise ToolError(
        f"Path rejected: '{original}' resolves to '{resolved}', which is not a "
        f"plain local drive path (anchor '{drive}'). Device paths are not accepted."
    )


def _normalized_allowed_root(path: str | Path) -> Path:
    normalized = normalize_cad_path(str(path))
    if _is_unc_path(normalized):
        return Path(normalized)
    return Path(normalize_cad_path(str(Path(normalized).resolve())))


def validate_path(path: str, *, allow_write: bool = False) -> Path:
    """Normalize and validate a local or optionally allowed UNC file path."""
    if not path or not path.strip():
        raise ToolError("File path cannot be empty.")

    normalized = normalize_cad_path(path)
    # Check the input as well as the normalized value: normpath collapses `..`
    # segments, and a collapsed traversal must not become an accepted path.
    if _BLOCKED_PATH_PATTERNS.search(path) or _BLOCKED_PATH_PATTERNS.search(normalized):
        log.warning("Blocked path traversal attempt: %s", path)
        raise ToolError(
            f"Path rejected: suspicious traversal pattern detected in '{path}'. "
            "Use absolute paths without '..' components."
        )
    if re.match(r"^\\\\[?.]\\", normalized):
        raise ToolError(f"Path rejected: device path '{path}' is not accepted.")

    is_unc = _is_unc_path(normalized)
    if is_unc:
        if not getattr(config.settings, "allow_unc_paths", True):
            raise ToolError(
                f"Path rejected: UNC network paths are disabled: '{normalized}'. "
                "Set ALLOW_UNC_PATHS=true to enable them."
            )
        if len(_unc_parts(normalized)) < 2:
            raise ToolError(
                f"Path rejected: UNC path must include both server and share: '{normalized}'."
            )
        # Do not resolve UNC paths: AutoCAD COM rejects the \\?\UNC\ prefix.
        resolved = Path(normalized)
    else:
        resolved = Path(normalize_cad_path(str(Path(normalized).resolve())))
        if os.name == "nt":
            _reject_non_local_anchor(path, resolved)

    _SYSTEM_DIRS = [
        Path("C:/Windows").resolve(),
        Path("C:/Program Files").resolve(),
        Path("C:/Program Files (x86)").resolve(),
        Path("/etc").resolve(),
        Path("/usr").resolve(),
        Path("/bin").resolve(),
        Path("/sbin").resolve(),
        Path("/boot").resolve(),
        Path("/sys").resolve(),
        Path("/proc").resolve(),
    ]
    security_path = resolved
    if is_unc:
        parts = _unc_parts(resolved)
        if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]\$", parts[1]):
            # A UNC admin share still names a system drive; do not let its
            # different anchor bypass the ordinary system-directory policy.
            security_path = Path(parts[1][0] + ":\\" + "\\".join(parts[2:]))
    for sys_dir in _SYSTEM_DIRS:
        try:
            if security_path.is_relative_to(sys_dir):
                log.warning("Blocked system directory access: %s", resolved)
                raise ToolError(
                    f"Path rejected: '{resolved}' is inside a system directory. "
                    "File operations in system directories are not allowed."
                )
        except (ValueError, TypeError):
            pass

    if config.settings.allowed_paths:
        allowed = False
        for allowed_dir in config.settings.allowed_paths:
            try:
                if resolved.is_relative_to(_normalized_allowed_root(allowed_dir)):
                    allowed = True
                    break
            except (OSError, ValueError, TypeError):
                # Different drives and UNC shares are simply non-matches.
                pass
        if not allowed:
            allowed_str = ", ".join(str(p) for p in config.settings.allowed_paths)
            log.warning("Path outside allowed directories: %s", resolved)
            raise ToolError(
                f"Path rejected: '{resolved}' is not inside any allowed directory. "
                f"Allowed directories: {allowed_str}. "
                "Set ALLOWED_PATHS environment variable to configure."
            )

    if allow_write:
        parent = resolved.parent
        if not parent.exists():
            raise ToolError(
                f"Parent directory does not exist: '{parent}'. "
                "Create the directory first or use a valid path."
            )

    return resolved


# ---------------------------------------------------------------------------
# Command / LISP sanitization
#
# Why it is NOT incoherent to block INSERT/SAVEAS/NEW/OPEN/PURGE/ERASE here
# while `block_insert` / `drawing_save_as` / `drawing_new` / `drawing_open` /
# `drawing_purge` / `entity_delete` are tools — this gets re-raised, so it is
# recorded once:
#
# `sanitize_command` and `sanitize_lisp` each have exactly ONE caller, both of
# them the COM-only free-text escape hatch into SendCommand. The policy is
# scoped to the CHANNEL, not to the operation, which is capability narrowing
# rather than a contradiction.
#
# That "the CHANNEL" was singular was wrong, and it took an audit to notice.
# `_ensure_linetype_loaded` and `ComBackend.linetype_load` reach SendCommand
# too, carrying a caller's linetype name and file path, and neither passed
# through anything here — so the denylist below was guarding one door of two.
# `sanitize_symbol_name` / `sanitize_macro_argument` at the end of this module
# close the other. Before adding a third SendCommand site, guard it there and
# amend this paragraph rather than this one:
#
#   * `block_insert(name, x, y, ...)` takes a block ALREADY in the drawing plus
#     typed geometry. The INSERT *command* takes a file path and pulls in an
#     external DWG, which can carry an attached VBA/LISP payload. Same name,
#     different capability.
#   * `entity_delete(handle)` deletes one named entity. `ERASE ALL` does not.
#   * `drawing_save_as` / `_open` / `_new` route their path through
#     validate_path. A free-text `SAVEAS C:\...` routes through nothing.
# ---------------------------------------------------------------------------

# Matched against the extracted VERB, so the "_XXX" duplicates the old list
# carried are gone — command_verbs() strips every decorator AutoCAD accepts.
#
# Single-letter aliases are deliberately absent. `E` is the ERASE alias, but it
# is also the Extents *option* of ZOOM, so blocking it refuses `_ZOOM\nE\n` —
# the canonical zoom-extents macro, and this repo's own safe-command fixture.
# A guardrail that blocks the most common harmless macro gets switched off, and
# then it guards nothing.
_DANGEROUS_COMMANDS = frozenset(
    {
        # destructive to the drawing
        "ERASE",
        "DELETE",
        "PURGE",
        "WBLOCK",
        "INSERT",
        "XREF",
        # ends or overwrites the session / file
        "QUIT",
        "EXIT",
        "CLOSE",
        "CLOSEALL",
        "QSAVE",
        "SAVE",
        "SAVEAS",
        "NEW",
        "OPEN",
        "RECOVER",
        "RECOVERALL",
        # loads or runs code — every channel found while probing
        "SHELL",
        "SCRIPT",
        "SCR",
        "APPLOAD",
        "NETLOAD",
        "ARX",
        "VBARUN",
        "VBALOAD",
        "VBASTMT",
        "VBAIDE",
        "VBAMAN",
        "CUILOAD",
        "MENULOAD",
        "MENU",
        "CUI",
        # writes files outside the tool layer's path validation
        "EXPORT",
        "ETRANSMIT",
        "PUBLISH",
        "SECURITYOPTIONS",
    }
)

# AutoCAD accepts a command with any mix of these leading decorators:
#   _  international (locale-independent) form
#   .  suppress a redefined command
#   -  the command-line (no dialog) variant
#   '  transparent
_COMMAND_DECORATORS = "_.-*'"


def command_verbs(command: str) -> list[str]:
    """Every command verb in a SendCommand string.

    SendCommand takes a whole macro, not one command: segments are separated by
    a newline or ``;``, and only the FIRST token of a segment is a verb — the
    rest is argument data. Scanning the raw string with a word-boundary regex is
    what refused ``MTEXT 0,0 10,10 Do not erase this part``, which is annotation
    text, while still missing spellings AutoCAD accepts.
    """
    verbs: list[str] = []
    for segment in re.split(r"[\n;]+", command):
        segment = segment.strip()
        if not segment:
            continue
        token = segment.split()[0].split(",")[0].lstrip(_COMMAND_DECORATORS)
        if token:
            verbs.append(token.upper())
    return verbs


# Dangerous LISP symbols. We match these as **bare tokens anywhere** in the
# expression — not only when they appear right after `(` — because aliasing
# tricks like `(setq f command)(f "ERASE")` reference the symbol as a value.
_DANGEROUS_LISP_PATTERNS = [
    # File and shell I/O
    r"startapp",
    r"dos_[\w-]*",
    r"vl-file-(?:delete|rename|copy|directory-p)",
    r"vl-mkdir",
    r"vl-directory-files",
    r"read-line",
    r"write-line",
    r"findfile",
    r"getfiled",
    # Command execution (every known channel)
    r"command(?:-s)?",
    r"vl-cmdf",
    # ActiveX/COM family — vla-*, vlax-*, vlr-*; covers vla-SendCommand etc.
    r"vla[xr]?-[\w-]+",
    # AutoCAD Express Tools — acet-sys-shell and friends
    r"acet-[\w-]+",
    # Code loading
    r"vl-acad-(?:de|un)fun",
    r"(?:auto)?load",
    r"arxload",
    # Indirection vectors that bypass head-only matching
    r"eval",
    r"apply",
    r"funcall",
    r"function",
    r"\(\s*quote\s+command\b",
    # Custom command invocation prefix `(c:my-cmd)`
    r"c:[\w-]+",
    # Error handler hijack
    r"\*push-error-using-command\*",
    r"\*push-error-using-stack\*",
]

# A double-quoted literal is DATA — drawing text, a layer name, a note. The
# symbol denylist must not read it. Without this, an engineering CAD server
# refused its own vocabulary: '(setq desc "Max load 500 kg")' matched
# r"(?:auto)?load" and '(setq layer "C:CENTER")' matched r"c:[\w-]+".
_LISP_STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')

_DANGEROUS_LISP_PATTERN = re.compile(
    r"(?:(?<![\w-])|^)(?:" + "|".join(_DANGEROUS_LISP_PATTERNS) + r")(?![\w-])",
    re.IGNORECASE,
)


def sanitize_command(command: str) -> str:
    """Validate an AutoCAD command string for safety.

    Raises ToolError if a dangerous command pattern is detected
    (unless DANGEROUS_COMMANDS_ENABLED is true).
    """
    if not command or not command.strip():
        raise ToolError("Command string cannot be empty.")

    if config.settings.dangerous_commands_enabled:
        log.info("Dangerous commands enabled — bypassing sanitization for: %s", command[:80])
        return command

    blocked = {verb for verb in command_verbs(command) if verb in _DANGEROUS_COMMANDS}
    if blocked:
        log.warning("Blocked dangerous command: %s", command[:120])
        raise ToolError(
            f"Command rejected: {', '.join(sorted(blocked))} "
            f"{'is a restricted command' if len(blocked) == 1 else 'are restricted commands'}. "
            "This denylist is a guardrail against accidental destruction, not a "
            "security boundary — "
            "prefer the typed tools (entity_delete, drawing_save_as, block_insert, "
            "drawing_purge), which validate their arguments. Set "
            "DANGEROUS_COMMANDS_ENABLED=true to allow all commands."
        )

    return command


def sanitize_lisp(expression: str) -> str:
    """Validate an AutoLISP expression for safety.

    Raises ToolError if a dangerous LISP function is detected
    (unless DANGEROUS_COMMANDS_ENABLED is true).
    """
    if not expression or not expression.strip():
        raise ToolError("LISP expression cannot be empty.")

    if config.settings.dangerous_commands_enabled:
        log.info(
            "Dangerous commands enabled — bypassing LISP sanitization for: %s", expression[:80]
        )
        return expression

    # Blank the literals, then scan. The ORIGINAL expression is returned:
    # this decides whether to run it, it does not rewrite it.
    code_only = _LISP_STRING_LITERAL.sub('""', expression)
    if _DANGEROUS_LISP_PATTERN.search(code_only):
        log.warning("Blocked dangerous LISP expression: %s", expression[:120])
        raise ToolError(
            "LISP expression rejected: contains a restricted function. "
            "Restricted functions include startapp, command, load, eval, file I/O, etc. "
            "Set DANGEROUS_COMMANDS_ENABLED=true to allow all LISP expressions."
        )

    return expression


# ---------------------------------------------------------------------------
# Symbol-table names interpolated into a SendCommand macro
# ---------------------------------------------------------------------------
#
# The scoped-to-the-CHANNEL argument above assumed `sanitize_command` and
# `sanitize_lisp` were the only routes into SendCommand. They were not.
# `_ensure_linetype_loaded` built `_-LINETYPE _LOAD {name} {file}` from a
# caller's `linetype=` argument, and `ComBackend.linetype_load` added a
# caller-supplied path. SendCommand takes a whole macro and treats a newline
# and `;` as segment separators, so either string could append commands that
# the 36-verb denylist never sees. (v1.6 moved both loaders to the ActiveX
# `Linetypes.Load` member, which takes the name as data; the guard stays,
# because a name this rule refuses was never a loadable linetype either.)
#
# The guard is the DXF symbol-name rule rather than a second blocklist. AutoCAD
# already forbids these characters in a table name, so nothing this rejects was
# ever a loadable linetype, and the injection characters fall out of the rule
# instead of having to be enumerated and kept up to date.

#: Characters DXF forbids in a symbol-table name, plus the whitespace that ends
#: a SendCommand segment.
_FORBIDDEN_SYMBOL_CHARS = set('<>/\\":;?*|,=`') | set("\n\r\t")

#: Long enough for any real linetype, short enough not to be a payload.
_MAX_SYMBOL_NAME = 255


def illegal_symbol_name_chars(name: str) -> list[str]:
    """The characters in `name` that DXF forbids in a symbol-table name, sorted.

    The one rule behind every symbol-name gate in this repo: `sanitize_symbol_name`
    refuses on it for macro interpolation, and pre-write validators (block
    primitive specs, for one) refuse on it so ezdxf's own `DXFValueError` never
    fires mid-write after earlier entities have already landed.
    """
    return sorted({c for c in name if c in _FORBIDDEN_SYMBOL_CHARS or ord(c) < 32})


def sanitize_symbol_name(name: str, *, kind: str = "symbol") -> str:
    """Return `name` if it is a legal DXF symbol-table name, else refuse.

    Used at every point where a caller-controlled name is interpolated into an
    AutoCAD command macro. The refusal deliberately does not echo the rejected
    string back: an error message that a client logs or renders is a channel
    too, and a payload that reaches a log is a payload that reached something.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise ToolError(f"{kind} name rejected: empty.")
    if len(candidate) > _MAX_SYMBOL_NAME:
        raise ToolError(f"{kind} name rejected: longer than {_MAX_SYMBOL_NAME} characters.")
    offenders = illegal_symbol_name_chars(candidate)
    if offenders:
        shown = ", ".join(repr(c) for c in offenders)
        raise ToolError(
            f"{kind} name rejected: DXF symbol-table names cannot contain {shown}. "
            "This name would also end the AutoCAD command macro it is placed in."
        )
    return candidate


#: What ends a segment inside a SendCommand macro. A path legitimately contains
#: ':' and '\', so a file argument cannot be held to the symbol-name rule -- it
#: gets this narrower one plus `validate_path`.
_MACRO_SEPARATORS = set("\n\r;") | {chr(c) for c in range(32)}


def sanitize_macro_argument(value: str, *, kind: str = "argument") -> str:
    """Return `value` if it cannot end the AutoCAD command macro it sits in.

    For arguments that are legitimately allowed the characters DXF forbids in a
    symbol name -- a file path, mostly. Narrower than `sanitize_symbol_name` on
    purpose: the goal is only that the string cannot start a second command.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise ToolError(f"{kind} rejected: empty.")
    if any(c in _MACRO_SEPARATORS for c in candidate):
        raise ToolError(
            f"{kind} rejected: contains a newline, carriage return, semicolon or "
            "control character, which would end the AutoCAD command macro it is "
            "placed in and start another."
        )
    return candidate
