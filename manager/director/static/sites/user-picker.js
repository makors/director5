(() => {
	let nextId = 0;
	function enhance(select) {
		if (select.dataset.pickerReady || select.disabled || !select.multiple) return;
		select.dataset.pickerReady = "true";
		const prefix = `user-picker-${++nextId}`;
		const node = (tag, className, text) => {
			const element = document.createElement(tag);
			if (className) element.className = className;
			if (text !== undefined) element.textContent = text;
			return element;
		};
		const wrapper = node("div", "dt-user-picker");
		const chips = node("div", "dt-user-picker-chips");
		const input = node("input", "dt-input");
		input.type = "text";
		input.id = `${prefix}-input`;
		input.autocomplete = "off";
		input.spellcheck = false;
		input.placeholder = "Search people…";
		input.setAttribute("role", "combobox");
		input.setAttribute("aria-autocomplete", "list");
		input.setAttribute("aria-expanded", "false");
		input.setAttribute("aria-controls", `${prefix}-list`);
		if (select.getAttribute("aria-describedby"))
			input.setAttribute("aria-describedby", select.getAttribute("aria-describedby"));
		if (select.getAttribute("aria-invalid"))
			input.setAttribute("aria-invalid", select.getAttribute("aria-invalid"));
		const labels = [...select.labels];
		for (const [index, label] of labels.entries()) {
			if (!label.id) label.id = `${prefix}-label-${index}`;
			label.htmlFor = input.id;
		}
		if (labels.length)
			input.setAttribute("aria-labelledby", labels.map((label) => label.id).join(" "));
		else input.setAttribute("aria-label", "Additional people with access");
		const popup = node("div", "dt-user-picker-popup");
		popup.hidden = true;
		const list = node("ul", "dt-user-picker-list");
		list.id = `${prefix}-list`;
		list.setAttribute("role", "listbox");
		list.setAttribute("aria-label", "Matching people");
		const status = node("p", "dt-user-picker-status");
		status.setAttribute("role", "status");
		popup.append(list, status);
		wrapper.append(chips, input, popup);
		select.after(wrapper);
		select.hidden = true;
		let matches = [];
		let current = -1;
		const available = () =>
			[...select.options].filter((option) => !option.disabled && option.value);
		function close() {
			popup.hidden = true;
			input.setAttribute("aria-expanded", "false");
			input.removeAttribute("aria-activedescendant");
		}
		function highlight(index) {
			current = index;
			for (const [position, option] of [...list.children].entries()) {
				option.setAttribute("aria-selected", String(position === current));
				if (position === current) {
					input.setAttribute("aria-activedescendant", option.id);
					option.scrollIntoView({ block: "nearest" });
				}
			}
			if (current < 0) input.removeAttribute("aria-activedescendant");
		}
		function choose(option) {
			option.selected = true;
			input.value = "";
			select.dispatchEvent(new Event("change", { bubbles: true }));
			input.focus({ preventScroll: true });
			renderResults();
		}
		function renderResults() {
			const query = input.value.trim().toLocaleLowerCase();
			matches = available().filter(
				(option) => !option.selected && option.textContent.toLocaleLowerCase().includes(query),
			);
			list.replaceChildren();
			for (const [index, option] of matches.entries()) {
				const result = node("li", "", option.textContent);
				result.id = `${prefix}-option-${index}`;
				result.setAttribute("role", "option");
				result.addEventListener("pointerdown", (event) => event.preventDefault());
				result.addEventListener("click", () => choose(option));
				list.append(result);
			}
			status.textContent = matches.length
				? `${matches.length} ${matches.length === 1 ? "person" : "people"} available`
				: available().every((option) => option.selected)
					? "No more people available."
					: "No matching people.";
			popup.hidden = false;
			input.setAttribute("aria-expanded", "true");
			highlight(matches.length ? 0 : -1);
		}
		function renderChips() {
			chips.replaceChildren();
			for (const option of [...select.options].filter((option) => option.selected)) {
				const chip = node("span", "dt-user-picker-chip");
				const remove = node("button", "", "×");
				remove.type = "button";
				remove.setAttribute("aria-label", `Remove ${option.textContent}`);
				remove.addEventListener("click", () => {
					option.selected = false;
					select.dispatchEvent(new Event("change", { bubbles: true }));
					input.focus({ preventScroll: true });
					renderResults();
				});
				chip.append(node("span", "", option.textContent), remove);
				chips.append(chip);
			}
			chips.hidden = !chips.childElementCount;
		}
		input.addEventListener("input", renderResults);
		input.addEventListener("focus", renderResults);
		input.addEventListener("keydown", (event) => {
			if (event.isComposing) return;
			if (event.key === "Escape") {
				event.preventDefault();
				close();
			} else if (["ArrowDown", "ArrowUp"].includes(event.key)) {
				event.preventDefault();
				if (popup.hidden) renderResults();
				else if (matches.length)
					highlight(
						(current + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length,
					);
			} else if (event.key === "Enter") {
				event.preventDefault();
				if (!popup.hidden && current >= 0) choose(matches[current]);
			}
		});
		wrapper.addEventListener("focusout", (event) => {
			if (!wrapper.contains(event.relatedTarget)) close();
		});
		select.addEventListener("change", renderChips);
		select.form?.addEventListener("reset", () =>
			setTimeout(() => {
				input.value = "";
				renderChips();
				close();
			}, 0),
		);
		renderChips();
	}
	function initialize() {
		for (const select of document.querySelectorAll("select[data-user-picker]")) enhance(select);
	}
	initialize();
	document.addEventListener("htmx:afterSwap", initialize);
})();
