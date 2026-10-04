# Director and the tjCSL design system

Reference: [tjCSL Design System in Paper](https://app.paper.design/file/01M425GS017E3MQY3ERWW8S929/p-1-0).
The Foundations and Components artboards were read through Paper MCP using their exact CSS/JSX exports and design tokens, rather than estimating values from screenshots.

Director uses the system's white surface, #F2F2F2 wash, #D9D9D9 dividers, #111111 ink, #555555 secondary text, and semantic status colors. Local Geist and Geist Mono fonts remain bundled. Task headings use 28/32 semibold type; the Sites display heading uses 44/48 on desktop. Controls use 8px corners, containers 12px, and page content a 1120px maximum width. Inputs use gray fill and an inset focus border. Mobile controls retain at least 44px touch targets.

The original requested Director blue #087cfc is retained as a deliberate brand adaptation instead of Paper's #1F4BFF. White-label blue buttons use the darker #066bd7 shade for readable contrast. Text remains readable at small sizes; #555555 replaces the reference's low-contrast #8A8A8A metadata where needed. The people picker's selection marker stays a straight vertical line.

The owner requested a borderless header and softer weights: page headings are semibold, while section labels and controls use medium weight.

Action hierarchy follows the reference: one prominent blue task action, black secondary actions, gray utility controls, and outlined red deletion. Repeated request approvals and independent Settings saves are secondary. Existing routes, permission conditions, form validation, confirmations, editor preferences, and mobile navigation are preserved.

Visual verification uses the isolated sample preview at desktop, phone, and narrow-phone widths. These captures do not establish production login or hosting. Never present sample data as the owner's account.
