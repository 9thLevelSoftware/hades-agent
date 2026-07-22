"""Reversible workspace effect adapters for mission-coordinated file mutations.

Task 4 — two adapters that mediate between the coordinator and the real
filesystem:

* ``WorkspaceEffectAdapter`` (adapter_id ``workspace.v1``)
  handles ``write_file`` / ``patch`` mutations with full before/after
  snapshots, forced per-transaction checkpoints, and dependency-aware
  cascade compensation.

* ``WorkspaceCommitEffectAdapter`` (adapter_id ``workspace_commit.v1``)
  performs bounded local ``git add + commit`` inside a disposable worktree,
  with fail-closed compensation via ``git reset --mixed``.

Both adapters are stdlib-only and rely on ``tools/checkpoint_manager.py``
for shadow-Git state.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from agent.effect_transactions import (
    EffectSemantics,
    PreparedEffect,
    OperationRequest,
)
from tools.checkpoint_manager import (
    CheckpointManager,
    CheckpointRef,
    CHECKPOINT_BASE,
)
from tools.patch_parser import (
    OperationType,
    PatchOperation,
    parse_v4a_patch,
)


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceAuthority:
    """Injected mission authority the adapter trusts for workspace root
    validation. Never trusts a client-supplied workspace root — always
    looks up authority keyed by mission context."""

    mission_id: str
    workspace_roots: Tuple[str, ...]
    workspace_root: Path
    actor_id: str
    workspace_kind: str = ""

    def __post_init__(self) -> None:
        # Spec: trust boundary. Capture the caller's ``workspace_roots``
        # into an immutable tuple so post-construction mutation of the
        # source list (or any nested list) cannot widen what the adapter
        # trusts. Likewise resolve ``workspace_root`` to its canonical
        # absolute form so a non-absolute or tilde-prefixed caller value
        # cannot smuggle in a different path later — the dataclass
        # itself accepts str OR Path (existing callers pass Path).
        coerced_roots: List[str] = []
        for wr in self.workspace_roots:
            try:
                coerced_roots.append(
                    str(Path(str(wr)).expanduser().resolve())
                )
            except (OSError, RuntimeError):
                coerced_roots.append(str(wr))
        object.__setattr__(
            self, "workspace_roots", tuple(coerced_roots),
        )
        try:
            object.__setattr__(
                self, "workspace_root",
                Path(str(self.workspace_root)).expanduser().resolve(),
            )
        except (OSError, RuntimeError):
            object.__setattr__(
                self, "workspace_root",
                Path(str(self.workspace_root)),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str | None:
    """SHA-256 hex of a file, or None if not readable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _mode(path: Path) -> int | None:
    """File permission mode bits, or None if not stat-able."""
    try:
        return path.stat().st_mode
    except OSError:
        return None


