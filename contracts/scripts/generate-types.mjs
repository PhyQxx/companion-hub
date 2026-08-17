import { createRequire } from "node:module";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { compile } = require("json-schema-to-typescript");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schemaDir = path.join(root, "jsonschema");
const outputDir = path.join(root, "types");
const index = JSON.parse(await fs.readFile(path.join(schemaDir, "index.json"), "utf8"));

const schema = JSON.parse(await fs.readFile(path.join(schemaDir, index.bundle), "utf8"));
const output = await compile(schema, "AriaContracts", {
  bannerComment: "// Generated from Pydantic JSON Schema. Do not edit by hand.",
  style: { singleQuote: false },
  unreachableDefinitions: true,
});

await fs.mkdir(outputDir, { recursive: true });
await fs.writeFile(path.join(outputDir, "index.d.ts"), output);
