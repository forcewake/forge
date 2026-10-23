"""Additive engine tables, separate from legacy lifecycle ownership."""

import json

import sqlalchemy as sa
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None

# Frozen migration input, deliberately independent of the application's models.
_SCHEMA = r"""
[
["eng_runs",[
["id","String",[32],false,true,null,null],
["provider","String",[20],false,false,null,null],
["connection_id","String",[100],false,false,null,null],
["repository_id","String",[100],false,false,null,null],
["subject_kind","String",[20],false,false,null,null],
["subject_id","String",[100],false,false,null,null],
["subject_key","String",[600],false,false,null,null],
["state","String",[32],false,false,"'queued'",null],
["failed_handler","String",[32],true,false,null,null],
["reason","String",[300],true,false,null,null],
["root_run_id","String",[32],false,false,null,null],
["parent_run_id","String",[32],true,false,null,"eng_runs.id"],
["attempt","Integer",[],false,false,"'1'",null],
["generation","Integer",[],false,false,"'1'",null],
["cancellation_generation","Integer",[],false,false,"'0'",null],
["cancel_requested","Boolean",[],false,false,"false",null],
["version","Integer",[],false,false,"'1'",null],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","attempt > 0 AND generation > 0 AND cancellation_generation >= 0 AND version > 0","ck_eng_run_counters"],["check","provider IN ('github','gitlab')","ck_eng_run_provider"],["check","state IN ('queued','planning','awaiting_approval','running','publishing','verifying','reviewing','ready_for_human','blocked','failed','cancelled')","ck_eng_run_state"],["check","subject_kind IN ('issue','review')","ck_eng_run_subject_kind"]],[["ix_eng_runs_subject_key",["subject_key"],false,null],["uq_eng_active_subject",["subject_key"],true,"state NOT IN ('ready_for_human', 'blocked', 'failed', 'cancelled')"]]],
["eng_approved_inputs",[
["id","String",[32],false,true,null,null],
["revision","Integer",[],false,false,null,null],
["digest","String",[64],false,false,null,null],
["predecessor_digest","String",[64],true,false,null,null],
["spec_digest","String",[64],false,false,null,null],
["base_sha","String",[64],false,false,null,null],
["document_json","Text",[],false,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","revision > 0","ck_eng_approved_revision"],["unique",["run_id","revision"],"uq_eng_approved_revision"]],[["ix_eng_approved_inputs_run_id",["run_id"],false,null]]],
["eng_budget_pools",[
["id","String",[32],false,true,null,null],
["purpose","String",[20],false,false,null,null],
["usd","Money",[],false,false,null,null],
["calls","BigInteger",[],false,false,null,null],
["tokens","BigInteger",[],false,false,null,null],
["version","Integer",[],false,false,"'1'",null],
["state_json","Text",[],false,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","usd >= 0 AND calls >= 0 AND tokens >= 0 AND version > 0","ck_eng_pool_capacity"],["check","purpose IN ('implementation','closing')","ck_eng_pool_purpose"],["unique",["run_id","purpose"],"uq_eng_pool_purpose"]],[["ix_eng_budget_pools_run_id",["run_id"],false,null]]],
["eng_candidates",[
["id","String",[32],false,true,null,null],
["attempt","Integer",[],false,false,null,null],
["generation","Integer",[],false,false,null,null],
["digest","String",[64],false,false,null,null],
["base_sha","String",[64],false,false,null,null],
["approved_digest","String",[64],false,false,null,null],
["manifest_json","Text",[],false,false,null,null],
["state","String",[20],false,false,"'received'",null],
["validation_json","Text",[],true,false,null,null],
["published_sha","String",[64],true,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","state IN ('received','validated','refused','published')","ck_eng_candidate_state"],["unique",["run_id","attempt","generation"],"uq_eng_candidate_dispatch"]],[["ix_eng_candidates_run_id",["run_id"],false,null]]],
["eng_checkpoints",[
["id","String",[64],false,true,null,null],
["attempt","Integer",[],false,false,null,null],
["generation","Integer",[],false,false,null,null],
["digest","String",[64],false,false,null,null],
["base_sha","String",[64],false,false,null,null],
["approved_digest","String",[64],false,false,null,null],
["driver","String",[100],false,false,null,null],
["manifest_json","Text",[],false,false,null,null],
["retained","Boolean",[],false,false,"true",null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["unique",["run_id","attempt","generation","digest"],"uq_eng_checkpoint_identity"]],[["ix_eng_checkpoints_run_id",["run_id"],false,null]]],
["eng_control_commands",[
["id","String",[32],false,true,null,null],
["command_key","String",[300],false,false,null,null],
["attempt","Integer",[],false,false,null,null],
["generation","Integer",[],false,false,null,null],
["sequence","Integer",[],false,false,null,null],
["world_version","Integer",[],false,false,null,null],
["payload_json","Text",[],false,false,null,null],
["state","String",[20],false,false,"'pending'",null],
["deadline_at","DateTime",[],false,false,null,null],
["acknowledgement_json","Text",[],true,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","state IN ('pending','acknowledged','refused','expired')","ck_eng_control_state"],["unique",["run_id","command_key"],"uq_eng_control_key"],["unique",["run_id","attempt","sequence"],"uq_eng_control_sequence"]],[["ix_eng_control_commands_run_id",["run_id"],false,null]]],
["eng_gates",[
["id","String",[32],false,true,null,null],
["decision_key","String",[200],false,false,null,null],
["state","String",[20],false,false,"'open'",null],
["approved_digest","String",[64],false,false,null,null],
["actor_scope_json","Text",[],false,false,null,null],
["expires_at","DateTime",[],false,false,null,null],
["consumed_at","DateTime",[],true,false,null,null],
["consumed_by","String",[200],true,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","state IN ('open','consumed','expired','refused')","ck_eng_gate_state"],["unique",["run_id","decision_key"],"uq_eng_gate_decision"]],[["ix_eng_gates_run_id",["run_id"],false,null]]],
["eng_gateway_keys",[
["id","String",[32],false,true,null,null],
["alias","String",[200],false,false,null,null],
["attempt","Integer",[],false,false,null,null],
["generation","Integer",[],false,false,null,null],
["ciphertext","LargeBinary",[],true,false,null,null],
["aad_digest","String",[64],false,false,null,null],
["expires_at","DateTime",[],false,false,null,null],
["state","String",[20],false,false,"'reserved'",null],
["block_effect_key","String",[300],true,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["unique",["alias"],null],["check","state <> 'purged' OR ciphertext IS NULL","ck_eng_key_purged"],["check","state IN ('reserved','active','unknown','blocked','purged')","ck_eng_key_state"],["unique",["run_id","attempt","generation"],"uq_eng_key_dispatch"]],[["ix_eng_gateway_keys_run_id",["run_id"],false,null]]],
["eng_lane_sessions",[
["id","String",[32],false,true,null,null],
["attempt","Integer",[],false,false,null,null],
["generation","Integer",[],false,false,null,null],
["cancellation_generation","Integer",[],false,false,null,null],
["nonce_hash","String",[64],false,false,null,null],
["nonce_consumed","Boolean",[],false,false,"false",null],
["expires_at","DateTime",[],false,false,null,null],
["dispatch_json","Text",[],false,false,null,null],
["brief_json","Text",[],false,false,null,null],
["provider_binding_json","Text",[],false,false,null,null],
["first_job_json","Text",[],true,false,null,null],
["api_state_json","Text",[],false,false,"'{}'",null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["unique",["nonce_hash"],null],["check","attempt > 0 AND generation > 0 AND cancellation_generation >= 0","ck_eng_session_counters"],["unique",["run_id","attempt","generation"],"uq_eng_session_dispatch"]],[["ix_eng_lane_sessions_run_id",["run_id"],false,null]]],
["eng_review_rounds",[
["id","String",[32],false,true,null,null],
["root_run_id","String",[32],false,false,null,"eng_runs.id"],
["parent_run_id","String",[32],false,false,null,"eng_runs.id"],
["child_run_id","String",[32],false,false,null,"eng_runs.id"],
["root_review_ref","String",[600],false,false,null,null],
["feedback_key","String",[300],false,false,null,null],
["base_sha","String",[64],false,false,null,null],
["state","String",[20],false,false,"'open'",null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["unique",["child_run_id"],null],["check","state IN ('open','closed','refused')","ck_eng_round_state"],["unique",["root_run_id","feedback_key"],"uq_eng_round_feedback"]],[["ix_eng_review_rounds_run_id",["run_id"],false,null],["uq_eng_round_slot",["root_review_ref"],true,"state = 'open'"]]],
["eng_run_specs",[
["id","String",[32],false,true,null,null],
["digest","String",[64],false,false,null,null],
["document_json","Text",[],false,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["unique",["run_id"],"uq_eng_spec_run"]],[["ix_eng_run_specs_run_id",["run_id"],false,null]]],
["eng_steps",[
["id","String",[32],false,true,null,null],
["key","String",[300],false,false,null,null],
["handler","String",[32],false,false,null,null],
["payload_json","Text",[],false,false,null,null],
["state","String",[20],false,false,"'scheduled'",null],
["due_at","DateTime",[],false,false,null,null],
["deadline_at","DateTime",[],true,false,null,null],
["attempts","Integer",[],false,false,"'0'",null],
["max_attempts","Integer",[],false,false,"'3'",null],
["lease_epoch","Integer",[],false,false,"'0'",null],
["lease_owner","String",[200],true,false,null,null],
["lease_expires_at","DateTime",[],true,false,null,null],
["attempt","Integer",[],false,false,null,null],
["generation","Integer",[],false,false,null,null],
["cancellation_generation","Integer",[],false,false,null,null],
["result_json","Text",[],true,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["unique",["key"],null],["check","attempts >= 0 AND max_attempts > 0 AND lease_epoch >= 0","ck_eng_step_counts"],["check","handler IN ('plan','approve','dispatch','await_lane','publish','verify','review','ready')","ck_eng_step_handler"],["check","(state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR state <> 'running'","ck_eng_step_lease"],["check","state IN ('scheduled','running','succeeded','failed','cancelled','dead')","ck_eng_step_state"]],[["ix_eng_steps_due",["state","due_at"],false,null],["ix_eng_steps_run_id",["run_id"],false,null]]],
["eng_usage",[
["id","String",[32],false,true,null,null],
["source_key","String",[300],false,false,null,null],
["attempt","Integer",[],false,false,null,null],
["generation","Integer",[],false,false,null,null],
["alias","String",[200],true,false,null,null],
["completeness","String",[20],false,false,null,null],
["final","Boolean",[],false,false,"false",null],
["usd","Money",[],true,false,null,null],
["calls","BigInteger",[],true,false,null,null],
["tokens","BigInteger",[],true,false,null,null],
["receipt_json","Text",[],false,false,null,null],
["conflict_json","Text",[],true,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","completeness IN ('exact','aggregate','partial','unknown','unmetered')","ck_eng_usage_completeness"],["check","(usd IS NULL OR usd >= 0) AND (calls IS NULL OR calls >= 0) AND (tokens IS NULL OR tokens >= 0)","ck_eng_usage_nonnegative"],["unique",["source_key"],"uq_eng_usage_source"]],[["ix_eng_usage_run_id",["run_id"],false,null]]],
["eng_blobs",[
["id","String",[32],false,true,null,null],
["attempt","Integer",[],false,false,null,null],
["generation","Integer",[],false,false,null,null],
["digest","String",[64],false,false,null,null],
["size","Integer",[],false,false,null,null],
["storage_ref","String",[500],false,false,null,null],
["checkpoint_id","String",[64],true,false,null,"eng_checkpoints.id"],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","size >= 0","ck_eng_blob_size"],["unique",["run_id","attempt","generation","digest"],"uq_eng_blob_authorization"]],[["ix_eng_blobs_digest",["digest"],false,null],["ix_eng_blobs_run_id",["run_id"],false,null]]],
["eng_budget_amendments",[
["id","String",[32],false,true,null,null],
["pool_id","String",[32],false,false,null,"eng_budget_pools.id"],
["command_key","String",[300],false,false,null,null],
["actor","String",[200],false,false,null,null],
["reason","String",[300],false,false,null,null],
["amount_json","Text",[],false,false,null,null],
["state","String",[20],false,false,null,null],
["gateway_effect_key","String",[300],true,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","state IN ('authorized','applied','refused','unknown')","ck_eng_amendment_state"],["unique",["run_id","command_key"],"uq_eng_amendment_command"]],[["ix_eng_budget_amendments_run_id",["run_id"],false,null]]],
["eng_budget_reservations",[
["id","String",[32],false,true,null,null],
["pool_id","String",[32],false,false,null,"eng_budget_pools.id"],
["reservation_key","String",[300],false,false,null,null],
["requested_json","Text",[],false,false,null,null],
["minimum_json","Text",[],false,false,null,null],
["allowance_json","Text",[],false,false,null,null],
["state","String",[20],false,false,null,null],
["observed_usd","Money",[],true,false,null,null],
["observed_calls","BigInteger",[],true,false,null,null],
["observed_tokens","BigInteger",[],true,false,null,null],
["version","Integer",[],false,false,"'1'",null],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["check","state IN ('reserved','accrued','unknown','settled')","ck_eng_reservation_state"],["unique",["pool_id","reservation_key"],"uq_eng_reservation_key"]],[["ix_eng_budget_reservations_pool_id",["pool_id"],false,null]]],
["eng_effects",[
["id","String",[32],false,true,null,null],
["key","String",[300],false,false,null,null],
["kind","String",[100],false,false,null,null],
["step_id","String",[32],false,false,null,"eng_steps.id"],
["lease_epoch","Integer",[],false,false,null,null],
["payload_digest","String",[64],false,false,null,null],
["payload_json","Text",[],false,false,null,null],
["state","String",[20],false,false,"'requested'",null],
["version","Integer",[],false,false,"'1'",null],
["outcome_json","Text",[],true,false,null,null],
["observations_json","Text",[],false,false,"'[]'",null],
["repo_key","String",[400],true,false,null,null],
["head_branch","String",[255],true,false,null,null],
["dispatched_at","DateTime",[],true,false,null,null],
["dispatch_deadline_at","DateTime",[],true,false,null,null],
["run_id","String",[32],false,false,null,"eng_runs.id"],
["created_at","DateTime",[],false,false,"@now",null],
["updated_at","DateTime",[],false,false,"@now",null]
],[["unique",["key"],null],["check","version > 0 AND lease_epoch > 0","ck_eng_effect_counters"],["check","kind <> 'ensure_draft' OR (repo_key IS NOT NULL AND head_branch IS NOT NULL)","ck_eng_effect_draft_identity"],["check","state IN ('requested','dispatched','committed','adopted','conflict','failed','unknown')","ck_eng_effect_state"]],[["ix_eng_effect_unresolved",["state","updated_at"],false,null],["ix_eng_effects_run_id",["run_id"],false,null],["uq_eng_effect_draft",["repo_key","head_branch"],true,"kind = 'ensure_draft'"]]]
]
"""


