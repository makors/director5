(() => {
	const data = window.directorPreviewData || { files: {}, docs: [] };
	const nativeFetch = window.fetch.bind(window);
	const unavailable = "This is a static UI preview. This action requires the live Director server.";
	const json = (body, status = 200) =>
		new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
	const notice = () => {
		const panel = document.getElementById("preview-notice");
		if (!panel) return;
		let message = document.getElementById("preview-action-message");
		if (!message) {
			message = document.createElement("p");
			message.id = "preview-action-message";
			message.setAttribute("role", "alert");
			panel.append(message);
		}
		message.textContent = unavailable;
		panel.scrollIntoView({ block: "nearest" });
	};
	window.fetch = async (input, options = {}) => {
		const url = new URL(
			typeof input === "string" || input instanceof URL ? input : input.url,
			window.location.href,
		);
		const method = String(options.method || input.method || "GET").toUpperCase();
		if (!["GET", "HEAD"].includes(method)) {
			notice();
			return json({ error: unavailable, message: unavailable, state: "unavailable" }, 503);
		}
		const files = url.pathname.match(/^\/(\d+)\/files\/([^/]+)\/$/);
		if (url.origin === window.location.origin && files) {
			const [, site, action] = files;
			if (["list", "read"].includes(action)) {
				const result = data.files[site]?.[action]?.[url.searchParams.get("path") || ""];
				return result
					? json(result.body, result.status)
					: json({ error: "This sample file or folder does not exist." }, 404);
			}
			return json({ error: unavailable }, 503);
		}
		if (
			url.origin !== window.location.origin ||
			/^\/runtime\//.test(url.pathname) ||
			/\/terminal\//.test(url.pathname)
		) {
			return json({ error: unavailable, message: unavailable, state: "unavailable" }, 503);
		}
		return nativeFetch(input, options);
	};
	document.addEventListener(
		"submit",
		(event) => {
			if (!event.target.matches("[data-preview-action]")) return;
			event.preventDefault();
			event.stopImmediatePropagation();
			notice();
		},
		true,
	);
	document.addEventListener("click", (event) => {
		const link = event.target.closest("a");
		if (!link) return;
		const url = new URL(link.href, window.location.href);
		if (
			link.hasAttribute("data-preview-unavailable") ||
			/^\/\d+\/files\/(download|archive)\//.test(url.pathname) ||
			/^\/runtime\/\d+\/(status|logs\/output)\//.test(url.pathname)
		) {
			event.preventDefault();
			notice();
		}
	});
	document.addEventListener("DOMContentLoaded", () => {
		const files = document.getElementById("file-browser");
		if (files) {
			const state = document.getElementById("file-save-state");
			const setReadOnly = () => {
				if (state?.textContent === "Saved") state.textContent = "Read-only sample";
			};
			new MutationObserver(setReadOnly).observe(state, { childList: true, subtree: true });
			setReadOnly();
			if (window.ace) window.ace.edit("file-code-editor").setReadOnly(true);
			document.getElementById("file-content").readOnly = true;
			document.getElementById("file-executable").disabled = true;
		}
		const search = document.getElementById("site-search");
		const type = document.getElementById("site-type");
		const scope = document.getElementById("site-scope");
		if (search && type) {
			const rows = Array.from(document.querySelectorAll(".dt-site-list > li"));
			const params = new URLSearchParams(window.location.search);
			search.value = params.get("q") || "";
			type.value = params.get("mode") || "";
			if (scope) scope.value = params.has("all") ? params.get("all") : "1";
			const filter = (update = false) => {
				const words = search.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
				let count = 0;
				for (const row of rows) {
					const visible =
						words.every((word) => row.dataset.previewSearch.includes(word)) &&
						(!type.value || row.dataset.previewType === type.value) &&
						(!scope || scope.value === "1" || row.dataset.previewOwned === "true");
					row.hidden = !visible;
					if (visible) count++;
				}
				document.querySelector(".dt-result-count").textContent =
					`${count} site${count === 1 ? "" : "s"} found`;
				document.querySelector(".dt-site-table-heading").hidden = count === 0;
				let empty = document.getElementById("preview-no-results");
				if (!empty) {
					empty = document.createElement("p");
					empty.id = "preview-no-results";
					empty.className = "dt-muted";
					empty.textContent = "No sites found. Try another search or clear the filters.";
					document.getElementById("site-results").append(empty);
				}
				empty.hidden = count !== 0;
				if (update) {
					const values = new URLSearchParams();
					if (search.value) values.set("q", search.value);
					if (type.value) values.set("mode", type.value);
					if (scope) values.set("all", scope.value);
					history.replaceState(
						null,
						"",
						window.location.pathname + (values.size ? `?${values}` : ""),
					);
				}
			};
			search.addEventListener("input", () => filter(true));
			type.addEventListener("change", () => filter(true));
			scope?.addEventListener("change", () => filter(true));
			search.form.addEventListener("submit", (event) => {
				event.preventDefault();
				filter(true);
			});
			filter();
		}
		const docs = document.getElementById("docs-query");
		if (docs) {
			const renderResults = () => {
				const query = docs.value.trim().toLowerCase();
				const words = query.split(/\s+/).filter(Boolean).slice(0, 20);
				let results = document.getElementById("preview-doc-results");
				if (!results) {
					results = document.createElement("div");
					results.id = "preview-doc-results";
					results.className = "dt-preview-doc-results";
					docs.form.after(results);
				}
				results.replaceChildren();
				if (!query) {
					results.hidden = true;
					return;
				}
				results.hidden = false;
				const matches = data.docs.filter((doc) =>
					words.some((word) => `${doc.title} ${doc.text}`.toLowerCase().includes(word)),
				);
				const heading = document.createElement("p");
				heading.textContent = `${matches.length} documentation result${matches.length === 1 ? "" : "s"}`;
				heading.setAttribute("role", "status");
				results.append(heading);
				const list = document.createElement("ul");
				for (const doc of matches.slice(0, 50)) {
					const row = document.createElement("li");
					const link = document.createElement("a");
					link.href = doc.url;
					link.textContent = doc.title;
					row.append(link);
					list.append(row);
				}
				results.append(list);
			};
			const params = new URLSearchParams(window.location.search);
			docs.value = params.get("q") || docs.value;
			docs.form.addEventListener("submit", (event) => {
				event.preventDefault();
				renderResults();
			});
			renderResults();
		}
	});
})();
