(() => {
	// A user may close an editor while its request is pending. Reveal the
	// returned form before the shared error handler moves focus into it.
	document.addEventListener("htmx:beforeSwap", (event) => {
		const target = event.detail.target;
		if (!target?.matches('form[id^="settings-"]')) return;
		const editor = target.closest("details.dt-settings-edit");
		if (editor) editor.open = true;
	});
})();
