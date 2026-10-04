(() => {
	const drafts = new WeakMap();

	document.addEventListener("htmx:wsBeforeMessage", (event) => {
		const status = document.getElementById("site-status");
		const focusedControl = document.activeElement?.closest("[data-dashboard-focus]");
		const input = document.getElementById("delete-confirmation");
		const disclosure = document.getElementById("delete-site-disclosure");
		drafts.set(event.target, {
			sectionOpen: status?.querySelector(".dt-mobile-section")?.open || false,
			focusKey: status?.contains(focusedControl) ? focusedControl.dataset.dashboardFocus : null,
			confirmation:
				input && disclosure
					? {
							value: input.value,
							open: disclosure.open,
							focused: document.activeElement === input,
							selectionStart: input.selectionStart,
							selectionEnd: input.selectionEnd,
						}
					: null,
		});
	});

	document.addEventListener("htmx:wsAfterMessage", (event) => {
		const draft = drafts.get(event.target);
		drafts.delete(event.target);
		if (!draft) return;

		const input = document.getElementById("delete-confirmation");
		const disclosure = document.getElementById("delete-site-disclosure");
		if (draft.confirmation && input && disclosure) {
			input.value = draft.confirmation.value;
			disclosure.open = draft.confirmation.open;
		}

		const status = document.getElementById("site-status");
		const section = status?.querySelector(".dt-mobile-section");
		if (section) section.open = draft.sectionOpen;
		const focusedControl = Array.from(
			status?.querySelectorAll("[data-dashboard-focus]") || [],
		).find((control) => control.dataset.dashboardFocus === draft.focusKey);
		if (
			focusedControl &&
			!focusedControl.matches(":disabled") &&
			focusedControl.getAttribute("aria-disabled") !== "true"
		) {
			focusedControl.focus({ preventScroll: true });
			if (draft.confirmation?.focused && focusedControl === input) {
				input.setSelectionRange(draft.confirmation.selectionStart, draft.confirmation.selectionEnd);
			}
		}
	});
})();