def _git_status(repo_root: Path) -> str:
    """Best-effort ``git status --short`` inside repo_root."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "status", "--short"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _unified_diff(before: str, after: str, label_a: str, label_b: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=label_a,
            tofile=label_b,
            lineterm="",
        )
    )


def _rewrite_v4a_headers(
    patch_text: str,
    resolved: Dict[str, str],
) -> str:
    """Rewrite V4A header lines in ``patch_text`` so each relative
    target path is replaced with its absolute authorized form.

    ``resolved`` maps the ORIGINAL header path → absolute resolved
    path. The original key is matched against the header value with
    full line reconstruction (surrounding whitespace preserved). For
    Move headers, BOTH endpoints are rewritten via the same map —
    the caller must populate ``resolved`` with both keys.

    Non-header lines (hunk lines, ``@@``, ``+``/``-``/`` ``, ``***``
    boundary markers) are preserved byte-for-byte. Only the path
    string inside an ``*** Update|Add|Delete|Move File: ...``
    header line is touched.

    ponytail: global line-by-line rewrite rather than a parser-driven
    rebuild keeps hunks byte-identical and avoids duplicating the V4A
    grammar. The execution parser (``parse_v4a_patch``) is the
    authority; this helper just substitutes the path tokens it
    identified during the prepare-time parse.
    """
    if not resolved:
        return patch_text
    out_lines: List[str] = []
    # Compile regex once. V4A headers can be one of:
    #   *** Update File: <path>
    #   *** Add File: <path>
    #   *** Delete File: <path>
    #   *** Move File: <src> -> <dst>
    move_re = re.compile(
        r"^(\*\*\*\s*Move\s+File:\s*)(.+?)(\s*->\s*)(.+?)(\s*)$",
    )
    single_re = re.compile(
        r"^(\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*)(.+?)(\s*)$",
    )
    for line in patch_text.splitlines():
        m_move = move_re.match(line)
        if m_move:
            src = m_move.group(2).strip()
            dst = m_move.group(4).strip()
            new_src = resolved.get(src, src)
            new_dst = resolved.get(dst, dst)
            out_lines.append(
                f"{m_move.group(1)}{new_src}{m_move.group(3)}{new_dst}{m_move.group(5)}"
            )
            continue
        m_single = single_re.match(line)
        if m_single:
            old = m_single.group(2).strip()
            new = resolved.get(old, old)
            out_lines.append(
                f"{m_single.group(1)}{new}{m_single.group(3)}"
            )
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _find_git_root(path: Path) -> Path | None:
    """Walk upwards from *path* looking for a ``.git`` marker."""
    check = path if path.is_dir() else path.parent
    while check != check.parent:
        if (check / ".git").exists():
            return check
        check = check.parent
    return None


def _current_branch(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _is_primary_checkout(repo: Path) -> bool:
    """A primary checkout is NOT a git worktree (i.e. it has its own
    ``.git`` directory, not a ``.git`` file pointing at a parent repo)."""
    git_marker = repo / ".git"
    return git_marker.is_dir()


# Spec: ``PreparedEffect.compensation`` must round-trip through SessionDB
# canonical JSON. ``CheckpointRef`` is a frozen dataclass and would be
# persisted as an opaque string. Serialize as a plain mapping, expose a
# tiny rehydrator for compensate() / future drift/verify paths.

def _checkpoint_ref_to_mapping(cp_ref: "CheckpointRef") -> Dict[str, Any]:
    return {
        "checkpoint_id": cp_ref.checkpoint_id,
        "working_dir": cp_ref.working_dir,
        "commit_hash": cp_ref.commit_hash,
        "created_at": cp_ref.created_at,
    }


def _checkpoint_ref_from_mapping(payload: Mapping[str, Any]) -> "CheckpointRef":
    """Rehydrate a ``CheckpointRef`` from the JSON-decoded compensation
    payload (the canonical shape produced by ``_checkpoint_ref_to_mapping``).
    Missing optional fields default to safe empty values; unknown fields
    are ignored — the durable row may carry additional SessionDB-side
    metadata that does not map to ``CheckpointRef``."""
    from tools.checkpoint_manager import CheckpointRef
    return CheckpointRef(
        checkpoint_id=str(payload.get("checkpoint_id", "")),
        working_dir=str(payload.get("working_dir", "")),
        commit_hash=str(payload.get("commit_hash", "")),
        created_at=int(payload.get("created_at", 0)),
    )


# ---------------------------------------------------------------------------
# WorkspaceEffectAdapter
# ---------------------------------------------------------------------------


class WorkspaceEffectAdapter:
    """Effect adapter for ``write_file`` / ``patch`` inside mission
    workspaces.

    Prepare: snapshot existence/SHA/mode/git state, create forced
    checkpoint, compute unified diff preview.

    Commit: invoke the real handler with normalized absolute args.

    Verify: compare after-state hashes and record changed_paths.

    Reconcile: consult durable before/after hashes without re-invoking
    the handler.

    Compensate: restore exact checkpoint state, block on drift/dependents.
    """

    adapter_id: str = "workspace.v1"

    def __init__(
        self,
        authority: WorkspaceAuthority,
        checkpoint_base: Path | None = None,
        *,
        review_callback: Callable[[Dict[str, Any]], None] | None = None,
        dependency_check: Callable[[str], bool] | None = None,
        # Spec: a recovery-time lookup that returns the durable effect
        # transaction record (or None). The adapter uses this in
        # ``reconcile`` to fetch the durable ``prepared.before`` +
        # ``verification`` evidence without re-invoking the handler —
        # the Task 3 OperationRecord only carries ``operation_id``.
        transaction_lookup: Callable[[str], Any] | None = None,
    ):
        self._authority = authority
        self._mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Override checkpoint base for tests.
        if checkpoint_base is not None:
            import tools.checkpoint_manager as _cm
            # ponytail: use the store at checkpoint_base, monkeypatch at
            # call-time so the manager's methods see the right base.
            self._checkpoint_base_override = checkpoint_base
        else:
            self._checkpoint_base_override = None
        self._review = review_callback
        self._dependency_check = dependency_check
        self._transaction_lookup = transaction_lookup
        # ponytail: track last verify result so compensate can compare
        # current state against post-commit hashes (drift detection).
        self._last_verify: Dict[str, Dict[str, str]] = {}  # op_key -> after_hashes

    # -- private path resolution -------------------------------------------

    def _resolve_path(self, filepath: str) -> Path:
        """Resolve ``filepath`` against workspace_root, normalize, and
        validate it lives inside one of the authorized ``workspace_roots``.

        Raises ``PermissionError`` if the resolved path escapes the
        authorized roots.
        """
        root = Path(self._authority.workspace_root)
        # Relative paths resolve against workspace_root.
        candidate = Path(filepath)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        # Check symlinks: resolved must be inside workspace_roots.
        allowed = False
        for wr in self._authority.workspace_roots:
            wr_resolved = Path(wr).resolve()
            try:
                resolved.relative_to(wr_resolved)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise PermissionError(
                f"resolved path {resolved!r} is outside all authorized "
                f"workspace roots"
            )
        return resolved

    def _assert_not_main_branch(self, root: Path) -> None:
        """Reject mutations on main/master or detached HEAD checkouts."""
        branch = _current_branch(root)
        if not branch or branch in ("HEAD", "main", "master"):
            raise PermissionError(
                f"mutations rejected on branch {branch!r}; "
                f"create or use a feature worktree"
            )

    def _assert_not_primary_checkout(self, root: Path) -> None:
        """Reject mutations on the primary checkout of a Kanban or
        repository workspace."""
        kind = self._authority.workspace_kind
        if kind in ("kanban", "repository"):
            if _is_primary_checkout(root):
                raise PermissionError(
                    "mutations rejected on primary checkout of "
                    f"workspace kind {kind!r}; use a worktree"
                )

    # -- EffectAdapter protocol --------------------------------------------

    def prepare(self, request: OperationRequest) -> PreparedEffect:
        args = dict(request.args)
        tool = request.tool_name
        targets: List[str] = []

        if tool == "write_file":
            path = args.get("path")
            if not path:
                raise ValueError("write_file: missing 'path'")
            targets.append(path)
        elif tool == "patch":
            mode = args.get("mode", "replace")
            if mode == "replace":
                path = args.get("path")
                if not path:
                    raise ValueError("patch replace mode: missing 'path'")
                targets.append(path)
            elif mode == "patch":
                # V4A patch: drive the same execution parser the real
                # FileOperations handler will use so target extraction
                # matches handler semantics exactly. Operations live in
                # ``PatchOperation.file_path`` (and ``new_path`` for
                # MOVE) — derive targets from those, NOT from a
                # hand-rolled regex, otherwise a parser edge case
                # could let a header past the regex but reject the
                # handler (or vice versa).
                #
                # Authorization still happens here, BEFORE any
                # mutation: every target is resolved against
                # workspace_root and validated against
                # workspace_roots so a destination outside the
                # authorized roots is blocked before commit. The
                # resolved targets are also used to rewrite the
                # V4A header lines in ``normalized_args['patch']``
                # so the real handler (which re-parses the patch)
                # operates on the same absolute paths the adapter
                # authorized.
                patch_content = args.get("patch", "")
                operations, parse_error = parse_v4a_patch(patch_content)
                if parse_error:
                    raise ValueError(
                        f"patch mode: V4A parse failed: {parse_error}"
                    )
                if not operations:
                    raise ValueError(
                        "patch mode: no file targets found in patch content"
                    )
                for op in operations:
                    targets.append(op.file_path)
                    if op.operation == OperationType.MOVE:
                        if not op.new_path:
                            raise ValueError(
                                "patch mode: 'Move File' header requires "
                                "both a source and a destination path"
                            )
                        targets.append(op.new_path)
        else:
            raise ValueError(f"unsupported tool for workspace adapter: {tool!r}")

        # Resolve and validate all target paths.
        resolved_targets: List[str] = []
        targets_with_state: List[Dict[str, Any]] = []
        for t in targets:
            resolved = self._resolve_path(t)
            root = _find_git_root(resolved)
            if root is not None:
                self._assert_not_main_branch(root)
                self._assert_not_primary_checkout(root)
            target_path = Path(resolved) if isinstance(resolved, str) else resolved
            state = {
                "path": str(resolved),
                "existed": target_path.exists(),
                "sha256": _sha256(target_path),
                "mode": _mode(target_path),
            }
            targets_with_state.append(state)
            resolved_targets.append(str(resolved))

        # Capture git status snapshot.
        git_root = _find_git_root(Path(resolved_targets[0])) if resolved_targets else None
        git_status = _git_status(git_root) if git_root else ""

        # Compute unified diff preview.
        preview_diff = ""
        if tool == "write_file" and targets:
            target_path = Path(resolved_targets[0])
            old_content = ""
            if target_path.exists():
                old_content = target_path.read_text(encoding="utf-8", errors="replace")
            new_content = args.get("content", "")
            preview_diff = _unified_diff(old_content, new_content, "a/", "b/")
        elif tool == "patch" and targets:
            target_path = Path(resolved_targets[0])
            old_content = ""
            if target_path.exists():
                old_content = target_path.read_text(encoding="utf-8", errors="replace")
            mode = args.get("mode", "replace")
            if mode == "replace":
                old_str = args.get("old_string", "")
                new_str = args.get("new_string", "")
                if old_str in old_content:
                    new_content = old_content.replace(old_str, new_str, 1 if not args.get("replace_all") else -1)
                    preview_diff = _unified_diff(old_content, new_content, "a/", "b/")

        # Force a distinct checkpoint for this transaction. The
        # checkpoint root must be a DIRECTORY the later restore can match
        # against ``authority.workspace_root``: use the git root when the
        # target lives in a repo (mission worktrees — workspace_root IS
        # the worktree root), otherwise the authorized workspace root
        # itself. The previous fallback passed the target FILE path,
        # which both failed shadow-repo init and could never satisfy
        # restore_checkpoint's root equality check.
        checkpoint_root = str(git_root or self._authority.workspace_root)
        checkpoint_base = self._checkpoint_base_override
        if checkpoint_base is not None:
            import tools.checkpoint_manager as _cm
            _orig = _cm.CHECKPOINT_BASE
            try:
                _cm.CHECKPOINT_BASE = checkpoint_base
                cp_ref = self._mgr.create_checkpoint(
                    checkpoint_root,
                    reason=f"tx:{request.operation_key}",
                    force=True,
                )
            finally:
                _cm.CHECKPOINT_BASE = _orig
        else:
            cp_ref = self._mgr.create_checkpoint(
                checkpoint_root,
                reason=f"tx:{request.operation_key}",
                force=True,
            )

        normalized_args = dict(args)
        if tool == "write_file":
            normalized_args["path"] = resolved_targets[0]
        elif tool == "patch" and args.get("mode") == "replace":
            normalized_args["path"] = resolved_targets[0]
        elif tool == "patch" and args.get("mode") == "patch":
            # Spec: the real handler (tools.file_operations.FileOperations
            # .patch_v4a) re-parses the patch text and resolves every
            # header path against its terminal/task CWD. Without
            # rewriting the V4A header lines in the patch we ship, a
            # relative ``src.txt`` would silently resolve against
            # whatever CWD the handler runs in — bypassing the
            # adapter's workspace_roots authorization. Substitute
            # each ORIGINAL header path with the absolute form the
            # adapter just authorized so the handler operates on
            # the same paths.
            #
            # ``targets`` (originals) and ``resolved_targets``
            # (absolutes) are in lockstep because we appended them
            # in the same order while walking the operations list.
            # Build the original→absolute map directly — order is
            # preserved across MOVE (source, destination) pairs.
            rewrite_map: Dict[str, str] = {}
            for orig, abs_str in zip(targets, resolved_targets):
                rewrite_map.setdefault(orig, abs_str)
            normalized_args["patch"] = _rewrite_v4a_headers(
                args.get("patch", ""), rewrite_map,
            )

        # Spec: ``compensation`` must survive Task 3 SessionDB canonical
        # JSON. ``CheckpointRef`` is a frozen dataclass and would persist
        # as an opaque string. Use a plain mapping the loader can
        # rehydrate from; ``_checkpoint_ref_from_mapping`` is the
        # rehydrator used by ``compensate`` (and any future
        # drift/verify paths). Embed ``operation_id`` so a fresh-process
        # ``compensate`` can recover ``verification.after_hashes`` from
        # SessionDB without re-running verify.
        compensation = _checkpoint_ref_to_mapping(cp_ref)
        compensation["operation_id"] = request.operation_key

        return PreparedEffect(
            adapter_id=self.adapter_id,
            normalized_args=normalized_args,
            before={
                "targets": resolved_targets,
                "targets_with_state": targets_with_state,
                "git_status": git_status,
            },
            preview={"unified_diff": preview_diff, "targets": resolved_targets},
            semantics=EffectSemantics(
                kind="reversible", idempotent=False, reconcilable=True,
            ),
            compensation=compensation,
        )

    def commit(
        self,
        prepared: PreparedEffect,
        invoke: Callable[[Mapping[str, Any]], Any],
    ) -> Any:
        return invoke(dict(prepared.normalized_args))

    def verify(
        self,
        prepared: PreparedEffect,
        result: Any,
    ) -> Mapping[str, Any]:
        targets = prepared.before["targets"]
        # ponytail: every declared target is recorded in
        # ``changed_paths`` and ``after_hashes`` including missing
        # targets as ``None`` (JSON-safe). The deletion-state case
        # (mission handler deleted a target) is represented as a
        # ``None`` hash + presence in ``changed_paths`` so reconcile
        # can certify ``landed`` (current absent, expected None) and
        # compensate can restore from the checkpoint.
        after_hashes: Dict[str, Optional[str]] = {}
        changed_paths: List[str] = []
        for t in targets:
            p = Path(t)
            if p.exists() or p.is_symlink():
                sha = _sha256(p)
                after_hashes[t] = sha
            else:
                # Missing target recorded as None (JSON-safe).
                after_hashes[t] = None
            changed_paths.append(t)
        # Track verify state for drift detection in compensate(). The
        # canonical compensation payload is a JSON-decoded mapping, so
        # pull the checkpoint_id from there (rehydrate-on-demand keeps
        # the on-disk shape JSON-stable without coupling the adapter's
        # accounting to a CheckpointRef dataclass).
        comp = prepared.compensation or {}
        cp_id = comp.get("checkpoint_id", "")
        if cp_id:
            self._last_verify[cp_id] = dict(after_hashes)
        return {"changed_paths": changed_paths, "after_hashes": after_hashes}

    def reconcile(self, record: Any) -> Mapping[str, Any]:
        """Compare durable before/after state without re-invoking handler.

        Resolution order (lazy: smallest change at the seam where the
        evidence already exists):

        1. If the in-hand ``record`` carries ``before`` + ``verification``
           (direct-record path; used by existing unit tests and any
           caller that wraps the durable row in a richer shape), use
           them as-is. This is the original behaviour, preserved.
        2. Otherwise — the Task 3 ``OperationRecord`` shape only carries
           ``operation_id`` — consult the injected
           ``transaction_lookup(operation_id)`` to fetch the durable
           SessionDB effect transaction row. Extract
           ``durable.prepared['before']`` and ``durable.verification``,
           guarded for missing/malformed evidence. Storage faults
           (lookup raises) and missing rows (lookup returns ``None``)
           are NOT promoted to landed — they return ``unknown`` so the
           coordinator can refuse rather than silently succeed.
        3. If neither path yields ``changed_paths`` + ``after_hashes``,
           return ``{"disposition": "unknown"}`` — never fabricate.
        """
        verification = getattr(record, "verification", None) or {}
        before = getattr(record, "before", None) or {}

        # Fallback: pull durable evidence via the injected lookup when
        # the in-hand record lacks usable before/verification. This is
        # the production path (Task 3 OperationRecord carries neither).
        if not before and not verification:
            lookup = self._transaction_lookup
            op_id = getattr(record, "operation_id", "") or ""
            if lookup is None or not op_id:
                return {"disposition": "unknown"}
            try:
                durable = lookup(op_id)
            except Exception:
                # Storage fault: never promote to landed.
                return {"disposition": "unknown"}
            if durable is None:
                return {"disposition": "unknown"}
            prepared = getattr(durable, "prepared", None) or {}
            if isinstance(prepared, Mapping):
                before = prepared.get("before", {}) or {}
            verification = getattr(durable, "verification", None) or {}
            if not isinstance(verification, Mapping):
                verification = {}

        after_hashes = verification.get("after_hashes", {}) or {}
        changed_paths = verification.get("changed_paths", []) or []

        # ponytail: if durable evidence is absent or malformed at this
        # point, fail closed — return ``unknown`` rather than guess.
        if not changed_paths or not after_hashes:
            return {"disposition": "unknown"}

        all_landed = True
        for path_str in changed_paths:
            p = Path(path_str)
            expected = after_hashes.get(path_str)
            if expected is None:
                # ponytail: deletion-state case — the durable record
                # says this target was deleted. ``landed`` only when
                # the target is currently absent; if a human
                # recreated it (current exists) we cannot certify the
                # deletion actually landed — return ``unknown`` so
                # the caller does NOT compensate (compensating would
                # clobber the human file).
                if p.exists() or p.is_symlink():
                    all_landed = False
                    break
                continue
            actual = _sha256(p)
            if actual != expected:
                all_landed = False
                break

        if all_landed:
            return {
                "disposition": "landed",
                "changed_paths": changed_paths,
                "after_hashes": after_hashes,
            }
        return {
            "disposition": "unknown",
            "changed_paths": changed_paths,
            "after_hashes": after_hashes,
        }

    def compensate(self, prepared: PreparedEffect) -> Mapping[str, Any]:
        """Restore checkpoint state only when safe.

        Raises ``RuntimeError`` if:
        - compensation payload missing/empty, or
        - dependency check says dependents remain, or
        - post-commit drift detected (file hashes don't match verify).
        """
        comp = prepared.compensation or {}
        cp_id = comp.get("checkpoint_id", "")
        if not cp_id:
            raise RuntimeError("no checkpoint_id in compensation data")
        # Rehydrate the durable compensation payload back into a
        # CheckpointRef; the mapping is canonical-JSON-safe.
        cp_ref = _checkpoint_ref_from_mapping(comp)

        targets = prepared.before["targets"]

        # Dependency check.
        dep_check = self._dependency_check
        if dep_check is not None:
            for target in targets:
                if not dep_check(target):
                    raise RuntimeError(
                        f"compensation blocked: dependents remain for {target}"
                    )

        # Pre-restore drift check: compare current file hashes against
        # the durable verified-after state. Resolution order:
        #   1. In-process _last_verify (same-process commit()).
        #   2. transaction_lookup(operation_id).verification.after_hashes
        #      (durable SessionDB path — survives fresh-process recovery).
        #   3. Best-effort before-state fallback for callers bypassing
        #      SessionDB (note: this compares to *before*, not *after*,
        #      so it can only block; never overwrite absent durable proof).
        verify_hashes = self._last_verify.get(cp_id, {})
        if not verify_hashes:
            op_id = comp.get("operation_id", "") if isinstance(comp, dict) else ""
            lookup = self._transaction_lookup
            if lookup and op_id:
                try:
                    durable = lookup(op_id)
                except Exception:
                    durable = None
                if durable is not None:
                    verification = getattr(durable, "verification", None) or {}
                    if isinstance(verification, Mapping):
                        verify_hashes = dict(
                            verification.get("after_hashes", {}) or {}
                        )
        # ponytail: this fallback only fires when both the in-process
        # map and the SessionDB lookup are missing. A rehydrated
        # prepared.compensation ALWAYS carries operation_id, so the
        # lookup should fire unless the caller is bypassing SessionDB.
        if not verify_hashes:
            verify_hashes = {
                ts["path"]: ts["sha256"]
                for ts in prepared.before.get("targets_with_state", [])
                if ts.get("sha256") is not None
            }
        for path_str in targets:
            expected_sha = verify_hashes.get(path_str)
            p = Path(path_str)
            # ponytail: deletion-state match — if the verified-after
            # hash is ``None`` (mission handler deleted the target)
            # and the target is currently absent, there is NO drift;
            # compensate proceeds normally to restore from the
            # checkpoint. If expected is None but the target now
            # exists (human recreated it after deletion), compensate
            # MUST NOT clobber — block via the drift guard.
            if expected_sha is None:
                if p.exists() or p.is_symlink():
                    if self._review:
                        self._review({
                            "reason": "post_commit_drift",
                            "path": path_str,
                            "expected_sha": None,
                            "actual_sha": _sha256(p),
                        })
                    raise RuntimeError(
                        f"drift detected: {path_str} expected to be "
                        f"absent (post-deletion state), but currently "
                        f"exists — refusing to clobber recreated file"
                    )
                continue
            current_sha = _sha256(p) if p.exists() else None
            if current_sha != expected_sha:
                if self._review:
                    self._review({
                        "reason": "post_commit_drift",
                        "path": path_str,
                        "expected_sha": expected_sha,
                        "actual_sha": current_sha,
                    })
                raise RuntimeError(
                    f"drift detected: {path_str} expected SHA "
                    f"{expected_sha!r}, got {current_sha!r}"
                )

        # Restore ONLY the declared targets via the per-path path —
        # a whole-root restore would clobber siblings and any other
        # unrelated human / non-mission work in the workspace.
        # ``restore_checkpoint`` enforces root+project-ref validation,
        # under-root + file/symlink-only validation, and per-target
        # delete-on-absent semantics for created/missing files.
        root = self._authority.workspace_root
        checkpoint_base = self._checkpoint_base_override
        file_paths: List[str] = [str(t) for t in targets if t]
        if checkpoint_base is not None:
            import tools.checkpoint_manager as _cm
            _orig = _cm.CHECKPOINT_BASE
            try:
                _cm.CHECKPOINT_BASE = checkpoint_base
                self._mgr.restore_checkpoint(
                    cp_ref, current_root=root, file_paths=file_paths,
                )
            finally:
                _cm.CHECKPOINT_BASE = _orig
        else:
            self._mgr.restore_checkpoint(
                cp_ref, current_root=root, file_paths=file_paths,
            )

        return {"compensated": True, "targets": targets}

    def compensate_cascade(
        self,
        preparations: Sequence[PreparedEffect],
    ) -> Mapping[str, Any]:
        """Cascade compensation in reverse order, stopping at the first
        irreversible boundary."""
        compensated = []
        for prepared in reversed(list(preparations)):
            if prepared.semantics.kind == "irreversible":
                break
            dep_check = self._dependency_check
            if dep_check is not None:
                targets = prepared.before.get("targets", [])
                blocked = any(not dep_check(t) for t in targets)
                if blocked:
                    break
            try:
                self.compensate(prepared)
                compensated.append(prepared.adapter_id)
            except RuntimeError:
                break
        return {"compensated": compensated}


# ---------------------------------------------------------------------------
# WorkspaceCommitEffectAdapter
# ---------------------------------------------------------------------------


class WorkspaceCommitEffectAdapter:
    """Bounded local commit adapter: ``git add + commit`` inside a disposable
    worktree. No remote/push operations. Compensation via
    ``git reset --mixed`` when safe."""

    adapter_id: str = "workspace_commit.v1"

    def __init__(
        self,
        checkpoint_base: Path | None = None,
        *,
        dependency_check: Callable[[str], bool] | None = None,
        review_callback: Callable[[Dict[str, Any]], None] | None = None,
        # Spec: a recovery-time lookup that returns the durable effect
        # transaction record (or None). The adapter uses this in
        # reconcile() to fetch the durable ``prepared.before`` +
        # ``verification`` evidence without re-invoking the handler.
        transaction_lookup: Callable[[str], Any] | None = None,
    ):
        self._checkpoint_base = checkpoint_base
        self._dependency_check = dependency_check
        self._review_callback = review_callback
        self._transaction_lookup = transaction_lookup
        # ponytail: in-process tracking of created commits keyed by the
        # compensation-payload fingerprint (parent_head + worktree).
        # ``before`` on the frozen record is immutable; compensate()
        # needs the created_commit to enforce "current HEAD must equal
        # exactly that commit before we reset". Durable SessionDB-side
        # evidence (verification.created_commit) is the production path;
        # this dict is the in-process fallback for unit tests / callers
        # running compensate in the same process lifetime as commit.
        self._in_process_created: Dict[str, str] = {}

    def _comp_key(self, parent_head: str, worktree: str) -> str:
        return f"{parent_head}:{worktree}"

    def _record_created_commit(
        self, parent_head: str, worktree: str, created_commit: str,
    ) -> None:
        """In-process registration of a created commit. Used by callers
        that hold an authoritative verify-result earlier in the same
        process lifetime and want compensate() to trust the recorded
        created_commit as the exact-HHEAD anchor."""
        if not created_commit:
            return
        self._in_process_created[self._comp_key(parent_head, worktree)] = created_commit

    def _lookup_created_commit(
        self, parent_head: str, worktree: str,
    ) -> str:
        return self._in_process_created.get(
            self._comp_key(parent_head, worktree), ""
        )

    def prepare(self, request: OperationRequest) -> PreparedEffect:
        args = dict(request.args)
        worktree = args.get("worktree")
        if not worktree:
            raise ValueError("local_commit: missing 'worktree'")

        # Reject any suspicious git operations.
        for forbidden in ("push", "remote", "add_remote", "extra_args"):
            if args.get(forbidden):
                raise PermissionError(
                    f"local_commit: {forbidden!r} operation not permitted"
                )

        wt_path = Path(worktree).resolve()
        if not wt_path.is_dir():
            raise ValueError(f"worktree not found: {wt_path}")

        # Spec: resolve to the git root before any branching check so a
        # subdirectory of the primary checkout cannot bypass the rejection
        # by passing a sub-path.
        git_root = _find_git_root(wt_path)
        if git_root is None:
            raise ValueError(f"not inside a git repository: {wt_path}")
        wt_path = git_root  # Re-anchor to the root for normalization.

        # Reject primary checkout (NOT a worktree). The check is done
        # against the resolved git root, not the requested subpath.
        if _is_primary_checkout(git_root):
            raise PermissionError(
                "local_commit: cannot commit on primary checkout; use a worktree"
            )

        # Detached HEAD / main / master all rejected.
        branch = _current_branch(git_root)
        if not branch or branch in ("HEAD", "main", "master"):
            raise PermissionError(
                f"local_commit: cannot commit on branch {branch!r}; "
                f"use a feature worktree"
            )

        # Record parent HEAD.
        parent_head = subprocess.check_output(
            ["git", "-C", str(git_root), "rev-parse", "HEAD"],
            text=True,
        ).strip()

        paths = list(args.get("paths") or [])
        # Spec: empty paths list is rejected before ``git add``. An empty
        # commit is rarely the intent and avoids an implicit stage sweep.
        if not paths:
            raise ValueError(
                "local_commit: 'paths' must be a non-empty list of file "
                "paths to stage"
            )

        return PreparedEffect(
            adapter_id=self.adapter_id,
            normalized_args={
                "worktree": str(git_root),
                "paths": paths,
                "message": args.get("message", ""),
            },
            before={
                "parent_head": parent_head,
                "worktree": str(git_root),
            },
            preview={"paths": paths, "message": args.get("message", "")},
            semantics=EffectSemantics(
                kind="compensatable", idempotent=False, reconcilable=True,
            ),
            # Compensation carries the durable record fields the
            # SessionDB canonical-JSON path can survive: parent_head
            # (for reset target) + worktree (for git invocations) +
            # operation_id (so a fresh-process compensate can pull
            # ``verification.created_commit`` from SessionDB without
            # relying on the in-process record).
            compensation={
                "parent_head": parent_head,
                "worktree": str(git_root),
                "operation_id": request.operation_key,
            },
        )

    def commit(
        self,
        prepared: PreparedEffect,
        invoke: Callable[[Mapping[str, Any]], Any],
    ) -> Any:
        worktree = prepared.normalized_args["worktree"]
        paths = prepared.normalized_args["paths"]
        message = prepared.normalized_args["message"]
        parent_head = prepared.before["parent_head"]

        # git add -- <paths>
        subprocess.run(
            ["git", "-C", worktree, "add", "--"] + paths,
            check=True,
            capture_output=True,
            text=True,
        )
        # git commit -m <message>
        result = subprocess.run(
            ["git", "-C", worktree, "commit", "-m", message],
            check=True,
            capture_output=True,
            text=True,
        )
        # Record the created commit so compensate() can enforce
        # "current HEAD must equal exactly this commit before reset".
        # The created commit is also returned to the caller so the
        # coordinator can persist it on the durable transaction record
        # (``verification.created_commit``) for cross-process recovery.
        created = subprocess.check_output(
            ["git", "-C", worktree, "rev-parse", "HEAD"],
            text=True,
        ).strip()
        self._record_created_commit(parent_head, worktree, created)
        return {
            "success": True,
            "stdout": result.stdout.strip(),
            "created_commit": created,
        }

    def verify(
        self,
        prepared: PreparedEffect,
        result: Any,
    ) -> Mapping[str, Any]:
        worktree = prepared.normalized_args["worktree"]
        new_head = subprocess.check_output(
            ["git", "-C", worktree, "rev-parse", "HEAD"],
            text=True,
        ).strip()
        parent_head = prepared.before["parent_head"]
        # Extract the created commit from result.created_commit (set in
        # commit()). Fallback: read HEAD if result is missing it.
        # ponytail: result is always a dict from commit() in this
        # adapter; a non-dict result is the caller's bookkeeping bug
        # and the boolean fallback at the bottom of the function is
        # the only consumer that needs to tolerate it.
        created_commit = ""
        if isinstance(result, dict):
            created_commit = result.get("created_commit", "") or created_commit
        if not created_commit:
            created_commit = new_head
        # Spec: protect against the post-commit race where a concurrent
        # git operation advanced HEAD AFTER commit() captured
        # created_commit but BEFORE verify ran. If actual HEAD differs
        # from the commit() result, the captured created_commit is no
        # longer the tip — surface that loudly so the caller can
        # reconcile instead of trusting a stale anchor.
        if created_commit and new_head != created_commit:
            raise RuntimeError(
                "verify detected HEAD race: commit() reported "
                f"created_commit={created_commit[:8]} but current HEAD="
                f"{new_head[:8]}; refusing to certify a stale commit "
                "anchor"
            )
        return {
            "created_commit": created_commit,
            "parent_head": parent_head,
            "success": (
                result.get("success", False)
                if isinstance(result, dict) else bool(result)
            ),
        }

    def reconcile(self, record: Any) -> Mapping[str, Any]:
        """Reconcile a commit after crash — check the durable record
        first (parent_head vs current HEAD). If a SessionDB lookup was
        injected, prefer its evidence: ``prepared.before.parent_head``
        + ``verification.created_commit`` from the durable row allow us
        to answer "landed" with high confidence WITHOUT re-invoking the
        handler. Without a lookup we fall back to a ``git rev-parse``
        probe of the worktree (the original behaviour, kept for unit
        tests that pre-date SessionDB wiring).

        Spec: durable ``verification.created_commit`` is the ONLY
        accepted evidence the adapter committed. We never promote to
        landed on ``current != parent_head`` alone — that would treat a
        human commit as our own. ``landed`` requires either:
          (a) current HEAD == created_commit, OR
          (b) ``git merge-base --is-ancestor <created> <current>`` exits 0
              (a descendant that includes our commit is fine — the
              effect landed; a human descendant that does NOT include
              our commit is impossible because git ancestry is linear).
        Missing created_commit → ``unknown`` (storage fault / no evidence).
        """
        prior_before: Mapping[str, Any] = {}
        verification: Mapping[str, Any] = {}
        durable = None
        lookup = self._transaction_lookup
        op_id = getattr(record, "operation_id", "")
        if lookup is not None and op_id:
            try:
                durable = lookup(op_id)
            except Exception:
                durable = None
        if durable is not None:
            # Durable record shape: prepared (mapping) + verification (mapping)
            prepared = getattr(durable, "prepared", None) or {}
            if isinstance(prepared, Mapping):
                prior_before = prepared.get("before", {}) or {}
            verification = getattr(durable, "verification", None) or {}

        # Fallback: in-hand record may carry verification directly (the
        # original Task 4 contract — tests / callers wrap the durable
        # row in a richer shape). Honour that evidence when no durable
        # lookup yielded one.
        if not verification and hasattr(record, "verification"):
            try:
                rec_v = getattr(record, "verification", None)
                if isinstance(rec_v, Mapping):
                    verification = rec_v
            except Exception:
                pass

        # If we got no parent_head from either durable prepared or the
        # record itself, we cannot answer landed honestly — return unknown.
        parent_head = prior_before.get("parent_head", "")
        worktree = prior_before.get("worktree", "")
        if not parent_head and hasattr(record, "before"):
            try:
                parent_head = (record.before or {}).get("parent_head", "")  # type: ignore[attr-defined]  # noqa: E501
                worktree = (record.before or {}).get("worktree", "")  # type: ignore[attr-defined]  # noqa: E501
            except Exception:
                parent_head, worktree = "", ""

        if not parent_head or not worktree:
            return {"disposition": "unknown"}

        # Verification.created_commit is the ONLY accepted evidence.
        created = ""
        if isinstance(verification, Mapping):
            created = verification.get("created_commit", "") or ""
        if not created:
            # ponytail: no durable created_commit → unknown. We do NOT
            # fall back to "current != parent_head" because that is
            # exactly the human-commit false-positive the spec rejects.
            return {"disposition": "unknown"}

        # Read current HEAD via the canonical subprocess helper.
        try:
            current = subprocess.check_output(
                ["git", "-C", worktree, "rev-parse", "HEAD"],
                text=True,
            ).strip()
        except Exception:
            return {"disposition": "unknown"}

        if current == created:
            return {
                "disposition": "landed",
                "created_commit": created,
                "current_head": current,
                "parent_head": parent_head,
            }
        # Use ``git merge-base --is-ancestor`` to detect a real ancestor
        # relationship. Argument-array form (no shell). Returncode 0
        # means ``created`` IS an ancestor of ``current``; nonzero
        # means it is NOT (returncode 1) or an error occurred. Any
        # non-zero result falls through to ``unknown``.
        try:
            rc = subprocess.call(
                [
                    "git", "-C", worktree,
                    "merge-base", "--is-ancestor", created, current,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            rc = 1
        if rc == 0:
            return {
                "disposition": "landed",
                "created_commit": created,
                "current_head": current,
                "parent_head": parent_head,
            }
        return {"disposition": "unknown"}

    def compensate(
        self,
        prepared: PreparedEffect,
        *,
        dependency_check: Callable[[str], bool] | None = None,
    ) -> Mapping[str, Any]:
        """``git reset --mixed <parent>`` only when HEAD still matches the
        commit we created and dependents are compensated.

        Spec: when HEAD has advanced past the created commit
        (human / another tool), compensate MUST NOT reset — call the
        injected review callback and raise RuntimeError. The exact
        match against the *created* commit (not parent_head) is the
        only safe reset condition.

        Resolution order for the created-commit anchor:
        1. ``prepared.compensation['operation_id']`` →
           ``transaction_lookup(...).verification.created_commit``
           (durable SessionDB path; survives fresh-process recovery).
        2. In-process record created by ``commit()`` earlier this run.
        3. Legacy alt slot in ``prepared.compensation['created_commit']``
           (kept for caller-supplied author annotations; never trusted
           over durable evidence).
        """
        dep = dependency_check or self._dependency_check
        if dep is not None and not dep(prepared.before.get("worktree", "")):
            raise RuntimeError(
                "compensation blocked: dependents remain"
            )

        worktree = prepared.before.get("worktree", "")
        parent_head = prepared.before.get("parent_head", "")
        if not worktree or not parent_head:
            raise RuntimeError("missing before state for compensation")

        # Resolve the created_commit anchor. Prefer the durable SessionDB
        # lookup keyed by ``compensation.operation_id``; fall back to the
        # in-process record; finally honour a caller-supplied annotation
        # on the compensation payload.
        created_commit = ""
        op_id = (prepared.compensation or {}).get("operation_id", "") \
            if isinstance(prepared.compensation, Mapping) else ""
        lookup = self._transaction_lookup
        if op_id and lookup is not None:
            try:
                rec = lookup(op_id)
            except Exception:
                rec = None
            if rec is not None:
                v = getattr(rec, "verification", None) or {}
                if isinstance(v, Mapping):
                    created_commit = v.get("created_commit", "") or ""
        if not created_commit:
            created_commit = self._lookup_created_commit(parent_head, worktree)
        if not created_commit:
            created_commit = (prepared.compensation or {}).get(
                "created_commit", ""
            ) if isinstance(prepared.compensation, Mapping) else ""

        # Read current HEAD.
        try:
            current_head = subprocess.check_output(
                ["git", "-C", worktree, "rev-parse", "HEAD"],
                text=True,
            ).strip()
        except Exception:
            raise RuntimeError("cannot read current HEAD")

        # Spec: reset ONLY when HEAD still matches the commit we
        # created (or, in case we never recorded a created_commit —
        # which would be a bookkeeping bug — never reset).
        if not created_commit or current_head != created_commit:
            review = self._review_callback
            if review is not None:
                try:
                    review({
                        "reason": (
                            "no_created_commit_anchor" if not created_commit
                            else "compensate_blocked"
                        ),
                        "expected_created_commit": created_commit,
                        "actual_head": current_head,
                        "parent_head": parent_head,
                        "worktree": worktree,
                    })
                except Exception:
                    pass
            raise RuntimeError(
                "compensate refused: HEAD does not match the commit this "
                "adapter created "
                f"(expected={created_commit[:8] if created_commit else 'unset'}, "
                f"head={current_head[:8]}); human or subsequent commit may "
                "have landed — refusing to clobber"
            )

        subprocess.run(
            ["git", "-C", worktree, "reset", "--mixed", parent_head],
            check=True,
            capture_output=True,
            text=True,
        )
        return {"compensated": True, "reset_to": parent_head}


__all__ = [
    "WorkspaceAuthority",
    "WorkspaceEffectAdapter",
    "WorkspaceCommitEffectAdapter",
    "StateMutation",
    "HermesWorkflowStateAdapter",
    "HermesCronStateAdapter",
    "HermesConfigStateAdapter",
    "register_hermes_state_adapters",
]


@dataclass(frozen=True)
class StateMutation:
    """JSON-safe adapter boundary for one versioned Hermes-state change."""

    resource: str
    action: str
    expected_revision: str | None
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "before", None if self.before is None else copy.deepcopy(dict(self.before))
        )
        object.__setattr__(
            self, "after", None if self.after is None else copy.deepcopy(dict(self.after))
        )


def _state_semantics() -> EffectSemantics:
    return EffectSemantics(kind="compensatable", idempotent=False, reconcilable=True)


def _state_prepared(
    *,
    adapter_id: str,
    args: Mapping[str, Any],
    mutation: StateMutation,
    payload: Mapping[str, Any],
) -> PreparedEffect:
    before = copy.deepcopy(dict(mutation.before or {}))
    after = copy.deepcopy(dict(mutation.after or {}))
    preview_before = None if mutation.before is None else copy.deepcopy(dict(mutation.before))
    preview_after = None if mutation.after is None else copy.deepcopy(dict(mutation.after))
    return PreparedEffect(
        adapter_id=adapter_id,
        normalized_args=dict(args),
        before=before,
        preview={
            "resource": mutation.resource,
            "action": mutation.action,
            "expected_revision": mutation.expected_revision,
            "before": preview_before,
            "after": preview_after,
        },
        semantics=_state_semantics(),
        compensation=dict(payload),
    )


def _state_payload(prepared_or_record: Any) -> Mapping[str, Any]:
    if isinstance(prepared_or_record, Mapping):
        payload = prepared_or_record.get("compensation", prepared_or_record)
    else:
        payload = getattr(prepared_or_record, "compensation", None)
    if not isinstance(payload, Mapping):
        raise ValueError("state adapter requires a persisted compensation payload")
    return payload


class HermesWorkflowStateAdapter:
    """Coordinator-only adapter for immutable workflow version state."""

    adapter_id = "hermes.workflow-state.v1"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @staticmethod
    def _payload(mutation: Any) -> Dict[str, Any]:
        spec = mutation.spec.model_dump(mode="json") if mutation.spec is not None else None
        return {
            "service": "workflow",
            "resource": mutation.resource,
            "action": mutation.action,
            "expected_revision": mutation.expected_revision,
            "before": {
                "version": mutation.before.version,
                "enabled": mutation.before.enabled,
                "checksum": mutation.before.checksum,
            },
            "after": {
                "version": mutation.after.version,
                "enabled": mutation.after.enabled,
                "checksum": mutation.after.checksum,
            },
            "spec": spec,
        }

    @staticmethod
    def _mutation(payload: Mapping[str, Any]) -> Any:
        from hades_cli.workflows_db import (
            WorkflowStateMutation,
            WorkflowStatePoint,
        )
        from hades_cli.workflows_spec import WorkflowSpec

        spec_data = payload.get("spec")
        return WorkflowStateMutation(
            resource=str(payload["resource"]),
            action=str(payload["action"]),
            expected_revision=payload.get("expected_revision"),
            before=WorkflowStatePoint(**dict(payload["before"])),
            after=WorkflowStatePoint(**dict(payload["after"])),
            spec=WorkflowSpec.model_validate(spec_data) if spec_data is not None else None,
        )

    def prepare(self, request: OperationRequest) -> PreparedEffect:
        from hades_cli.workflows_db import (
            prepare_state_mutation,
            snapshot_workflow_state,
        )
        from hades_cli.workflows_spec import WorkflowSpec

        args = dict(request.args)
        action = args.get("action")
        if action not in {"deploy", "enable", "disable"}:
            raise ValueError("workflow-state action must be deploy, enable, or disable")
        if action == "deploy":
            spec = WorkflowSpec.model_validate(args.get("spec"))
            workflow_id = spec.id
            version = spec.version
        else:
            workflow_id = args.get("workflow_id")
            version = args.get("version")
            if not isinstance(workflow_id, str) or not workflow_id:
                raise ValueError("workflow-state requires workflow_id")
            if version is not None and not isinstance(version, int):
                raise ValueError("workflow-state version must be an integer")
            spec = None
        snapshot = snapshot_workflow_state(self._conn, workflow_id, version)
        if action != "deploy" and snapshot.version is None:
            raise KeyError(f"workflow definition not found: {workflow_id}")
        mutation = prepare_state_mutation(snapshot, action=action, spec=spec)
        payload = self._payload(mutation)
        state = StateMutation(
            resource=f"workflow:{mutation.resource}",
            action=mutation.action,
            expected_revision=mutation.expected_revision,
            before=payload["before"],
            after=payload["after"],
        )
        return _state_prepared(
            adapter_id=self.adapter_id, args=args, mutation=state, payload=payload,
        )

    def commit(self, prepared: PreparedEffect, invoke: Callable[[Mapping[str, Any]], Any]) -> Mapping[str, Any]:
        del invoke  # State services are the only mutation path for this adapter.
        from hades_cli.workflows_db import apply_state_mutation

        mutation = self._mutation(_state_payload(prepared))
        apply_state_mutation(self._conn, mutation)
        return {"committed": True}

    def verify(self, prepared: PreparedEffect, result: Any) -> Mapping[str, Any]:
        del result
        from hades_cli.workflows_db import (
            snapshot_workflow_state,
            verify_state_mutation,
        )

        payload = _state_payload(prepared)
        mutation = self._mutation(payload)
        after = dict(payload["after"])
        snapshot = snapshot_workflow_state(
            self._conn, str(payload["resource"]), after["version"],
        )
        revision = None if snapshot.checksum is None else f"{snapshot.checksum}:{int(bool(snapshot.enabled))}"
        return {"revision": revision, "landed": verify_state_mutation(self._conn, mutation)}

    def reconcile(self, record: Any) -> Mapping[str, Any]:
        payload = _state_payload(record)
        prepared = PreparedEffect(
            adapter_id=self.adapter_id,
            normalized_args={}, before={}, preview={}, semantics=_state_semantics(),
            compensation=payload,
        )
        outcome = self.verify(prepared, None)
        return {"disposition": "landed" if outcome["landed"] else "unknown", **outcome}

    def compensate(self, prepared_or_record: Any) -> Mapping[str, Any]:
        from hades_cli.workflows_db import rollback_state_mutation

        mutation = self._mutation(_state_payload(prepared_or_record))
        rollback_state_mutation(self._conn, mutation)
        return {"compensated": True, "resource": mutation.resource}


class HermesCronStateAdapter:
    """Coordinator-only adapter for revision-checked cron job state."""

    adapter_id = "hermes.cron-state.v1"

    @staticmethod
    def _payload(mutation: Any) -> Dict[str, Any]:
        return {
            "service": "cron",
            "resource": mutation.resource,
            "action": mutation.action,
            "expected_revision": mutation.expected_revision,
            "before": copy.deepcopy(mutation.before),
            "after": copy.deepcopy(mutation.after),
        }

    @staticmethod
    def _mutation(payload: Mapping[str, Any]) -> Any:
        from cron.jobs import CronStateMutation

        return CronStateMutation(
            resource=str(payload["resource"]),
            action=str(payload["action"]),
            expected_revision=payload.get("expected_revision"),
            before=copy.deepcopy(payload.get("before")),
            after=copy.deepcopy(payload.get("after")),
        )

    def prepare(self, request: OperationRequest) -> PreparedEffect:
        from cron.jobs import get_job, prepare_create, prepare_disable, prepare_update

        args = dict(request.args)
        action = args.get("action")
        if action == "create":
            job = args.get("job")
            if not isinstance(job, Mapping) or not isinstance(job.get("id"), str):
                raise ValueError("cron-state create requires a complete job mapping with id")
            mutation = prepare_create(dict(job))
        elif action in {"update", "disable"}:
            job_id = args.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("cron-state update/disable requires job_id")
            before = get_job(job_id)
            if before is None:
                raise KeyError(f"cron job not found: {job_id}")
            if action == "update":
                updates = args.get("updates")
                if not isinstance(updates, Mapping):
                    raise ValueError("cron-state update requires updates mapping")
                mutation = prepare_update(job_id, before, dict(updates))
            else:
                mutation = prepare_disable(job_id, before)
        else:
            raise ValueError("cron-state action must be create, update, or disable")
        payload = self._payload(mutation)
        state = StateMutation(
            resource=f"cron:{mutation.resource}", action=mutation.action,
            expected_revision=mutation.expected_revision,
            before=payload["before"], after=payload["after"],
        )
        return _state_prepared(
            adapter_id=self.adapter_id, args=args, mutation=state, payload=payload,
        )

    def commit(self, prepared: PreparedEffect, invoke: Callable[[Mapping[str, Any]], Any]) -> Mapping[str, Any]:
        del invoke
        from cron.jobs import apply_mutation

        mutation = self._mutation(_state_payload(prepared))
        apply_mutation(mutation)
        return {"committed": True}

    def verify(self, prepared: PreparedEffect, result: Any) -> Mapping[str, Any]:
        del result
        from cron.jobs import canonical_revision, get_job, verify_mutation

        payload = _state_payload(prepared)
        mutation = self._mutation(payload)
        job = get_job(mutation.resource)
        return {
            "revision": canonical_revision(job),
            "landed": verify_mutation(mutation),
        }

    def reconcile(self, record: Any) -> Mapping[str, Any]:
        payload = _state_payload(record)
        mutation = self._mutation(payload)
        from cron.jobs import verify_mutation

        landed = verify_mutation(mutation)
        return {"disposition": "landed" if landed else "unknown", "landed": landed}

    def compensate(self, prepared_or_record: Any) -> Mapping[str, Any]:
        from cron.jobs import restore_mutation

        mutation = self._mutation(_state_payload(prepared_or_record))
        restore_mutation(mutation)
        return {"compensated": True, "resource": mutation.resource}


class HermesConfigStateAdapter:
    """Coordinator-only adapter for safe, one-key config.yaml mutation."""

    adapter_id = "hermes.config-state.v1"

    @staticmethod
    def _payload(mutation: Any) -> Dict[str, Any]:
        return {
            "service": "config",
            "resource": mutation.resource,
            "action": mutation.action,
            "expected_revision": mutation.expected_revision,
            "before": copy.deepcopy(mutation.before),
            "after": copy.deepcopy(mutation.after),
        }

    @staticmethod
    def _mutation(payload: Mapping[str, Any]) -> Any:
        from hades_cli.config import ConfigStateMutation

        return ConfigStateMutation(
            resource=str(payload["resource"]),
            action=str(payload["action"]),
            expected_revision=str(payload["expected_revision"]),
            before=copy.deepcopy(dict(payload["before"])),
            after=copy.deepcopy(dict(payload["after"])),
        )

    def prepare(self, request: OperationRequest) -> PreparedEffect:
        from hades_cli.config import prepare_config_mutation

        args = dict(request.args)
        if args.get("action") != "set":
            raise ValueError("config-state action must be set")
        key = args.get("key")
        if not isinstance(key, str):
            raise ValueError("config-state requires key")
        mutation = prepare_config_mutation(key, args.get("value"))
        payload = self._payload(mutation)
        state = StateMutation(
            resource=mutation.resource, action=mutation.action,
            expected_revision=mutation.expected_revision,
            before=payload["before"], after=payload["after"],
        )
        return _state_prepared(
            adapter_id=self.adapter_id, args=args, mutation=state, payload=payload,
        )

    def commit(self, prepared: PreparedEffect, invoke: Callable[[Mapping[str, Any]], Any]) -> Mapping[str, Any]:
        del invoke
        from hades_cli.config import apply_config_mutation

        mutation = self._mutation(_state_payload(prepared))
        return {"committed": True, **apply_config_mutation(mutation)}

    def verify(self, prepared: PreparedEffect, result: Any) -> Mapping[str, Any]:
        del result
        from hades_cli.config import verify_config_mutation

        return verify_config_mutation(self._mutation(_state_payload(prepared)))

    def reconcile(self, record: Any) -> Mapping[str, Any]:
        from hades_cli.config import verify_config_mutation

        outcome = verify_config_mutation(self._mutation(_state_payload(record)))
        return {"disposition": "landed" if outcome["landed"] else "unknown", **outcome}

    def compensate(self, prepared_or_record: Any) -> Mapping[str, Any]:
        from hades_cli.config import restore_config_mutation

        mutation = self._mutation(_state_payload(prepared_or_record))
        restore_config_mutation(mutation)
        return {"compensated": True, "resource": mutation.resource}


def register_hermes_state_adapters(registry: Any, *, workflow_conn: Any) -> None:
    """Register state adapters only in an injected coordinator registry."""
    registry.register(HermesWorkflowStateAdapter(workflow_conn))
    registry.register(HermesCronStateAdapter())
    registry.register(HermesConfigStateAdapter())

HadesWorkflowStateAdapter = HermesWorkflowStateAdapter

HadesCronStateAdapter = HermesCronStateAdapter

HadesConfigStateAdapter = HermesConfigStateAdapter
