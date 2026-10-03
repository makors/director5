# Opus review and implementation

On October 3, 2026, the configured Claude Code MCP was called with model `opus`.
Its response identified the resolved model as `claude-opus-5-5` and agent
`a3e62cd5404eac36b`. It confirmed that it opened and visually inspected all 28
original PNG screenshots, along with the manifest and independent recommendations.
This supersedes the earlier handoff's statement that Opus had not been consulted.

## Screenshot coverage confirmed by Opus

Desktop and mobile pairs: sites, create, overview, files, editor, settings,
database, terminal, admin, account-admin, management, governance, image, images.
All files loaded. Opus identified that the original files-mobile and editor-mobile
captures were duplicates; the updated captures now show these states separately.

## Review findings incorporated

- Director blue `#087cfc` for focus, selected tabs/files, radios and progress;
  darker `#066bd7` primary button fill for white-text contrast. Light mode remains.
- Text-only Director branding and the person's name without an account arrow.
- Bordered site list, semantic status dots with text, stronger heading hierarchy,
  distinct Activity and Site details sections, restrained settings/database/admin
  sections, readable form widths and spacing.
- Shared site identity partial for settings, files, databases and runtime pages.
  Overview retains its action-bearing header and permission-sensitive controls.
- Mobile current-tab visibility and overflow cues. The site title remains visible
  in the page header; the redundant narrow-screen site breadcrumb stays hidden
  to preserve room for the person's name.
- Create description before the required agreement; checkbox and label together;
  usable member multiselect and Guidelines link alongside form actions.
- Selected-file treatment, matching ZIP action, mobile Back to files, scroll to
  user-opened editor, no focus jump when restoring tabs, 44px mobile controls and
  aligned language/Reload/Save actions. All editor capabilities are retained.
- Terminal overflow clipping corrected, SSH link separated, and real disconnected
  guidance kept outside shell output. No simulated terminal output.
- Bounded, scrollable account menu with separators; clearer image/admin/request
  actions and labels. Existing warnings and permission conditions remain.

## Verification

- Manager: all 412 tests passed across a full run and the rerun of one test whose
  loopback socket was initially denied by the local sandbox.
- Current-tree focused listing/creation, images, governance and runtime: 91 passed.
- Current-tree Files suite: 24 passed.
- Orchestrator on the inspected Linux server: 119 passed, including the two real
  disposable Nginx validation tests. The earlier macOS-only filesystem failures
  do not reproduce on Linux.
- Repository pre-commit checks passed on changed code.
- Browser: actual Django preview search/filter/empty results, account Escape/focus,
  editor open/selection, preferences persistence and Back to files verified.
- Updated sample screenshots: all 14 states at 1440x1000, 390x844 and 320x844;
  additional mobile editor viewport captures. No horizontal page overflow or
  clipped current tab in the 36 page/viewport measurements.

## Deployment boundary

The destination `bryce@135.148.41.70` was inspected before changes. Its unrelated
Webtop container on port 3000 and persistent volume were preserved. Ports 80, 443
and 2222 were free; no Director installation or site data directories existed.
Source and a private mode-600 environment file were staged in
`/home/bryce/director5-staging`. Actual credentials are never stored in this repo.

Production services are now deployed. Ion credentials and admin identity were
saved privately in the server environment; sudo authentication was supplied in
the existing director tmux deployment window. DNS records `director` and
`*.sites` now point to `135.148.41.70`, DNS-only, with no conflicting AAAA.

## Updated screenshot follow-up

A second explicit Opus MCP call (resolved model `claude-opus-5-5`, agent
`ab3b9ec6757cb2dfb`) visually inspected ten updated screenshots: desktop Overview
and Terminal; mobile Settings, Create, initial Files, Database and Images; mobile
and 320px editor viewports; and the 320px administrator account menu. It reported
no release-blocking visual or usability regressions. It confirmed that initial
Files now shows the empty editor state and that editor footer controls fit at
both 390px and 320px. Partially visible inactive tabs and horizontally scrolling
code/connection fields were considered expected behavior.

## Production verification completed

The production image `director5-production:local` was built from UI commit
`dc13dc5`. Docker Compose and Buildx were installed without upgrading or removing
other packages. Production services are running; Manager, Orchestrator and data
services report healthy. The unrelated Webtop service remains running.

The initial Traefik 3.2 image could not negotiate Docker 29's API. Only Director's
proxy was updated to pinned 3.7.13, preserving ACME storage and all application
containers. Valid public HTTPS works for Manager and hosted static/dynamic sites.

- Real Ion login completed in the user's browser. Bryce Conrad is associated with
  Ion and retains administrator access. The account menu shows the user's name.
- Temporary static and dynamic sites passed file save/read, binary upload/download,
  restart and rebuild. Exact expected content was fetched over verified public TLS
  from the user's Mac for both sites.
- PostgreSQL and MySQL each passed creation, query, password rotation and deletion.
- Public WebSocket and external SSH sessions executed verified shell commands.
  SSH used a host key obtained over the trusted administrative SSH connection.
- A live browser terminal on the user's devclub site connected and executed a
  harmless printf command, then disconnected. No site files were changed.
- Test sites 1/2 and the temporary test user were deleted through normal operations;
  short-lived test sessions/tickets were revoked. User-created devclub was preserved.
- Live production desktop/mobile captures are included separately from the original
  sample-data gallery.

## Remaining limitations and deliberate differences

SMTP delivery is not configured or live-tested, so notification and mass-email
capabilities are not operationally verified. Governance/teacher workflows and
custom-domain scenarios have automated coverage but were not exercised with real
users/domains in production. No claim of exhaustive live feature parity is made.

Requests from the Manager container back to the server's public hostname timed
out during smoke tests. Equivalent external HTTPS, WSS and SSH tests passed;
container-to-public-address routing remains an environment limitation to investigate
if applications need that path. Internal service/database operations passed.

Director5 uses one-time SSH credentials rather than Director4 Kerberos, and shells
run in disposable site workspaces rather than attaching to the serving container.
Failed-operation recovery deliberately refuses active/queued operations.

Credentials were not committed. Source changes are committed locally; no push was
performed. See dev/production/DEPLOYMENT.md for deployment and recovery procedures.
