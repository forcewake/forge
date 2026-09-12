from __future__ import annotations

import fnmatch
import logging

from forge.agents.base import ForgeAgent
from forge.llm.prompts import build_review_user_message

logger = logging.getLogger(__name__)


class CodeReviewAgent(ForgeAgent):
    """Concrete agent that reviews merge request diffs.

    Uses the GitLab toolkit to post inline comments during execution
    and returns a structured ReviewResult for the summary.
    """

    def _build_user_message(self) -> str:
        """Assemble the review user prompt, filtering out skip_paths."""
        ctx = self.context
        skip_paths = self.project_config.skip_paths

        # If there are skip_paths, filter the raw diff to exclude matching files
        if skip_paths and ctx.raw_diff:
            ctx = self._filter_context(ctx, skip_paths)

        return build_review_user_message(ctx, rules=self.project_config.review_rules)

    def _filter_context(self, ctx, skip_paths: list[str]):
        """Return a new context with files matching skip_paths removed from the diff.

        Uses fnmatch for glob-pattern matching against file paths.
        """
        filtered_diff = []
        for file_diff in ctx.parsed_diff:
            path = file_diff.new_path or file_diff.old_path
            if any(fnmatch.fnmatch(path, pattern) for pattern in skip_paths):
                logger.debug("Skipping file %s (matches skip_paths)", path)
                continue
            filtered_diff.append(file_diff)

        if len(filtered_diff) == len(ctx.parsed_diff):
            return ctx  # Nothing filtered

        # Rebuild raw_diff from filtered parsed diffs
        # We filter by removing file sections from raw_diff
        filtered_paths = {fd.new_path for fd in filtered_diff} | {
            fd.old_path for fd in filtered_diff
        }

        raw_lines = ctx.raw_diff.split("\n")
        filtered_raw_lines: list[str] = []
        include = True

        for line in raw_lines:
            if line.startswith("diff --git"):
                # Extract file paths from diff header
                # Format: diff --git a/path b/path
                parts = line.split()
                if len(parts) >= 4:
                    b_path = parts[3].removeprefix("b/")
                    include = b_path in filtered_paths
                else:
                    include = True
            if include:
                filtered_raw_lines.append(line)

        # Return a new context with filtered data
        # AgentContext is frozen, so we need model_copy
        return ctx.model_copy(
            update={
                "raw_diff": "\n".join(filtered_raw_lines),
                "parsed_diff": filtered_diff,
            }
        )
