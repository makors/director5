(() => {
	const form = document.getElementById("terminal-connect");
	if (!form) return;
	const connectButton = form.querySelector('button[type="submit"]');
	const disconnectButton = document.getElementById("terminal-disconnect");
	const status = document.getElementById("terminal-status");
	const terminal = new Terminal({
		fontFamily: '"Geist Mono", monospace',
		fontSize: 14,
		scrollback: 2000,
		screenReaderMode: true,
		theme: {
			background: "#ffffff",
			foreground: "#171717",
			cursor: "#171717",
			selectionBackground: "#eaeaea",
		},
	});
	const fit = new FitAddon.FitAddon();
	terminal.loadAddon(fit);
	terminal.open(document.getElementById("site-terminal"));
	fit.fit();
	let socket = null;
	let ready = false;
	const encoder = new TextEncoder();

	function resize() {
		fit.fit();
		if (ready && socket?.readyState === WebSocket.OPEN) {
			socket.send(
				JSON.stringify({
					type: "resize",
					rows: Math.min(120, Math.max(2, terminal.rows)),
					cols: Math.min(300, Math.max(10, terminal.cols)),
				}),
			);
		}
	}
	new ResizeObserver(resize).observe(document.getElementById("site-terminal"));
	terminal.onData((data) => {
		if (!ready || socket?.readyState !== WebSocket.OPEN) return;
		const bytes = encoder.encode(data);
		for (let offset = 0; offset < bytes.length; offset += 8192)
			socket.send(bytes.slice(offset, offset + 8192));
	});
	disconnectButton.addEventListener("click", () => socket?.close());
	window.addEventListener("pagehide", () => socket?.close());

	form.addEventListener("submit", async (event) => {
		event.preventDefault();
		if (socket || connectButton.disabled) return;
		connectButton.disabled = true;
		status.textContent = "Connecting…";
		try {
			const response = await fetch(form.action, {
				method: "POST",
				body: new FormData(form),
				credentials: "same-origin",
				headers: { Accept: "application/json" },
			});
			const data = await response.json();
			if (!response.ok) throw new Error(data.message || "The terminal could not be opened.");
			const ticket = btoa(data.token).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
			const url = new URL(data.path, window.location.href);
			url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
			socket = new WebSocket(url, ["director-terminal", `ticket.${ticket}`]);
			socket.binaryType = "arraybuffer";
			socket.onmessage = (message) => {
				if (typeof message.data !== "string") {
					terminal.write(new Uint8Array(message.data));
					return;
				}
				try {
					const control = JSON.parse(message.data);
					if (control.type === "ready") {
						ready = true;
						disconnectButton.disabled = false;
						status.textContent = "Connected";
						resize();
						terminal.focus();
					} else if (control.type === "error") status.textContent = control.message;
				} catch {
					status.textContent = "The terminal returned an invalid response.";
				}
			};
			socket.onerror = () => {
				status.textContent = "The terminal connection failed.";
			};
			socket.onclose = () => {
				if (ready || status.textContent === "Connecting…") status.textContent = "Disconnected";
				ready = false;
				socket = null;
				connectButton.disabled = false;
				disconnectButton.disabled = true;
			};
		} catch (error) {
			socket = null;
			connectButton.disabled = false;
			status.textContent = error.message || "The terminal connection failed.";
		}
	});
})();
