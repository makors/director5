(() => {
	let busy = false;
	async function refresh() {
		if (busy || document.hidden) return;
		const field = document.querySelector("[data-runtime-status-url]");
		if (!field) return;
		busy = true;
		try {
			const response = await fetch(field.dataset.runtimeStatusUrl, {
				headers: { Accept: "application/json" },
				credentials: "same-origin",
			});
			const result = await response.json();
			const states = {
				running: "Running",
				degraded: "Degraded",
				stopped: "Stopped",
				starting: "Starting",
				failed: "Failed",
				absent: "Not deployed",
				unavailable: "Unavailable",
				missing: "Not deployed",
			};
			field.textContent = response.ok ? states[result.state] || "Unavailable" : "Unavailable";
		} catch {
			field.textContent = "Unavailable";
		} finally {
			busy = false;
		}
	}
	refresh();
	document.addEventListener("htmx:wsAfterMessage", refresh);
	document.addEventListener("visibilitychange", refresh);
	window.setInterval(refresh, 15000);
})();
