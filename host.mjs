import { spawn } from "node:child_process";
import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import { isIPv4 } from "node:net";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const { values } = parseArgs({ options: {
    bind: { type: "string", default: "127.0.0.1" },
    vm: { type: "string", default: "127.0.0.1" },
    port: { type: "string", default: "8765" },
    "target-pid": { type: "string" },
    cast: { type: "string", default: "1" },
    reel: { type: "string", default: "F" },
    help: { type: "boolean" },
} });

if (values.help) {
    console.log("node host.mjs --bind WINDOWS_IP --vm DEBIAN_IP --target-pid PID [--port 8765] [--cast 1] [--reel F]");
    console.log("Set CAPTURE_RELAY_SECRET to the shared 64-character hex secret.");
    process.exit(0);
}

const port = Number(values.port);
const secret = process.env.CAPTURE_RELAY_SECRET ?? "";
if (![values.bind, values.vm].every((ip) => isIPv4(ip) && ip !== "0.0.0.0") ||
    !Number.isInteger(port) || port < 1 || port > 65535 ||
    !/^[1-9]\d{0,9}$/.test(values["target-pid"] ?? "") || Number(values["target-pid"]) > 2147483647 ||
    ![values.cast, values.reel].every((key) => /^(?:[a-z0-9]|f(?:[1-9]|1[0-2]))$/i.test(key)) ||
    !/^[0-9a-f]{64}$/i.test(secret)) {
    throw new Error("Expected specific IPv4 addresses, an available port, a target process ID, keys A-Z, 0-9, or F1-F12, and a 64-character hex secret.");
}

function virtualKeyCode(key) {
    return key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0x6f + Number(key.slice(1));
}

const bridgeEnvironment = { ...process.env };
delete bridgeEnvironment.CAPTURE_RELAY_SECRET;
const bridge = spawn("powershell.exe", [
    "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
    fileURLToPath(new URL("bridge.ps1", import.meta.url)),
    "-TargetPid", values["target-pid"], "-Cast", String(virtualKeyCode(values.cast)), "-Reel", String(virtualKeyCode(values.reel)),
], { windowsHide: true, stdio: ["pipe", "pipe", "inherit"], env: bridgeEnvironment });

const windowMs = 3000;
const usedNonces = new Map();
let pending;
let shuttingDown = false;
let ready = false;
const startupTimeout = setTimeout(() => stop(new Error("Windows key helper did not start.")), 15000);

function reply(response, status, message) {
    response.writeHead(status, { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" });
    response.end(message);
}

const server = createServer({ headersTimeout: 3000, requestTimeout: 3000 }, (request, response) => {
    const path = request.url;
    if (request.socket.remoteAddress !== values.vm) return reply(response, 403, "Source IP is not the configured VM.");
    if (request.method !== "POST" || !["/focus", "/cast", "/reel"].includes(path)) {
        return reply(response, 404, "Unknown command.");
    }
    if (request.headers["transfer-encoding"] || Number(request.headers["content-length"] ?? 0) !== 0) {
        return reply(response, 400, "Commands have no body.");
    }
    const timestamp = request.headers["x-capture-time"] ?? "";
    const nonce = request.headers["x-capture-nonce"] ?? "";
    const signature = request.headers["x-capture-signature"] ?? "";
    if (!/^\d{13}$/.test(timestamp) || !/^[a-f0-9]{32}$/.test(nonce) || !/^[a-f0-9]{64}$/.test(signature)) {
        return reply(response, 401, "Missing or malformed signature.");
    }
    const expected = createHmac("sha256", Buffer.from(secret, "hex")).update(`${path}\n${timestamp}\n${nonce}`).digest();
    const now = Date.now();
    if (!timingSafeEqual(Buffer.from(signature, "hex"), expected) || Math.abs(now - Number(timestamp)) >= windowMs) {
        return reply(response, 401, "Invalid or expired signature. Check the secret and system clocks.");
    }
    for (const [id, deadline] of usedNonces) {
        if (deadline <= now) usedNonces.delete(id);
    }
    if (usedNonces.has(nonce)) return reply(response, 409, "Command was already received.");
    const deadline = Number(timestamp) + windowMs;
    usedNonces.set(nonce, deadline);
    if (pending) return reply(response, 429, "A command is already in progress.");
    const timer = setTimeout(() => stop(new Error("Windows key helper timed out.")), 1200);
    pending = { response, timer, path };
    bridge.stdin.write(`${path.slice(1)} ${deadline}\n`);
});

createInterface({ input: bridge.stdout }).on("line", (line) => {
    if (line === "READY" && !shuttingDown && !ready) {
        ready = true;
        clearTimeout(startupTimeout);
        server.listen(port, values.bind, () => console.log(
            `Windows input helper listening on ${values.bind}:${port} for ${values.vm}. Press Ctrl+C to stop.`,
        ));
        return;
    }
    if (!pending) return;
    const { response, timer, path } = pending;
    pending = undefined;
    clearTimeout(timer);
    if (line === "OK") {
        if (path !== "/focus") console.log(`${path.slice(1)} key sent.`);
        reply(response, 204);
    } else {
        reply(response, line.startsWith("PAUSE ") ? 409 : 502, line.replace(/^(PAUSE|ERROR) /, ""));
    }
});

function stop(error) {
    if (shuttingDown) return;
    shuttingDown = true;
    clearTimeout(startupTimeout);
    if (error) {
        console.error(error.message);
        process.exitCode = 1;
    }
    if (pending) {
        clearTimeout(pending.timer);
        reply(pending.response, 503, "Windows helper stopped. Check the game before restarting.");
        pending = undefined;
    }
    bridge.kill();
    server.close();
    server.closeAllConnections();
}

server.on("error", stop);
bridge.on("error", stop);
bridge.stdin.on("error", stop);
bridge.on("exit", (code) => stop(new Error(`Windows key helper exited (${code}).`)));
process.once("SIGINT", () => stop());
process.once("SIGTERM", () => stop());
