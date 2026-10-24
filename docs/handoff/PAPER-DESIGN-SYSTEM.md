# Director and the tjCSL design system

Reference: [tjCSL Design System in Paper](https://app.paper.design/file/01M425GS017E3MQY3ERWW8S929/p-1-0).
The Foundations and Components artboards were read through Paper MCP using their exact CSS/JSX exports and design tokens, rather than estimating values from screenshots.

Director uses the system's white surface, #F2F2F2 wash, #D9D9D9 dividers, #111111 ink, #555555 secondary text, and semantic status colors. Local Geist and Geist Mono fonts remain bundled. Task headings use 28/32 semibold type; the Sites display heading uses 44/48 on desktop. Controls use 8px corners, containers 12px, and page content a 1120px maximum width. Inputs use gray fill and an inset focus border. Mobile controls retain at least 44px touch targets.

The original requested Director blue #087cfc is retained as a deliberate brand adaptation instead of Paper's #1F4BFF. White-label blue buttons use the darker #066bd7 shade for readable contrast. Text remains readable at small sizes; #555555 replaces the reference's low-contrast #8A8A8A metadata where needed. The people picker's selection marker stays a straight vertical line.

The owner requested a borderless header and softer weights: page headings are semibold, while section labels and controls use medium weight.

Action hierarchy follows the reference: one prominent blue task action, black secondary actions, gray utility controls, and outlined red deletion. Repeated request approvals and independent Settings saves are secondary. Existing routes, permission conditions, form validation, confirmations, editor preferences, and mobile navigation are preserved.

Visual verification uses the isolated sample preview at desktop, phone, and narrow-phone widths. These captures do not establish production login or hosting. Never present sample data as the owner's account.

## Stylesheet structure

`manager/director/static/tailwind/input.css` is the single source for these rules, ordered as Tokens, Base, Typography, Controls, Feedback, Navigation, Patterns, and Pages. Each selector is defined once in the section for its role; change a token or that one rule instead of adding an override further down. Rebuild `build.css` with `dev/tailwind/build-tailwind.sh` after editing.

| Token group    | Values                                                                                                                                                                                                                            |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Colour         | `--dt-bg`, `--dt-wash`, `--dt-wash-strong`, `--dt-ink`, `--dt-muted`, `--dt-disabled`, `--dt-border`, `--dt-border-strong`, brand (`--dt-brand`, `-strong`, `-hover`, `-tint`), and `--dt-danger`, `--dt-success`, `--dt-warning` |
| Type           | `--dt-text-xs` 12, `-sm` 14, `-control` 15, `-base` 16, `-lg` 18, `-xl` 22, `-2xl` 28, `-display` 44; medium 500 and semibold 600                                                                                                 |
| Space          | `--dt-space-1` to `--dt-space-16` on a 4px grid                                                                                                                                                                                   |
| Shape and size | `--dt-radius` 8px, `--dt-container-radius` 12px, `--dt-control-height` 40px (44px on phones), `--dt-control-height-sm` 32px (44px on phones)                                                                                      |
| Layout         | `--dt-page-width` 1120px, `--dt-prose-width` 680px, `--dt-gutter` 32/20/16px; the header and page share the gutter so their edges align                                                                                           |

Heading levels: page title (`h1`, 28px; 44px on Sites), page subtitle (`.dt-page-heading h2`, 22px), section title (`.dt-project-block > h2`, `.dt-settings-section > h2`, 18px), then 14px medium labels. A heading the site tabs already show uses `.dt-context-heading` so it remains available to screen readers without repeating visually.

Components: `.dt-btn-primary`, `-secondary`, `-outline`, `-danger`, with `.dt-btn-sm` for row actions; `.dt-form-field`; `.dt-disclosure` for chevron toggles (credentials, diagnostics, deletion); plus/minus toggles for inline editors; `.dt-status` dots with text; `.dt-project-block` and `.dt-settings-section` sections; `.dt-settings-access-list` rows; `.dt-table`; and `.dt-runtime-output` for logs and commands. Links inside paragraphs are underlined; standalone `.dt-inline-link` actions are quiet until hover.
