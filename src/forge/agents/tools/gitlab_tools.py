from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from agno.tools import Toolkit

if TYPE_CHECKING:
    from forge.gitlab.client import GitLabClient
    from forge.gitlab.schemas import DiffRefs

logger = logging.getLogger(__name__)


class GitLabToolkit(Toolkit):
    """Tools for interacting with GitLab merge requests during agent execution.

    Provides inline commenting, general notes, label management, and file fetching.
    The toolkit tracks created discussion IDs for later reference.
    """

    def __init__(
        self,
        gitlab: GitLabClient,
        project_id: int,
        mr_iid: int,
        diff_refs: DiffRefs | None = None,
    ) -> None:
        super().__init__(name="gitlab")
        self.gitlab = gitlab
        self.project_id = project_id
        self.mr_iid = mr_iid
        self.diff_refs = diff_refs
        self.discussion_ids: list[str] = []

        # Register our async methods as tools
        self.register(self.post_inline_comment)
        self.register(self.post_note)
        self.register(self.add_label)
        self.register(self.remove_label)
        self.register(self.get_file_content)

    async def post_inline_comment(
        self,
        file: str,
        line: int,
        line_type: str,
        body: str,
        suggestion: str = "",
    ) -> str:
        """Post a review comment on a specific file and line in the merge request diff.

        Args:
            file: Path to the file in the repository.
            line: Line number in the diff.
            line_type: 'new' for added lines or 'old' for removed lines.
            body: The review comment text.
            suggestion: Optional corrected code to suggest.

        Returns:
            Confirmation message with discussion ID.
        """
        if not self.diff_refs or not self.diff_refs.base_sha:
            # Fall back to a general note if diff refs unavailable
            return await self.post_note(f"**[{file}:{line}]** {body}")

        # Format body with suggestion block if provided
        full_body = body
        if suggestion:
            full_body += f"\n\n```suggestion:-0+0\n{suggestion}\n```"

        position: dict = {
            "position_type": "text",
            "base_sha": self.diff_refs.base_sha,
            "head_sha": self.diff_refs.head_sha,
            "start_sha": self.diff_refs.start_sha,
            "new_path": file,
            "old_path": file,
        }
        if line_type == "new":
            position["new_line"] = line
        else:
            position["old_line"] = line

        try:
            discussion = await self.gitlab.create_mr_discussion(
                self.project_id,
                self.mr_iid,
                full_body,
                position=position,
            )
            self.discussion_ids.append(discussion.id)
            return f"Posted inline comment on {file}:{line} (discussion {discussion.id})"
        except Exception as exc:
            logger.warning(
                "Failed to post inline comment on %s:%d — falling back to note: %s",
                file,
                line,
                exc,
            )
            # Fall back to a general note
            return await self.post_note(f"**[{file}:{line}]** {body}")

    async def post_note(self, body: str) -> str:
        """Post a general comment on the merge request.

        Args:
            body: The comment text (markdown supported).

        Returns:
            Confirmation message with note ID.
        """
        note = await self.gitlab.create_mr_note(self.project_id, self.mr_iid, body)
        return f"Posted note (id={note.id})"

    async def add_label(self, label: str) -> str:
        """Add a label to the merge request.

        Args:
            label: The label name to add.

        Returns:
            Confirmation message.
        """
        await self.gitlab.add_mr_labels(self.project_id, self.mr_iid, [label])
        return f"Added label '{label}'"

    async def remove_label(self, label: str) -> str:
        """Remove a label from the merge request.

        Args:
            label: The label name to remove.

        Returns:
            Confirmation message.
        """
        await self.gitlab.remove_mr_labels(self.project_id, self.mr_iid, [label])
        return f"Removed label '{label}'"

    async def get_file_content(self, file_path: str, ref: str = "HEAD") -> str:
        """Fetch the content of a file from the repository.

        Args:
            file_path: Path to the file in the repository.
            ref: Git ref (branch, tag, or commit SHA). Defaults to HEAD.

        Returns:
            The file content as text.
        """
        repo_file = await self.gitlab.get_file(self.project_id, file_path, ref)
        if repo_file.encoding == "base64":
            return base64.b64decode(repo_file.content).decode("utf-8", errors="replace")
        return repo_file.content
