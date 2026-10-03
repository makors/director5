(() => {
	const root = document.getElementById("file-browser");
	if (!root) return;
	const endpoints = JSON.parse(document.getElementById("file-endpoints").textContent);
	const csrf = document.querySelector("#file-csrf [name=csrfmiddlewaretoken]").value;
	const el = (id) => document.getElementById(id);
	const state = { path: "", file: null, busy: false, files: new Map() };
	const workspaceKey = `director-files:${root.dataset.workspaceKey}`;
	const defaults = { fontSize: 13, indent: 4, softTabs: true, wrap: false, lineNumbers: true };
	function stored(key, fallback) {
		try {
			return JSON.parse(localStorage.getItem(key)) || fallback;
		} catch {
			return fallback;
		}
	}
	function store(key, value) {
		try {
			localStorage.setItem(key, JSON.stringify(value));
		} catch {
			/* Private browsing may block storage. */
		}
	}
	const savedPrefs = stored("director-editor-preferences", {});
	const prefs = {
		keybindings: ["standard", "vim", "emacs"].includes(savedPrefs.keybindings)
			? savedPrefs.keybindings
			: "standard",
		fontSize: [12, 13, 14, 16, 18].includes(savedPrefs.fontSize)
			? savedPrefs.fontSize
			: defaults.fontSize,
		indent: [2, 4, 8].includes(savedPrefs.indent) ? savedPrefs.indent : defaults.indent,
		softTabs: typeof savedPrefs.softTabs === "boolean" ? savedPrefs.softTabs : defaults.softTabs,
		wrap: typeof savedPrefs.wrap === "boolean" ? savedPrefs.wrap : defaults.wrap,
		lineNumbers:
			typeof savedPrefs.lineNumbers === "boolean" ? savedPrefs.lineNumbers : defaults.lineNumbers,
	};
	let editor = null;
	if (window.ace) {
		window.ace.config.set("basePath", root.dataset.editorAssets);
		editor = window.ace.edit("file-code-editor", {
			theme: "ace/theme/textmate",
			useWorker: false,
			enableBasicAutocompletion: true,
			enableLiveAutocompletion: false,
			showPrintMargin: false,
			fontFamily: "Geist Mono, ui-monospace, monospace",
		});
		el("file-content").hidden = true;
		el("file-code-editor").hidden = false;
		editor.commands.addCommand({
			name: "directorSave",
			bindKey: { win: "Ctrl-S", mac: "Command-S" },
			exec: () => el("file-editor-form").requestSubmit(),
		});
	}
	const active = () => state.files.get(state.file);
	const join = (name) => [state.path, name].filter(Boolean).join("/");
	const url = (name, path) => `${endpoints[name]}?${new URLSearchParams({ path })}`;
	const message = (text, error = false) => {
		el("file-message").textContent = text;
		el("file-message").hidden = !text;
		el("file-message").dataset.error = String(error);
	};
	const node = (tag, text) => {
		const result = document.createElement(tag);
		if (text !== undefined) result.textContent = text;
		return result;
	};
	async function api(name, data, mutation = false) {
		const body = data instanceof FormData ? data : new URLSearchParams(data);
		const response = await fetch(mutation ? endpoints[name] : `${endpoints[name]}?${body}`, {
			method: mutation ? "POST" : "GET",
			credentials: "same-origin",
			headers: mutation
				? { "X-CSRFToken": csrf, Accept: "application/json" }
				: { Accept: "application/json" },
			body: mutation ? body : undefined,
		});
		if (!response.headers.get("content-type")?.includes("application/json"))
			throw new Error("Your session changed. Reload this page to continue.");
		const result = await response.json();
		if (!response.ok)
			throw new Error(
				`${result.uploaded?.length ? `${result.uploaded.length} file(s) uploaded. ` : ""}${result.error || "The file service could not complete this operation."}`,
			);
		return result;
	}
	async function run(action) {
		if (state.busy) return;
		state.busy = true;
		root.setAttribute("aria-busy", "true");
		try {
			await action();
		} catch (error) {
			message(error.message, true);
		} finally {
			state.busy = false;
			root.removeAttribute("aria-busy");
		}
	}
	function ask(title, description, label, value = "", danger = false) {
		return new Promise((resolve) => {
			const dialog = el("file-dialog");
			el("file-dialog-title").textContent = title;
			el("file-dialog-description").textContent = description;
			el("file-dialog-label").textContent = label;
			el("file-dialog-label").hidden = !label;
			el("file-dialog-value").hidden = !label;
			el("file-dialog-value").required = !!label;
			el("file-dialog-value").value = value;
			el("file-dialog-confirm").textContent = danger ? "Delete" : "Continue";
			el("file-dialog-confirm").className = danger ? "dt-btn-danger" : "dt-btn-primary";
			let settled = false;
			function finish(result) {
				if (settled) return;
				settled = true;
				dialog.close();
				resolve(result);
			}
			el("file-dialog-form").onsubmit = (event) => {
				event.preventDefault();
				finish(label ? el("file-dialog-value").value : true);
			};
			el("file-dialog-cancel").onclick = () => finish(null);
			dialog.oncancel = () => finish(null);
			dialog.showModal();
			if (label) el("file-dialog-value").focus();
		});
	}
	async function discard(paths = [state.file]) {
		return (
			!paths.some((path) => state.files.get(path)?.dirty) ||
			(await ask(
				"Discard unsaved changes?",
				"Changes to these open files have not been saved.",
				"",
			)) !== null
		);
	}
	function dirty(value) {
		if (!active()) return;
		active().dirty = value;
		el("file-save-state").textContent = value ? "Unsaved changes" : "Saved";
		tabs();
	}
	function workspace() {
		store(workspaceKey, { paths: [...state.files.keys()].slice(0, 20), active: state.file });
	}
	function tabs() {
		const list = el("file-tabs");
		list.hidden = state.files.size === 0;
		el("file-close").hidden = state.files.size === 0;
		list.replaceChildren();
		let index = 0;
		for (const [path, file] of state.files) {
			const tab = node("div");
			tab.className = "dt-file-tab";
			tab.setAttribute("role", "presentation");
			const button = node("button", `${path.split("/").at(-1)}${file.dirty ? " ●" : ""}`);
			button.type = "button";
			button.title = path;
			button.setAttribute("role", "tab");
			button.id = `file-tab-${index++}`;
			button.setAttribute("aria-controls", "file-editor-panel");
			if (state.file === path) el("file-editor-panel").setAttribute("aria-labelledby", button.id);
			button.setAttribute("aria-selected", String(state.file === path));
			button.tabIndex = state.file === path ? 0 : -1;
			button.onclick = () => run(() => activate(path));
			button.onkeydown = (event) => {
				if (event.key === "Delete") {
					event.preventDefault();
					run(async () => {
						if (await discard([path])) closeFiles([path]);
					});
					return;
				}
				if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
				event.preventDefault();
				const paths = [...state.files.keys()];
				let index = paths.indexOf(path);
				index =
					event.key === "Home"
						? 0
						: event.key === "End"
							? paths.length - 1
							: (index + (event.key === "ArrowRight" ? 1 : -1) + paths.length) % paths.length;
				activate(paths[index]);
				list.querySelector('[aria-selected="true"]')?.focus();
			};
			tab.append(button);
			list.append(tab);
		}
	}
	function closeFiles(paths) {
		for (const path of paths) {
			state.files.get(path)?.session?.destroy();
			state.files.delete(path);
		}
		if (!state.files.has(state.file)) state.file = state.files.keys().next().value || null;
		if (state.file) activate(state.file);
		else {
			el("file-editor-panel").hidden = true;
			el("file-editor-empty").hidden = false;
			tabs();
			workspace();
		}
	}
	const related = (path) =>
		[...state.files.keys()].filter((name) => name === path || name.startsWith(`${path}/`));
	function syntax(path) {
		const name = path.split("/").at(-1).toLowerCase();
		if (/^dockerfile(?:\.|$)/.test(name)) return "dockerfile";
		const extension = name.split(".").at(-1);
		return (
			{
				html: "html",
				htm: "html",
				css: "css",
				js: "javascript",
				jsx: "javascript",
				mjs: "javascript",
				ts: "typescript",
				tsx: "typescript",
				json: "json",
				py: "python",
				php: "php",
				rb: "ruby",
				sh: "sh",
				bash: "sh",
				yml: "yaml",
				yaml: "yaml",
				md: "markdown",
				sql: "sql",
				xml: "xml",
				svg: "xml",
				ini: "ini",
				conf: "ini",
				c: "c_cpp",
				h: "c_cpp",
				cpp: "c_cpp",
				java: "java",
				go: "golang",
				rs: "rust",
			}[extension] || "text"
		);
	}
	function applyPreferences() {
		const handler = prefs.keybindings === "standard" ? null : `ace/keyboard/${prefs.keybindings}`;
		if (editor && editor.$directorKeybindings !== prefs.keybindings) {
			editor.setKeyboardHandler(handler);
			editor.$directorKeybindings = prefs.keybindings;
		}
		editor?.setOptions({ fontSize: prefs.fontSize, showGutter: prefs.lineNumbers });
		for (const file of state.files.values()) {
			file.session?.setTabSize(prefs.indent);
			file.session?.setUseSoftTabs(prefs.softTabs);
			file.session?.setUseWrapMode(prefs.wrap);
			file.session?.setUseWorker(false);
		}
		el("file-content").style.fontSize = `${prefs.fontSize}px`;
		el("file-content").style.tabSize = prefs.indent;
		el("file-content").wrap = prefs.wrap ? "soft" : "off";
		editor?.resize();
	}
	function activate(path) {
		const file = state.files.get(path);
		if (!file) return;
		if (!editor && active()) active().content = el("file-content").value;
		state.file = path;
		el("file-editor-path").textContent = path;
		el("file-language").value = file.language;
		el("file-executable").checked = !!(Number.parseInt(file.mode, 8) & 0o111);
		if (editor) editor.setSession(file.session);
		else el("file-content").value = file.content;
		el("file-editor-empty").hidden = true;
		el("file-editor-panel").hidden = false;
		el("file-save-state").textContent = file.dirty ? "Unsaved changes" : "Saved";
		tabs();
		workspace();
		applyPreferences();
		editor?.focus();
	}
	function breadcrumbs() {
		const crumb = el("file-breadcrumbs");
		crumb.replaceChildren();
		const parts = state.path ? state.path.split("/") : [];
		["Site", ...parts].forEach((part, index) => {
			if (index) crumb.append(node("span", "/"));
			const button = node("button", part);
			button.type = "button";
			button.onclick = () => run(() => load(parts.slice(0, index).join("/")));
			crumb.append(button);
		});
		el("file-folder-download").href = url("archive", state.path);
	}
	function size(bytes) {
		if (bytes < 1024) return `${bytes} B`;
		return bytes < 1048576
			? `${(bytes / 1024).toFixed(1)} KB`
			: `${(bytes / 1048576).toFixed(1)} MB`;
	}
	function menuAction(menu, label, action) {
		const button = node("button", label);
		button.type = "button";
		button.onclick = () => {
			menu.parentElement.open = false;
			run(action);
		};
		menu.append(button);
	}
	async function load(path = state.path) {
		const listing = await api("list", { path });
		state.path = path;
		breadcrumbs();
		const list = el("file-list");
		list.replaceChildren();
		for (const entry of listing.entries) {
			const row = node("tr");
			const name = node("td");
			const button = node("button");
			button.type = "button";
			button.className = "dt-file-name";
			const icon = node("span", entry.type === "directory" ? "▸" : "·");
			icon.setAttribute("aria-hidden", "true");
			button.append(icon, node("span", entry.name));
			if (entry.type === "directory") button.onclick = () => run(() => load(entry.path));
			else if (entry.type === "file") button.onclick = () => run(() => open(entry.path));
			else button.disabled = true;
			name.append(button);
			if (entry.type === "link") name.append(node("span", "Symbolic link · cannot be opened"));
			const bytes = node("td", entry.type === "file" ? size(entry.size) : "—");
			const actions = node("td");
			const detail = node("details");
			detail.className = "dt-file-menu";
			const summary = node("summary", "···");
			summary.setAttribute("aria-label", `Actions for ${entry.name}`);
			const menu = node("div");
			if (["directory", "file"].includes(entry.type)) {
				const download = node("a", entry.type === "directory" ? "Download ZIP" : "Download");
				download.href = url(entry.type === "directory" ? "archive" : "download", entry.path);
				menu.append(download);
				menuAction(menu, "Rename or move", async () => {
					const affected = related(entry.path);
					const destination = await ask(
						"Rename or move",
						"Enter a path relative to the site root. Existing files are never overwritten.",
						"New path",
						entry.path,
					);
					if (destination === null) return;
					await api("move", { path: entry.path, destination }, true);
					for (const path of affected) {
						const next = destination + path.slice(entry.path.length);
						const file = state.files.get(path);
						state.files.delete(path);
						state.files.set(next, file);
						if (state.file === path) state.file = next;
						if (file.language === "auto") file.session?.setMode(`ace/mode/${syntax(next)}`);
					}
					if (state.file) activate(state.file);
					await load();
					message("Moved.");
				});
				menuAction(menu, "Permissions", async () => {
					const mode = await ask(
						"File permissions",
						"Use three octal digits, such as 644 for a file or 755 for an executable script or folder.",
						"Permissions",
						entry.mode,
					);
					if (mode === null) return;
					await api("chmod", { path: entry.path, mode }, true);
					if (state.files.has(entry.path)) state.files.get(entry.path).mode = mode;
					if (state.file === entry.path)
						el("file-executable").checked = !!(Number.parseInt(mode, 8) & 0o111);
					await load();
					message("Permissions updated.");
				});
			}
			menuAction(menu, "Delete", async () => {
				const affected = related(entry.path);
				if (!(await discard(affected))) return;
				if (
					(await ask(
						"Delete permanently?",
						entry.type === "directory"
							? `Delete ${entry.path} and everything inside it?`
							: `Delete ${entry.path}?`,
						"",
						"",
						true,
					)) === null
				)
					return;
				await api(
					"delete",
					{ path: entry.path, recursive: entry.type === "directory" ? "true" : "" },
					true,
				);
				closeFiles(affected);
				await load();
				message("Deleted.");
			});
			detail.append(summary, menu);
			actions.append(detail);
			row.append(name, bytes, actions);
			list.append(row);
		}
		if (!listing.entries.length) {
			const row = node("tr");
			const cell = node("td", "This folder is empty.");
			cell.colSpan = 3;
			row.append(cell);
			list.append(row);
		}
	}
	async function open(path, force = false) {
		if (!force && state.files.has(path)) {
			activate(path);
			return;
		}
		const file = await api("read", { path });
		state.files.get(path)?.session?.destroy();
		const session = editor
			? window.ace.createEditSession(file.content, `ace/mode/${syntax(path)}`)
			: null;
		const record = {
			content: file.content,
			digest: file.sha256,
			mode: file.mode,
			dirty: false,
			language: "auto",
			session,
		};
		state.files.set(path, record);
		session?.on("change", () => {
			record.dirty = true;
			tabs();
			if (active() === record) el("file-save-state").textContent = "Unsaved changes";
		});
		activate(path);
		message("");
	}
	el("file-refresh").onclick = () => run(() => load());
	el("file-new").onclick = () =>
		run(async () => {
			const name = await ask("New file", "Enter a filename in the current folder.", "Filename");
			if (name === null) return;
			const path = join(name);
			await api("write", { path, content: "", create_only: "true" }, true);
			await load();
			await open(path, true);
		});
	el("file-mkdir").onclick = () =>
		run(async () => {
			const name = await ask(
				"New folder",
				"Enter a folder name in the current folder.",
				"Folder name",
			);
			if (name === null) return;
			await api("mkdir", { path: join(name) }, true);
			await load();
			message("Folder created.");
		});
	el("file-upload").onclick = () => el("file-upload-input").click();
	el("file-upload-input").onchange = () =>
		run(async () => {
			const data = new FormData();
			data.append("path", state.path);
			for (const file of el("file-upload-input").files) data.append("files", file);
			message("Uploading…");
			try {
				const result = await api("upload", data, true);
				message(`Uploaded ${result.uploaded.length} file(s).`);
			} finally {
				el("file-upload-input").value = "";
				await load();
			}
		});
	el("file-content").oninput = () => dirty(true);
	el("file-executable").onchange = () => dirty(true);
	el("file-editor-form").onsubmit = (event) => {
		event.preventDefault();
		run(async () => {
			const file = active();
			if (!file) return;
			const path = state.file;
			const original = Number.parseInt(file.mode, 8);
			const mode = (el("file-executable").checked ? original | 0o111 : original & ~0o111)
				.toString(8)
				.padStart(3, "0");
			const content = editor ? file.session.getValue() : el("file-content").value;
			const result = await api(
				"write",
				{ path, content, expected_sha256: file.digest, mode },
				true,
			);
			file.digest = result.sha256;
			file.mode = mode;
			file.content = content;
			const current = editor ? file.session.getValue() : el("file-content").value;
			file.dirty =
				current !== content ||
				(active() === file &&
					el("file-executable").checked !== !!(Number.parseInt(mode, 8) & 0o111));
			if (active() === file) dirty(file.dirty);
			else tabs();
			await load();
			message(file.dirty ? "Saved. New changes remain unsaved." : "Saved.");
		});
	};
	el("file-reload").onclick = () =>
		run(async () => {
			if (await discard()) await open(state.file, true);
		});
	el("file-content").onkeydown = (event) => {
		if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
			event.preventDefault();
			el("file-editor-form").requestSubmit();
		}
	};
	el("file-language").onchange = () => {
		const file = active();
		if (!file) return;
		file.language = el("file-language").value;
		file.session?.setMode(
			`ace/mode/${file.language === "auto" ? syntax(state.file) : file.language}`,
		);
	};
	el("file-preferences").onclick = () => {
		el("file-keybindings").value = prefs.keybindings;
		el("file-font-size").value = prefs.fontSize;
		el("file-indent-size").value = prefs.indent;
		el("file-soft-tabs").checked = prefs.softTabs;
		el("file-word-wrap").checked = prefs.wrap;
		el("file-line-numbers").checked = prefs.lineNumbers;
		el("file-preferences-dialog").showModal();
	};
	el("file-close").onclick = () =>
		run(async () => {
			if (await discard()) closeFiles([state.file]);
		});
	el("file-preferences-form").onchange = () => {
		prefs.keybindings = el("file-keybindings").value;
		prefs.fontSize = Number(el("file-font-size").value);
		prefs.indent = Number(el("file-indent-size").value);
		prefs.softTabs = el("file-soft-tabs").checked;
		prefs.wrap = el("file-word-wrap").checked;
		prefs.lineNumbers = el("file-line-numbers").checked;
		store("director-editor-preferences", prefs);
		applyPreferences();
	};
	window.addEventListener("beforeunload", (event) => {
		if ([...state.files.values()].some((file) => file.dirty)) {
			event.preventDefault();
			event.returnValue = "";
		}
	});
	setInterval(() => {
		if (
			!state.busy &&
			!document.hidden &&
			!el("file-dialog").open &&
			!el("file-preferences-dialog").open
		)
			run(() => load());
	}, 30000);
	run(async () => {
		await load();
		const previous = stored(workspaceKey, {});
		if (Array.isArray(previous.paths)) {
			for (const path of previous.paths.slice(0, 20)) {
				if (typeof path !== "string") continue;
				try {
					await open(path);
				} catch {
					/* Closed or unavailable files are omitted. */
				}
			}
			if (state.files.has(previous.active)) activate(previous.active);
		}
	});
})();
