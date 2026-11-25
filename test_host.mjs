import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHmac, randomBytes } from "node:crypto";
import { once } from "node:events";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";

const loopback = "127.0.0.1";
const probe = createServer().listen(0, loopback);
await once(probe, "listening");
const port = probe.address().port;
await new Promise((resolve) => probe.close(resolve));
const secret = randomBytes(32);
const host = spawn(process.execPath, [fileURLToPath(new URL("host.mjs", import.meta.url)),
    "--port", String(port), "--target-pid", String(process.pid), "--cast", "F11", "--reel", "F11"], {
    env: { ...process.env, CAPTURE_RELAY_SECRET: secret.toString("hex") },
    stdio: ["ignore", "pipe", "inherit"],
});

try {
    await Promise.race([
        once(host.stdout, "data"),
        once(host, "exit").then(() => { throw new Error("Windows helper exited before the test."); }),
    ]);
    const send = (path, headers) => fetch(`http://${loopback}:${port}${path}`, {
        method: "POST", headers, signal: AbortSignal.timeout(5000),
    });
    function sign(path, time = Date.now()) {
        const nonce = randomBytes(16).toString("hex");
        return {
            "X-Capture-Time": time,
            "X-Capture-Nonce": nonce,
            "X-Capture-Signature": createHmac("sha256", secret).update(`${path}\n${time}\n${nonce}`).digest("hex"),
        };
    }

    assert.equal((await send("/cast")).status, 401);
    assert.equal((await send("/cast", sign("/cast", Date.now() - 4000))).status, 401);
    assert.equal((await send("/reel", sign("/cast"))).status, 401);
    const headers = sign("/cast");
    const unfocused = await send("/cast", headers);
    assert.equal(unfocused.status, 409);
    assert.match(await unfocused.text(), /not in front|foreground window/);
    const duplicate = await send("/cast", headers);
    assert.equal(duplicate.status, 409);
    assert.match(await duplicate.text(), /already received/);
    assert.equal((await send("/frame", sign("/frame"))).status, 404);
    console.log("Windows helper passed signature, expiration, replay, foreground, and input-only checks.");
} finally {
    host.kill();
}
