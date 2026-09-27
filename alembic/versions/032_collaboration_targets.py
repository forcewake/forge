"""Collaboration targets — the explicit branch/MR surface (R41-04 #359).

The recorded debt: child round ids deliberately reused the parent's
first 8 hex chars (``child_id = f"{run_id[:8]}{uuid4().hex[:24]}"``) so
every ``factory_branch`` derivation landed on the same source branch —
work identity, collaboration identity and display shorthand coupled,
and short command ids were ambiguous within a lineage.

From 032 the collaboration surface is a PERSISTED record linked from
root and child work:

- ``collaboration_targets`` — provider, repository (``project_ref``),
  the immutable SOURCE branch, the TARGET branch, the lineage's ONE MR
  identity, and the round-reference anchor (``root_run_id`` =
  delivery 1). ``uq_collaboration_target_branch`` (partial UNIQUE on
  (provider, project_ref, source_branch) WHERE status = 'active')
  makes "one active target per lineage branch" a DB invariant. A
  ``refused`` row is the durable record of an UNRESOLVED legacy
  topology — typed reason, never an inferred branch (the shape CHECK
  enforces refused ⇒ no branch, active ⇒ branch + destination);
- ``flow_runs.target_id`` — the link. New admissions (root AND round
  children) set it; branch identity resolves through it everywhere.

The data migration derives each existing GitLab run's target ONCE —
the run's OWN historical derivation (``factory/<iid>/<run-id[:8]>``,
prefix reuse included, exactly what every pre-#359 call site evaluated
for that run) — VALIDATED against the run's recorded MR/source
information before anything is materialized:

- every recorded branch (``mr_reservations.branch``,
  ``publication_intents.target_ref`` of commit intents, succeeded
  commit ``action_log.correlation_id``) must equal the derivation —
  a disagreeing record is an unresolved topology → a REFUSED row;
- the recorded MR numbers must agree on at most one MR per lineage →
  disagreement is a topology mismatch → REFUSED;
- a run with NO contradicting evidence keeps its own deterministic
  derivation — that IS the topology its admission minted (delivery 1
  derived from its own full id; a legacy round child's prefix reuse
  made its own-id derivation the lineage branch) → materialized with
  provenance ``legacy``.

Runs sharing one branch (a legacy lineage: root + prefix-sharing
children) collapse onto ONE row keyed by the branch — the earliest
run (delivery 1) is the ``root_run_id``. Existing runs and pending
events keep their original branch and MR: the backfill only RECORDS
what already stood, it rewrites nothing (``mr_reservations``,
``review_rounds``, ``flow_runs.mr_iid`` are untouched).

GitHub/Azure runs are deliberately not backfilled: those lanes keep
their per-run branch twins (no review rounds exist there); their
targets materialize when that lane adopts the contract.
"""

import hashlib

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def _derive_gitlab_source_branch(issue_iid, run_id) -> str:
    """The run's OWN historical derivation — identity.factory_branch's
    exact expression, inlined so the migration never imports the
    package (the chain stays self-contained)."""
    return f"factory/{issue_iid if issue_iid is not None else 0}/{run_id[:8]}"


