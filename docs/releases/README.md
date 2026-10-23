# Releases

The current source release implementation is documented in the
[release runbook](../operations/release.md). It builds once, verifies
installed wheels and the exact image, requires both native provider
smokes, signs and attests the artifacts, and promotes the same digests.
Source tests do not prove that this hosted flow has run successfully.

Current changes are in the [changelog](../../CHANGELOG.md).
Historical profile formats, promotion records and qualification narratives
are recoverable from [the archive](../archive.md), including original
paths `docs/releases/profile-records.md` and `docs/releases/evidence/`.
Publishing a tag, prerelease or final release remains an explicitly
authorized owner action.
