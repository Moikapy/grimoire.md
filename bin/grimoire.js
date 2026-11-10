#!/usr/bin/env node
// Grimoire is a Python program. This finds Python 3.10+ and hands it the terminal.
"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");

const script = path.join(__dirname, "..", "grimoire.py");
const wanted = [3, 10];
const candidates = [process.env.GRIMOIRE_PYTHON, "python3", "python"].filter(Boolean);
if (process.platform === "win32") candidates.push("py"); // the Python launcher

function recentEnough(command) {
  const probe = spawnSync(
    command,
    ["-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
    { encoding: "utf8" },
  );
  if (probe.error || probe.status !== 0) return false;
  const [major, minor] = probe.stdout.trim().split(" ").map(Number);
  return major > wanted[0] || (major === wanted[0] && minor >= wanted[1]);
}

const python = candidates.find(recentEnough);
if (!python) {
  console.error(`grimoire: needs Python ${wanted.join(".")} or newer on PATH (or set GRIMOIRE_PYTHON)`);
  process.exit(127);
}

// The reader owns ctrl+c and ctrl+z (undo) while it runs. Node only waits.
// Without a SIGTSTP handler the shell would stop Node and leave the reader running.
for (const name of ["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT", "SIGTSTP"]) {
  try {
    process.on(name, () => {});
  } catch {
    // Windows does not have every one of these
  }
}

const result = spawnSync(python, [script, ...process.argv.slice(2)], { stdio: "inherit" });
if (result.error) {
  console.error(`grimoire: ${result.error.message}`);
  process.exit(1);
}
if (result.signal) process.kill(process.pid, result.signal);
process.exit(result.status === null ? 1 : result.status);
