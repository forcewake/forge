# Runtime controls and recovery

The ordinary issue → approved plan → CI lane → Draft path remains the
provider services' execution route. Adaptive controls are a separately
enabled surface; do not infer availability from a historical recipe or
from the new lane package. Current configuration and typed refusals are
authoritative. Start with [operator commands](operator-commands.md) and
[the read-only operator API](operator-view.md).

A control command is durable before its acknowledgement. Received,
authorized, dispatched and applied are different states. Pause is safe
only when the checkpoint is committed and its execution fence is held;
runner terminal observation is a separate milestone. Resume must activate
the exact pinned checkpoint from the approved base and generation. A
missing/corrupt required checkpoint refuses before model execution.
An explicit restart discards WIP only through the authorized command path.

Steering is guidance, never approval or wider write scope. Material changes
need the existing approval gate and a newly frozen input. Stale head,
revision, execution epoch or command sequence refuses; it never rewrites
human commits. Acknowledgement cannot prove useful delivery, independent
verification or human acceptance.

Cancel revokes publication authority before stopping the lane. Unknown
native start/cancel outcomes hold occupancy until observation proves the
provider state. Probe unknown publication effects before retry. Restore
lost checkpoint state from a consistent backup or retire the run; never
force a resume by editing the database.

`/status` and `/why-blocked` report the reason and permitted next step.
Use `/reconcile` for an unresolved publication effect and `/retry` only
where the shared rejection rules allow it. Expired credentials need
rotation and re-dispatch; exhausted budgets need an explicit amendment,
not a hidden paid retry. The readonly closing review can continue against
the same verified candidate without launching a coder.

See [backup/restore](backup-restore.md), [token rotation](token-rotation.md),
[closing reserve](closing-budget.md), [review rounds](review-rounds.md) and
[deployment boundaries](deployment-boundaries.md). Historical recipe
qualification and live traces remain at original path
`docs/operations/adaptive-runbook.md` in the [archive](../archive.md).
