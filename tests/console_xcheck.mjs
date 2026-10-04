// Helper for tests/test_console_js.py: JSON in on stdin, JSON out on stdout.
//   {"op":"create", credentialId, prfOutput, prfSalt}  (hex)  -> {container, mainKey}
//   {"op":"open", container, credentialId, prfOutput}          -> {mainKey}
import * as C from "../console/js/container.js";
const hex = (s) => Uint8Array.from(Buffer.from(s, "hex"));
const out = (b) => Buffer.from(b).toString("hex");
let input = "";
for await (const chunk of process.stdin) input += chunk;
const r = JSON.parse(input);
if (r.op === "create") {
  const { container, mainKey } = await C.createContainer({ credentialId: hex(r.credentialId), prfOutput: hex(r.prfOutput), prfSalt: hex(r.prfSalt) });
  console.log(JSON.stringify({ container, mainKey: out(mainKey) }));
} else if (r.op === "open") {
  console.log(JSON.stringify({ mainKey: out(await C.openContainer(r.container, hex(r.credentialId), hex(r.prfOutput))) }));
}
