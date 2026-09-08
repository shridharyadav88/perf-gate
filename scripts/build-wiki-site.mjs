#!/usr/bin/env node
/**
 * Build a static OpenWiki visualizer for GitHub Pages.
 * Reads openwiki/ markdown, emits graph.json plus the OpenWiki client bundle.
 */
import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const wikiRoot = path.join(repoRoot, "openwiki");
const outDir = path.join(repoRoot, "docs");
const visualizerIgnorePath = path.join(wikiRoot, ".visualizerignore");

function parseIgnorePatterns(raw) {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
}

function matchesIgnore(id, patterns) {
  const normalized = id.replace(/\\/g, "/");
  return patterns.some((pattern) => {
    if (pattern.endsWith("/")) {
      return normalized.startsWith(pattern) || normalized.includes(`/${pattern}`);
    }
    if (pattern.includes("*")) {
      const regex = new RegExp(
        `^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*\*/g, ".*").replace(/\*/g, "[^/]*")}$`,
      );
      return regex.test(normalized);
    }
    return normalized === pattern || normalized.endsWith(`/${pattern}`);
  });
}

function filterGraph(graph, patterns) {
  if (patterns.length === 0) {
    return graph;
  }
  const nodes = graph.nodes.filter((node) => !matchesIgnore(node.id, patterns));
  const ids = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter(
    (edge) => ids.has(edge.source) && ids.has(edge.target),
  );
  for (const node of nodes) {
    node.links = node.links.filter((target) => ids.has(target));
    node.backlinks = node.backlinks.filter((source) => ids.has(source));
  }
  return { ...graph, nodes, edges };
}

async function loadVisualizerIgnorePatterns() {
  try {
    const raw = await readFile(visualizerIgnorePath, "utf8");
    return parseIgnorePatterns(raw);
  } catch {
    return [];
  }
}

async function resolveOpenWikiDist() {
  const candidates = [];
  try {
    const { createRequire } = await import("node:module");
    const require = createRequire(import.meta.url);
    candidates.push(
      path.join(path.dirname(require.resolve("openwiki/package.json")), "dist", "visualize"),
    );
  } catch {
    /* local openwiki not installed */
  }

  const { execFileSync } = await import("node:child_process");
  try {
    const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
    candidates.push(path.join(globalRoot, "openwiki", "dist", "visualize"));
  } catch {
    /* npm unavailable */
  }

  for (const candidate of candidates) {
    try {
      await readFile(path.join(candidate, "graph.js"), "utf8");
      return candidate;
    } catch {
      /* try next candidate */
    }
  }

  throw new Error(
    "OpenWiki not found. Install it with: npm install -g openwiki@0.3.1",
  );
}

async function main() {
  const visualizeDist = await resolveOpenWikiDist();
  const { buildGraph } = await import(
    pathToFileURL(path.join(visualizeDist, "graph.js")).href
  );
  const { PAGE } = await import(
    pathToFileURL(path.join(visualizeDist, "page.js")).href
  );

  const ignorePatterns = await loadVisualizerIgnorePatterns();
  const graph = filterGraph(await buildGraph(wikiRoot), ignorePatterns);
  await mkdir(outDir, { recursive: true });

  let clientJs = await readFile(path.join(visualizeDist, "client.js"), "utf8");
  clientJs = clientJs.replace(
    'const res = await fetch("/api/graph");',
    'const res = await fetch("./graph.json");',
  );
  clientJs = clientJs.replace(
    /function connectSSE\(\) \{[\s\S]*?\}\n/,
    `function connectSSE() {
  $("#live-text").textContent = "Static";
  $("#live").classList.add("stale");
}
`,
  );

  let pageHtml = PAGE.replace(
    'src="/client.js"',
    'src="./client.js"',
  );

  await writeFile(path.join(outDir, "graph.json"), JSON.stringify(graph));
  await writeFile(path.join(outDir, "index.html"), pageHtml);
  await writeFile(path.join(outDir, "client.js"), clientJs);
  await cp(path.join(visualizeDist, "client-lib.js"), path.join(outDir, "client-lib.js"));

  process.stdout.write(
    `Built static wiki: ${graph.nodes.length} pages → ${outDir}\n`,
  );
}

main().catch((err) => {
  process.stderr.write(`${err.stack ?? err}\n`);
  process.exit(1);
});
