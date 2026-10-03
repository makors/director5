(() => {
	const pending = new Map();
	const checkedForms = new WeakSet();
	const preparedTabs = new WeakSet();
	function revealActiveTabs() {
		for (const nav of document.querySelectorAll(".dt-project-tabs")) {
			if (!preparedTabs.has(nav)) {
				preparedTabs.add(nav);
				const active = nav.querySelector('[aria-current="page"]');
				if (active)
					nav.scrollLeft = Math.max(
						0,
						active.offsetLeft - nav.offsetLeft - (nav.clientWidth - active.offsetWidth) / 2,
					);
				const update = () => {
					nav.dataset.overflow = String(nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 2);
					nav.dataset.scrolled = String(nav.scrollLeft > 2);
				};
				nav.addEventListener("scroll", update, { passive: true });
				new ResizeObserver(update).observe(nav);
				update();
			}
		}
	}
	document.addEventListener("htmx:afterSettle", revealActiveTabs);
	document.addEventListener("htmx:wsAfterMessage", revealActiveTabs);
	window.addEventListener("pageshow", revealActiveTabs);
	revealActiveTabs();

	if (window.htmx) document.documentElement.classList.add("dt-enhanced");

	function setPending(form, submitter) {
		if (pending.has(form)) return;
		const button = submitter || form.querySelector('button[type="submit"]');
		if (!button) return;
		const state = {
			button,
			label: button.getAttribute("aria-label"),
			width: button.style.width,
		};
		const spinner = document.createElement("span");
		spinner.className = "dt-form-spinner";
		spinner.setAttribute("aria-hidden", "true");
		button.style.width = `${button.getBoundingClientRect().width}px`;
		button.classList.add("dt-form-busy");
		button.append(spinner);
		if (button.dataset.submittingText) {
			button.setAttribute("aria-label", button.dataset.submittingText);
		}
		form.setAttribute("aria-busy", "true");
		pending.set(form, { ...state, spinner });
		// Native submissions must serialize their submitter before disabling it.
		window.setTimeout(() => {
			if (pending.has(form)) button.disabled = true;
		}, 0);
	}

	function clearPending(form) {
		const state = pending.get(form);
		if (!state) return;
		state.spinner.remove();
		state.button.classList.remove("dt-form-busy");
		state.button.disabled = false;
		state.button.style.width = state.width;
		if (state.label === null) state.button.removeAttribute("aria-label");
		else state.button.setAttribute("aria-label", state.label);
		form.removeAttribute("aria-busy");
		pending.delete(form);
	}

	function focusError(root) {
		const forms = root.matches?.("form[data-dt-form]")
			? [root]
			: root.querySelectorAll?.("form[data-dt-form]") || [];
		for (const form of forms) {
			if (checkedForms.has(form)) continue;
			checkedForms.add(form);
			const invalid = form.querySelector('[aria-invalid="true"]');
			const target = invalid?.matches("fieldset") ? invalid.querySelector("input") : invalid;
			if (target) {
				target.focus();
				return;
			}
			const error = form.querySelector('[role="alert"]');
			if (error) {
				error.setAttribute("tabindex", "-1");
				error.focus();
				return;
			}
		}
	}

	function syncSearchHistory() {
		const form = document.querySelector("[data-dt-search]");
		if (!form) return;
		const params = new URLSearchParams(window.location.search);
		form.elements.q.value = params.get("q") || "";
		form.elements.mode.value = params.get("mode") || "";
		if (form.elements.all) form.elements.all.value = params.get("all") || "";
	}

	document.addEventListener(
		"submit",
		(event) => {
			const form = event.target;
			if (!form.matches("form[data-dt-form]")) return;
			if (pending.has(form)) {
				event.preventDefault();
				event.stopImmediatePropagation();
				return;
			}
			if (!window.htmx || !form.hasAttribute("hx-post")) setPending(form, event.submitter);
		},
		true,
	);

	document.addEventListener("htmx:beforeRequest", (event) => {
		const form = event.detail.elt;
		if (form.matches("form[data-dt-form]")) setPending(form);
		if (form.matches("[data-dt-search]")) {
			form.querySelector(".dt-search-error").hidden = true;
		}
		const error = form.querySelector("[data-dt-request-error]");
		if (error) error.hidden = true;
	});

	document.addEventListener("htmx:afterRequest", (event) => {
		clearPending(event.detail.elt);
	});

	for (const name of ["htmx:responseError", "htmx:sendError", "htmx:timeout"]) {
		document.addEventListener(name, (event) => {
			const form = event.detail.elt.closest("[data-dt-search]");
			if (form) form.querySelector(".dt-search-error").hidden = false;
			const submittedForm = event.detail.elt.closest("form[data-dt-form]");
			if (!submittedForm) return;
			let error = submittedForm.querySelector("[data-dt-request-error]");
			if (!error) {
				error = document.createElement("p");
				error.className = "dt-error-list";
				error.dataset.dtRequestError = "";
				error.setAttribute("role", "alert");
				submittedForm.prepend(error);
			}
			error.textContent = "Could not complete the request. Try again.";
			error.hidden = false;
		});
	}

	document.addEventListener("htmx:afterSwap", (event) => {
		focusError(event.target);
	});
	document.addEventListener("htmx:historyRestore", syncSearchHistory);
	window.addEventListener("pageshow", () => {
		for (const form of pending.keys()) clearPending(form);
		syncSearchHistory();
	});

	document.addEventListener("click", (event) => {
		for (const menu of document.querySelectorAll("[data-dt-menu][open]")) {
			if (!menu.contains(event.target)) menu.open = false;
		}
	});
	document.addEventListener("keydown", (event) => {
		if (event.key !== "Escape") return;
		const menu = document.querySelector("[data-dt-menu][open]");
		if (!menu) return;
		menu.open = false;
		menu.querySelector("summary").focus();
	});

	focusError(document);
})();
