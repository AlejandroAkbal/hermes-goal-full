# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem.

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a
vulnerability"). If that is unavailable, contact the maintainer through the GitHub profile linked in
the repository metadata.

Include:

- What the issue is and why it matters.
- The Hermes version and the surface you used.
- A minimal reproduction if you have one.
- Whether the issue is in the plugin or in Hermes itself. If it is in Hermes, report it to
  [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) as well.

You can expect an acknowledgement within a few days. This is a small project without a security team,
so please be patient.

## What counts

- A way for a model or a crafted tool result to make the plugin remove or weaken a criterion without
  the judge's approval.
- A path traversal or injection in the sidecar or audit file handling.
- A gate command that runs with more privilege than the session already has.
- A way to make the plugin write outside `$HERMES_HOME/state/goalfull/`.

## What does not count

- The agent proposing a removal and the judge approving it. That is the designed behaviour; the audit
  log records it. If the judge is too permissive, that is a quality issue, and a report with a
  reproducible example is still useful.
- A gate you approved that ran a destructive command. Gates are shell commands with your permissions.
  Read them before approving.
- Private imports breaking on a new Hermes release. That is a compatibility issue, not a
  vulnerability.

## Scope

The plugin is a thin layer over Hermes' goal storage. Vulnerabilities in Hermes itself belong in the
Hermes repository. Vulnerabilities in the plugin's own code, state handling, or hook logic belong here.
