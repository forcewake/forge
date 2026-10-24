# .NET projects

The dedicated `dotnet-lane` driver and its language-specific CI recipe are
retired. There is no driver alias, automatic .NET image selection, bootstrap
shell or embedded restore/build/test tail.

Use [Claude Code](claude-code.md) or [Copilot CLI](copilot-cli.md) for approved
source changes. Keep your SDK and dependency pins in the target repository.
Configure its independent CI to build and test with that toolchain, and name
the required native checks in the repository's frozen verification policy.
Lane completion does not substitute for those checks.

Use the current [onboarding instructions](onboarding.md) for GitHub or GitLab.