def upgrade() -> None:
    op.create_table(
        "collaboration_targets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="gitlab"),
        sa.Column("project_ref", sa.String(length=255), nullable=False),
        sa.Column("issue_iid", sa.Integer(), nullable=True),
        sa.Column("root_run_id", sa.String(length=32), nullable=False),
        sa.Column("source_branch", sa.String(length=200), nullable=True),
        sa.Column("target_branch", sa.String(length=200), nullable=True),
        sa.Column("mr_iid", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("refusal_reason", sa.String(length=300), nullable=True),
        sa.Column("provenance", sa.String(length=20), nullable=False, server_default="live"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["root_run_id"], ["flow_runs.id"]),
        sa.CheckConstraint(
            "status IN ('active', 'refused')",
            name="ck_collaboration_targets_status",
        ),
        sa.CheckConstraint(
            "provenance IN ('live', 'legacy')",
            name="ck_collaboration_targets_provenance",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND source_branch IS NOT NULL AND target_branch IS NOT NULL"
            " AND refusal_reason IS NULL)"
            " OR (status = 'refused' AND source_branch IS NULL AND target_branch IS NULL"
            " AND refusal_reason IS NOT NULL)",
            name="ck_collaboration_target_shape",
        ),
    )
    op.create_index(
        "ix_collaboration_targets_root_run_id", "collaboration_targets", ["root_run_id"]
    )
    op.create_index(
        "uq_collaboration_target_branch",
        "collaboration_targets",
        ["provider", "project_ref", "source_branch"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.add_column("flow_runs", sa.Column("target_id", sa.String(length=32), nullable=True))
    op.create_index("ix_flow_runs_target_id", "flow_runs", ["target_id"])

    # The legacy adapter's data migration (see the module docstring):
    # derive ONCE per run, validate against recorded MR/source
    # information, refuse unresolved topologies — never infer.
    bind = op.get_bind()
    runs = bind.execute(
        sa.text(
            "SELECT id, project_id, issue_iid, mr_iid FROM flow_runs"
            " WHERE provider = 'gitlab' ORDER BY created_at, id"
        )
    ).fetchall()
    evidence_branches: dict[str, set[str]] = {}
    evidence_mrs: dict[str, set[int]] = {}
    for run_id, branch, mr_iid in bind.execute(
        sa.text(
            "SELECT flow_run_id, branch, mr_iid FROM mr_reservations WHERE flow_run_id IS NOT NULL"
        )
    ).fetchall():
        if branch:
            evidence_branches.setdefault(run_id, set()).add(branch)
        if mr_iid is not None:
            evidence_mrs.setdefault(run_id, set()).add(int(mr_iid))
    for run_id, target_ref in bind.execute(
        sa.text("SELECT run_id, target_ref FROM publication_intents WHERE operation = 'commit'")
    ).fetchall():
        if target_ref:
            evidence_branches.setdefault(run_id, set()).add(target_ref)
    for run_id, correlation_id in bind.execute(
        sa.text(
            "SELECT flow_run_id, correlation_id FROM action_log"
            " WHERE action_kind = 'commit' AND status = 'succeeded'"
            " AND correlation_id IS NOT NULL"
        )
    ).fetchall():
        if run_id is not None and correlation_id:
            evidence_branches.setdefault(run_id, set()).add(correlation_id)

    # branch group → materialized target id (one row per lineage
    # branch; the earliest run of the group is delivery 1).
    branch_targets: dict[tuple[str, str, str], str] = {}
    branch_mrs: dict[tuple[str, str, str], set[int]] = {}
    for run_id, project_id, issue_iid, mr_iid in runs:
        candidate = _derive_gitlab_source_branch(issue_iid, run_id)
        branches = evidence_branches.get(run_id, set())
        mrs = set(evidence_mrs.get(run_id, set()))
        if mr_iid is not None:
            mrs.add(int(mr_iid))
        key = ("gitlab", str(project_id), candidate)
        refusal: str | None = None
        if branches and branches != {candidate}:
            refusal = "recorded_branch_mismatch: derivation {} but recorded [{}]".format(
                candidate, ", ".join(sorted(branches))
            )
        group_mrs = branch_mrs.setdefault(key, set()) | mrs
        branch_mrs[key] = group_mrs
        if refusal is None and len(group_mrs) > 1:
            refusal = "recorded_mr_mismatch: " + ", ".join(f"!{mr}" for mr in sorted(group_mrs))
        if refusal is not None:
            target_id = hashlib.sha256(f"refused:{run_id}".encode()).hexdigest()[:32]
            bind.execute(
                sa.text(
                    "INSERT INTO collaboration_targets"
                    " (id, provider, project_ref, issue_iid, root_run_id, status,"
                    "  refusal_reason, provenance, created_at, updated_at)"
                    " VALUES (:id, 'gitlab', :project_ref, :issue_iid, :root,"
                    "  'refused', :reason, 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": target_id,
                    "project_ref": str(project_id),
                    "issue_iid": issue_iid,
                    "root": run_id,
                    "reason": refusal[:300],
                },
            )
            bind.execute(
                sa.text("UPDATE flow_runs SET target_id = :t WHERE id = :r"),
                {"t": target_id, "r": run_id},
            )
            continue
        target_id = branch_targets.get(key)
        if target_id is None:
            target_id = hashlib.sha256(":".join(key).encode()).hexdigest()[:32]
            single_mr = next(iter(group_mrs)) if len(group_mrs) == 1 else None
            bind.execute(
                sa.text(
                    "INSERT INTO collaboration_targets"
                    " (id, provider, project_ref, issue_iid, root_run_id,"
                    "  source_branch, target_branch, mr_iid, status, provenance,"
                    "  created_at, updated_at)"
                    " VALUES (:id, 'gitlab', :project_ref, :issue_iid, :root,"
                    "  :source, 'main', :mr, 'active', 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": target_id,
                    "project_ref": str(project_id),
                    "issue_iid": issue_iid,
                    "root": run_id,
                    "source": candidate,
                    "mr": single_mr,
                },
            )
            branch_targets[key] = target_id
        elif branch_mrs.get(key) and len(branch_mrs[key]) == 1:
            # A later sibling recorded the MR the group's row lacks.
            single_mr = next(iter(branch_mrs[key]))
            bind.execute(
                sa.text("UPDATE collaboration_targets SET mr_iid = :mr WHERE id = :t"),
                {"mr": single_mr, "t": target_id},
            )
        bind.execute(
            sa.text("UPDATE flow_runs SET target_id = :t WHERE id = :r"),
            {"t": target_id, "r": run_id},
        )


def downgrade() -> None:
    # Honest downgrade (the 030/031 precedent): refuse while reverting
    # would DESTROY linkage evidence. An active target row is the only
    # record that root and round children share one collaboration
    # surface — with independent child run ids (post-032 admissions)
    # dropping the rows would leave the children publishing to
    # re-derived, DIFFERENT branches. Archive explicitly before
    # downgrading.
    bind = op.get_bind()
    recorded = bind.execute(sa.text("SELECT count(*) FROM collaboration_targets")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"collaboration_targets holds {recorded} row(s) — the target linkage is "
            "branch-identity evidence and is never dropped by a downgrade; archive "
            "the rows explicitly (and accept independent child ids re-deriving "
            "different branches) before downgrading"
        )
    op.drop_index("ix_flow_runs_target_id", table_name="flow_runs")
    op.drop_column("flow_runs", "target_id")
    op.drop_index("uq_collaboration_target_branch", table_name="collaboration_targets")
    op.drop_index("ix_collaboration_targets_root_run_id", table_name="collaboration_targets")
    op.drop_table("collaboration_targets")
