# Director screenshot handoff for Claude/Opus

These are current before-change screenshots from the static sample preview, captured 2026-10-03. No app changes were made for this review. No Claude/Opus call has been made.

**Review brief:** Review Director's UI only. Aim for a polished, modern, restrained SaaS interface inspired by Vercel LIGHT mode; keep original Director blue **#087cfc**, a text-only **Director** brand (no logo), a person's full name in the account trigger with no arrow, and the ease of tjcsl/director4. Retain every existing feature, route, form, action, permission condition, and informative warning. Give concrete minimal CSS/template changes ordered by impact. Reject decorative AI design tropes and overly stripped interfaces. Do not change authentication, backend behavior, deployment, or secrets. Read `recommendations.md` as a preliminary Codex review, then make your own independent judgment from the images.

Screenshot directory: `screenshots/`.

Desktop viewport: **1440×1000**. Mobile viewport: **390×844**. `--full` captures extend vertically for long pages. Account menu and file editor are separate interaction states.

| Page/state               | Desktop                   | Mobile                   | Preview route                    |
| ------------------------ | ------------------------- | ------------------------ | -------------------------------- |
| Member Sites             | sites-desktop.png         | sites-mobile.png         | `/`                              |
| Create                   | create-desktop.png        | create-mobile.png        | `/sites/create/`                 |
| Overview                 | overview-desktop.png      | overview-mobile.png      | `/sites/robotics-club/`          |
| Files initial            | files-desktop.png         | files-mobile.png         | `/sites/robotics-club/files/`    |
| Editor app.py            | editor-desktop.png        | editor-mobile.png        | Files → app.py                   |
| Settings                 | settings-desktop.png      | settings-mobile.png      | `/sites/robotics-club/settings/` |
| Database                 | database-desktop.png      | database-mobile.png      | `/sites/robotics-club/database/` |
| Terminal disconnected    | terminal-desktop.png      | terminal-mobile.png      | `/sites/robotics-club/terminal/` |
| Admin Sites              | admin-desktop.png         | admin-mobile.png         | `/preview/admin/`                |
| Admin account menu open  | account-admin-desktop.png | account-admin-mobile.png | Admin Sites → Sam Rivera         |
| Management               | management-desktop.png    | management-mobile.png    | `/preview/admin/management/`     |
| Governance requests      | governance-desktop.png    | governance-mobile.png    | `/preview/admin/requests/`       |
| Site image configuration | image-desktop.png         | image-mobile.png         | `/sites/robotics-club/image/`    |
| Image catalog            | images-desktop.png        | images-mobile.png        | `/preview/admin/images/`         |

Suggested review order: Sites → Overview → Settings → Files/editor → Create → Database/Terminal → Images/Management/Governance → open account menu. Compare each desktop/mobile pair. Current static preview sample banner is intentional; live server-only actions are unavailable here.

Do not use older `/workspace/work/director-redesign/` or `director-parity/` screenshots as the current implementation: some contain a removed logo/initial avatar or stale Account+arrow trigger.
