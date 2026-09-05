"""SequenceCompiler — expand loop/branch/subsequence plans into a flat executable DAG.

Implements the SequenceCompiler contract from 设计文档 §6.3: a YamlPlan's
hierarchical steps (v3.2 containers loop/branch/subsequence plus plain
script/action/barrier/fixture_control/call steps) are compiled into a flat
list of :class:`CompiledStep` nodes whose ``depends_on`` edges point at
expanded ids, ready for reactive scheduling.

Expansion semantics (matching LoopExecutor runtime conventions):

- FOR loops (``count`` set) expand once per iteration; each child step gets
  the id ``{child}_iter{n}`` (suffixes compose for nested loops:
  ``{child}_iter{i}_iter{j}``). The ``iteration`` field carries the
  innermost enclosing loop index.
- WHILE / FOREACH loops cannot be statically expanded (their trip count is
  only known at runtime), so they compile to a single deferred node with
  ``type == StepType.LOOP``; the scheduler routes those to LoopExecutor via
  ``source_step_id``, matching today's ``execute_loop_step`` path.
- Iterator placeholders (``${iterator_var}``) are *recorded* per iteration
  as :class:`IteratorBinding` entries — never resolved to values.
- Branch containers emit a single ``branch_eval`` node (``type ==
  StepType.BRANCH``) carrying ``then_ids``/``else_ids``; the runtime
  evaluates ``condition`` later. ``condition``/``then``/``else`` are
  promoted out of ``params`` to avoid dual sources of truth.
- Subsequence containers dissolve into their children with dotted ids
  ``{parent}.{child}`` (composing for nesting: ``{p1}.{p2}.{child}``).
- Every ``depends_on`` edge is remapped to expanded ids. Edges targeting a
  container resolve to that container's terminal (last emitted) node; edges
  from inside a loop iteration resolve siblings within the same iteration.
  Container-level ``depends_on`` gates are inherited by children that declare
  no dependencies of their own.
- ``export_outputs`` travels verbatim onto each expanded node, so inner-step
  exports propagate to plan-level outputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from shared.dsl import LoopType, StepType, YamlLoop, YamlPlan, YamlStep

_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")

#: Frame path identifying a expansion scope: one (container_id, iteration)
#: pair per enclosing loop (iteration index) or subsequence (``None``).
_Frames = tuple[tuple[str, int | None], ...]


class CircularDependencyError(Exception):
    """Raised when the compiled flat DAG contains a dependency cycle."""


@dataclass
class CompiledStep:
    """A single node of the flat executable DAG produced by SequenceCompiler.

    Attributes:
        id: Expanded unique id (may carry ``_iter{n}`` suffixes or ``{parent}.``
            prefixes derived from the source hierarchy).
        type: v3.2 step type; ``BRANCH`` marks a branch_eval node and ``LOOP``
            marks a deferred (runtime-expanded) loop node.
        script: Script path/name for script-bearing steps.
        params: Parameters passed to the script (branch control keys promoted
            to fields are removed).
        depends_on: Remapped dependencies pointing at expanded ids.
        uut_affinity: Target UUT ('any' or a specific UUT id).
        timeout: Maximum execution time in seconds.
        retry: Number of retry attempts on failure.
        on_failure: Failure policy (abort/continue/skip).
        barrier_name: Barrier group name for BARRIER steps.
        action: fixture_control action (clamp/release/set_route/read_sensor).
        fixture_id: Target fixture id for FIXTURE_CONTROL steps.
        then_ids: Branch taken-branch entry ids (BRANCH nodes only).
        else_ids: Branch skipped-branch entry ids (BRANCH nodes only).
        condition: Branch condition expression (BRANCH), breakpoint suspend
            condition (BREAKPOINT; None = unconditional hit), or WHILE
            condition (deferred LOOP nodes).
        source_step_id: Original YamlStep/YamlLoop id this node came from.
        iteration: Innermost enclosing loop iteration index (None outside loops).
        export_outputs: Whether outputs export to plan-level scope.
    """

    id: str
    type: StepType | None = None
    script: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    uut_affinity: str | None = None
    timeout: int = 60
    retry: int = 0
    on_failure: str | None = None
    barrier_name: str | None = None
    action: str | None = None
    fixture_id: str | None = None
    then_ids: list[str] = field(default_factory=list)
    else_ids: list[str] = field(default_factory=list)
    condition: str | None = None
    source_step_id: str = ""
    iteration: int | None = None
    export_outputs: bool = False


@dataclass(frozen=True)
class IteratorBinding:
    """A recorded (unresolved) iterator placeholder binding.

    Attributes:
        step_id: Expanded node id the binding applies to. For FOR loops this
            is each per-iteration child node; for FOREACH loops it is the
            deferred loop node (collection length unknown at compile time).
        placeholder: The unsubstituted placeholder, e.g. ``${item}``.
        source: Value source — ``"index"`` for FOR iteration variables or the
            collection expression for FOREACH loops.
        iteration: Iteration index when known statically, else None.
    """

    step_id: str
    placeholder: str
    source: str
    iteration: int | None


class SequenceCompiler:
    """Compiles a YamlPlan into a flat, dependency-ordered list of CompiledStep.

    Usage:
        >>> compiler = SequenceCompiler()
        >>> steps = compiler.compile(plan)
        >>> compiler.iterator_bindings  # recorded ${iterator.var} bindings
    """

    def __init__(self) -> None:
        self.iterator_bindings: list[IteratorBinding] = []
        self._reset_state()

    def compile(self, plan: YamlPlan) -> list[CompiledStep]:
        """Expand ``plan`` into the flat DAG.

        Args:
            plan: Parsed YamlPlan (see ate_platform.dsl.parser.YamlParser).

        Returns:
            Flat node list preserving original declaration order after
            expansion. Deterministic across runs.

        Raises:
            ValueError: On unknown ``depends_on`` targets or duplicate
                expanded ids.
            CircularDependencyError: If the flat DAG contains a cycle.
        """
        self.iterator_bindings = []
        self._reset_state()

        # Pass A — assign expanded ids and register scope maps so that
        # pass B can resolve edges including forward references.
        self._assign_scope(plan.steps, prefix="", suffix="", frames=())
        # Pass B — emit nodes in declaration order with remapped edges.
        nodes = self._emit_scope(plan.steps, prefix="", suffix="", frames=(), inherited_deps=[])

        self._check_cycles(nodes)
        return nodes

    # ------------------------------------------------------------------
    # Internal state
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        self._frame_children: dict[_Frames, dict[str, str]] = {}
        self._leaf_expansions: dict[str, list[str]] = {}
        self._terminal: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Pass A — id assignment
    # ------------------------------------------------------------------

    def _assign_scope(
        self, items: list[YamlStep | YamlLoop], *, prefix: str, suffix: str, frames: _Frames,
    ) -> str | None:
        """Assign expanded ids for a scope's direct children.

        Args:
            items: Child steps/loops of the scope.
            prefix: Dotted subsequence prefixes accumulated so far.
            suffix: ``_iter{n}`` suffixes accumulated so far.
            frames: Scope path of this scope.
            Returns the last expanded id assigned (container terminal).
        """
        mapping: dict[str, str] = {}
        last: str | None = None
        for item in items:
            assigned = self._assign_item(item, prefix, suffix, frames, mapping)
            if assigned is not None:
                last = assigned
        self._frame_children[frames] = mapping
        return last

    def _assign_item(
        self,
        item: YamlStep | YamlLoop,
        prefix: str,
        suffix: str,
        frames: _Frames,
        mapping: dict[str, str],
    ) -> str | None:
        """Register one item's expanded id(s) into its parent scope mapping.

        Containers (expanded loops, subsequences) register nothing — they are
        addressed through ``_terminal`` instead. Returns the last expanded id
        assigned beneath this item.
        """
        if isinstance(item, YamlLoop):
            return self._assign_loop(item, prefix, suffix, frames, mapping)

        if item.type == StepType.SUBSEQUENCE:
            child_frames = (*frames, (item.id, None))
            last = self._assign_scope(
                self._subsequence_steps(item),
                prefix=f"{prefix}{item.id}.",
                suffix=suffix,
                frames=child_frames,
            )
            if last is not None:
                self._terminal[item.id] = last
            return last

        expanded_id = f"{prefix}{item.id}{suffix}"
        mapping[item.id] = expanded_id
        self._leaf_expansions.setdefault(item.id, []).append(expanded_id)
        return expanded_id

    def _assign_loop(
        self,
        loop: YamlLoop,
        prefix: str,
        suffix: str,
        frames: _Frames,
        mapping: dict[str, str],
    ) -> str | None:
        """Assign ids for a loop: per-iteration for FOR, deferred otherwise."""
        if loop.loop_type == LoopType.FOR and loop.count:
            last: str | None = None
            for n in range(loop.count):
                child_frames = (*frames, (loop.id, n))
                iter_mapping: dict[str, str] = {}
                for child in loop.steps:
                    assigned = self._assign_item(child, prefix, f"{suffix}_iter{n}", child_frames, iter_mapping)
                    if assigned is not None:
                        last = assigned
                self._frame_children[child_frames] = iter_mapping
                if loop.iterator_var:
                    for expanded_id in iter_mapping.values():
                        self.iterator_bindings.append(
                            IteratorBinding(
                                step_id=expanded_id,
                                placeholder=f"${{{loop.iterator_var}}}",
                                source="index",
                                iteration=n,
                            )
                        )
            if last is not None:
                self._terminal[loop.id] = last
            return None

        # WHILE / FOREACH / count-less FOR: single deferred node.
        expanded_id = f"{prefix}{loop.id}{suffix}"
        mapping[loop.id] = expanded_id
        self._leaf_expansions.setdefault(loop.id, []).append(expanded_id)
        if loop.loop_type == LoopType.FOREACH:
            for placeholder in self._collect_placeholders(loop.steps):
                self.iterator_bindings.append(
                    IteratorBinding(
                        step_id=expanded_id, placeholder=placeholder, source=loop.collection or "", iteration=None,
                    )
                )
        return expanded_id

    # ------------------------------------------------------------------
    # Pass B — node emission
    # ------------------------------------------------------------------

    def _emit_scope(
        self,
        items: list[YamlStep | YamlLoop],
        *,
        prefix: str,
        suffix: str,
        frames: _Frames,
        inherited_deps: list[str],
    ) -> list[CompiledStep]:
        nodes: list[CompiledStep] = []
        for item in items:
            nodes.extend(self._emit_item(item, prefix, suffix, frames, inherited_deps))
        return nodes

    def _emit_item(
        self,
        item: YamlStep | YamlLoop,
        prefix: str,
        suffix: str,
        frames: _Frames,
        inherited_deps: list[str],
    ) -> list[CompiledStep]:
        if isinstance(item, YamlLoop):
            return self._emit_loop(item, prefix, suffix, frames, inherited_deps)

        if item.type == StepType.SUBSEQUENCE:
            container_deps = [self._resolve_target(t, frames) for t in item.depends_on]
            return self._emit_scope(
                self._subsequence_steps(item),
                prefix=f"{prefix}{item.id}.",
                suffix=suffix,
                frames=(*frames, (item.id, None)),
                inherited_deps=container_deps,
            )

        expanded_id = f"{prefix}{item.id}{suffix}"
        depends_on = [self._resolve_target(t, frames) for t in (item.depends_on or inherited_deps)]
        # 旧式无 type 字段的脚本步骤按 SCRIPT 处理（DSL v3.0 向后兼容）
        step_type = item.type if item.type is not None else StepType.SCRIPT

        if item.type == StepType.BRANCH:
            params = {k: v for k, v in item.params.items() if k not in ("condition", "then", "else")}
            condition = item.params.get("condition")
            then_ids = self._resolve_reference_list(item.params.get("then", []), frames)
            else_ids = self._resolve_reference_list(item.params.get("else", []), frames)
        else:
            params = dict(item.params)
            # BREAKPOINT carries its optional suspend condition as a field;
            # other step types have no compiled condition here (WHILE loops
            # emit their own deferred LOOP node with condition).
            condition = item.condition if item.type == StepType.BREAKPOINT else None
            then_ids = []
            else_ids = []

        return [
            CompiledStep(
                id=expanded_id,
                type=step_type,
                script=item.script,
                params=params,
                depends_on=depends_on,
                uut_affinity=item.uut_affinity,
                timeout=item.timeout,
                retry=item.retry,
                on_failure=item.on_failure if item.on_failure is not None else item.on_fail,
                barrier_name=item.barrier_name,
                action=item.action,
                fixture_id=item.fixture_id,
                then_ids=then_ids,
                else_ids=else_ids,
                condition=condition,
                source_step_id=item.id,
                iteration=self._innermost_iteration(frames),
                export_outputs=item.export_outputs,
            )
        ]

    def _emit_loop(
        self,
        loop: YamlLoop,
        prefix: str,
        suffix: str,
        frames: _Frames,
        inherited_deps: list[str],
    ) -> list[CompiledStep]:
        if loop.loop_type == LoopType.FOR and loop.count:
            container_deps = [self._resolve_target(t, frames) for t in loop.depends_on]
            nodes: list[CompiledStep] = []
            for n in range(loop.count):
                nodes.extend(
                    self._emit_scope(
                        loop.steps,
                        prefix=prefix,
                        suffix=f"{suffix}_iter{n}",
                        frames=(*frames, (loop.id, n)),
                        inherited_deps=container_deps,
                    )
                )
            return nodes

        return [
            CompiledStep(
                id=f"{prefix}{loop.id}{suffix}",
                type=StepType.LOOP,
                params={
                    key: value
                    for key, value in (
                        ("count", loop.count),
                        ("collection", loop.collection),
                        ("iterator_var", loop.iterator_var),
                        ("max_iterations", loop.max_iterations),
                        ("execution_mode", loop.execution_mode.value),
                    )
                    if value is not None
                },
                depends_on=[self._resolve_target(t, frames) for t in loop.depends_on or inherited_deps],
                condition=loop.condition,
                source_step_id=loop.id,
                iteration=self._innermost_iteration(frames),
            )
        ]

    # ------------------------------------------------------------------
    # Edge resolution and validation
    # ------------------------------------------------------------------

    def _resolve_target(self, target: str, frames: _Frames) -> str:
        """Resolve an original id reference to its expanded id.

        Lookup order: enclosing scopes innermost-outward (same-iteration
        siblings first), then container terminals (dependency on a loop or
        subsequence waits for its last node), then global leaf expansions
        (last iteration wins).
        """
        for i in range(len(frames), -1, -1):
            scope_map = self._frame_children.get(frames[:i])
            if scope_map and target in scope_map:
                return scope_map[target]
        if target in self._terminal:
            return self._terminal[target]
        expansions = self._leaf_expansions.get(target)
        if expansions:
            return expansions[-1]
        raise ValueError(
            f"Unknown dependency target '{target}': no such step, loop, or subsequence in plan"
        )

    def _resolve_reference_list(self, references: list[Any], frames: _Frames) -> list[str]:
        """Resolve a branch then/else id list; non-string entries are ignored."""
        return [self._resolve_target(ref, frames) for ref in references if isinstance(ref, str)]

    def _check_cycles(self, nodes: list[CompiledStep]) -> None:
        """Kahn's algorithm over the flat DAG; raises on any cycle."""
        ids = [node.id for node in nodes]
        if len(ids) != len(set(ids)):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"Duplicate expanded step ids: {duplicates}")

        indegree = dict.fromkeys(ids, 0)
        dependents: dict[str, list[str]] = {node_id: [] for node_id in ids}
        for node in nodes:
            for dep in node.depends_on:
                indegree[node.id] += 1
                dependents[dep].append(node.id)

        ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
        resolved = 0
        for node_id in ready:
            resolved += 1
            for dependent in dependents[node_id]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)

        if resolved != len(ids):
            cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
            raise CircularDependencyError(f"Circular dependency detected among steps: {cyclic}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _subsequence_steps(step: YamlStep) -> list[YamlStep | YamlLoop]:
        steps = step.params.get("steps", [])
        return steps if isinstance(steps, list) else []

    @staticmethod
    def _innermost_iteration(frames: _Frames) -> int | None:
        for _, iteration in reversed(frames):
            if iteration is not None:
                return iteration
        return None

    @staticmethod
    def _collect_placeholders(items: list[YamlStep | YamlLoop]) -> list[str]:
        """Collect distinct ``${var}`` placeholders in descendant params."""
        found: set[str] = set()

        def walk(entries: list[YamlStep | YamlLoop]) -> None:
            for entry in entries:
                if isinstance(entry, YamlLoop):
                    walk(entry.steps)
                    continue
                for value in entry.params.values():
                    if isinstance(value, str):
                        found.update(match.group(0) for match in _PLACEHOLDER_PATTERN.finditer(value))

        walk(items)
        return sorted(found)