def _column(spec):
    name, kind, args, nullable, primary, default, foreign = spec
    types = {
        "String": sa.String,
        "Integer": sa.Integer,
        "BigInteger": sa.BigInteger,
        "Money": lambda: (
            sa.Text() if op.get_bind().dialect.name == "sqlite" else sa.Numeric(38, 18)
        ),
        "Text": sa.Text,
        "DateTime": lambda: sa.DateTime(timezone=True),
        "Boolean": sa.Boolean,
        "Numeric": sa.Numeric,
        "LargeBinary": sa.LargeBinary,
    }
    references = [sa.ForeignKey(foreign)] if foreign else []
    kwargs = {"nullable": nullable, "primary_key": primary}
    if default is not None:
        kwargs["server_default"] = sa.func.now() if default == "@now" else sa.text(default)
    return sa.Column(name, types[kind](*args), *references, **kwargs)


def upgrade() -> None:
    for name, columns, constraints, indices in json.loads(_SCHEMA):
        parts = [_column(spec) for spec in columns]
        for kind, value, constraint_name in constraints:
            parts.append(
                sa.CheckConstraint(value, name=constraint_name)
                if kind == "check"
                else sa.UniqueConstraint(*value, name=constraint_name)
            )
        op.create_table(name, *parts)
        for index_name, fields, unique, predicate in indices:
            where = (
                {"sqlite_where": sa.text(predicate), "postgresql_where": sa.text(predicate)}
                if predicate
                else {}
            )
            op.create_index(index_name, name, fields, unique=unique, **where)


def downgrade() -> None:
    bind = op.get_bind()
    for name, *_ in json.loads(_SCHEMA):
        if bind.execute(sa.text(f'SELECT 1 FROM "{name}" LIMIT 1')).first() is not None:
            raise RuntimeError("engine history exists; export and explicit cutover are required")
    for name, *_ in reversed(json.loads(_SCHEMA)):
        op.drop_table(name)
