# Maintenance line 0.42.x

Azure DevOps and the pre-simplification architecture are preserved on
`release/0.42.x`. It is a local recovery branch; this source change does not
claim it has been pushed or passed current hosted CI. No recovery tag or
remote publication is implied. Exact recovery availability must be checked
in the owner's clone before changing an installation.

The line is for security fixes, not new features. Keep its own lane pins,
qualification/release tooling and schema policy together; newer main's
workflow is not automatically compatible. Read a removed file with
`git show release/0.42.x:<original-path>` and restore into a separate review
branch rather than reintroducing Azure into the active product.

Backports and any 0.42.N release require explicit owner publication
instructions. Preserve the source commit provenance, run that line's gates
and inspect hosted results on the exact commit before releasing. Prior
nightly `integration-os` and `release-canary` failures at the branch point
are historical failures to diagnose, never permission to weaken checks.

Before a maintenance release, ensure its image tags and upgrade canary name
the previous 0.42.x artifacts and cannot move the main line's latest image
or GitHub latest release. Preserve tested artifact identity and the branch's
own promotion requirements. The complete original maintenance procedure
remains at `docs/operations/maintenance-0.42.md` in the
[documentation archive](../archive.md).
