# Director5 local continuation

This branch contains the Director implementation, production authentication fixes,
deployment configuration, isolated preview tools, and the latest UI review with
28 desktop/mobile screenshots. Actual credentials and generated test data are
excluded.

## Continue locally

```bash
git clone --branch director5-handoff https://github.com/makors/director5.git
cd director5
codex mcp list
ssh-add -l
tmux list-sessions
codex
```

For an existing checkout, preserve any local changes before fetching and switching
to `origin/director5-handoff`. Configure the existing Claude Code MCP in the local
Codex client if it is currently registered only in another application.

## Next task: UI review with Opus

Read [the screenshot brief](ui-review/screenshot-manifest.md) and send the
desktop/mobile screenshots to Opus through Claude Code MCP. The accompanying
[recommendations](ui-review/recommendations.md) are an independent Codex review;
Claude/Opus has not been consulted. Incorporate the requested review before
deployment.

The UI goals are Vercel-inspired light mode, original Director blue `#087cfc`,
text-only Director branding, and the person's name in the account menu without
an arrow. Preserve every feature and permission condition. The current header
already has text-only branding and the full-name account trigger. The blue,
section hierarchy, and mobile refinements are still pending.

Use [the isolated preview tools](../../dev/ui-preview/README.md) to recreate sample
pages. The published preview at https://director-ui-preview.makors.chatgpt.site
shows sample UI, not live production authentication or hosting.

## Implementation and validation

[Director4 parity notes](../source/parity.md) describe the implemented features
and intentional differences. The latest Manager suite passed 411 tests after the
production settings and callback fixes. The Orchestrator suite previously passed
117 tests; real Docker, PostgreSQL, MySQL, file/editor, and Nginx smoke checks were
also completed during development. Repository pre-commit checks passed before
this handoff; the handoff packaging is checked separately.

Production settings now preserve environment-backed overrides, disable debug-only
tools in production, and retain the exact registered Ion callback without a
`redirect_state` query parameter. Live Ion sign-in and the target host's complete
production stack still require verification after deployment.

## Deployment after the UI review

Use the configured local SSH agent and existing tmux session named `director`.
The authorized destination is `bryce@135.148.41.70`. No remote server changes were
made from the cloud session: its local SSH agent/tmux were unavailable and direct
SSH connections were refused.

[Deployment instructions](../../dev/production/DEPLOYMENT.md) and the complete
configuration are under `dev/production/`. Copy that directory to the deployment
location described there, inspect the host, then configure private credentials on
the server. The `secret.py` in that directory contains environment references,
not secret values. The Ion client secret must be supplied privately by the owner;
it is not in Git.

The registered callback is:

```text
https://director.makors.xyz/social-auth/complete/ion/
```

The planned DNS records are `director.makors.xyz` and `*.sites.makors.xyz`, both
A records pointing to `135.148.41.70`. Confirm the server setup before asking the
owner to add them. Determine the owner's actual Ion username and email for the
administrator bootstrap; do not infer them from the SSH username. SMTP remains
to be configured if email features are needed.
