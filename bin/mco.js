#!/usr/bin/env node

const { spawnSync } = require("node:child_process");
const { resolve } = require("node:path");

function isCompatiblePython(command, runner, env) {
  const result = runner(
    command,
    ["-c", "import sys; raise SystemExit(sys.version_info < (3, 10))"],
    { encoding: "utf8", shell: false, env },
  );
  return !result.error && result.status === 0;
}

function resolvePython(deps = {}) {
  const runner = deps.runner || spawnSync;
  const env = deps.env || process.env;
  const candidates = [
    env.MCO_PYTHON,
    "python3.14",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
    "python",
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (isCompatiblePython(candidate, runner, env)) {
      return candidate;
    }
  }

  const pyResult = runner(
    "py",
    [
      "-3",
      "-c",
      "import sys; print(sys.executable); raise SystemExit(sys.version_info < (3, 10))",
    ],
    { encoding: "utf8", shell: false, env },
  );
  const pyPython = !pyResult.error && pyResult.status === 0
    ? String(pyResult.stdout || "").trim()
    : "";
  if (pyPython && isCompatiblePython(pyPython, runner, env)) {
    return pyPython;
  }

  const uvResult = runner("uv", ["python", "find", ">=3.10"], {
    encoding: "utf8",
    shell: false,
    env,
  });
  const uvPython = !uvResult.error && uvResult.status === 0
    ? String(uvResult.stdout || "").trim()
    : "";
  if (uvPython && isCompatiblePython(uvPython, runner, env)) {
    return uvPython;
  }
  return null;
}

function launch(args, deps = {}) {
  const exit = deps.exit || process.exit;
  if (args[0] === "install") {
    void require("../scripts/install-wizard.js")
      .main(args.slice(1), deps)
      .catch((err) => {
        console.error(String(err.message || err));
        process.exit(1);
      });
    return;
  }

  const scriptPath = resolve(__dirname, "..", "mco");
  const runner = deps.runner || spawnSync;
  const python = deps.python || resolvePython(deps);
  if (!python) {
    console.error("MCO requires Python 3.10 or newer. Install it or set MCO_PYTHON.");
    exit(1);
    return;
  }
  const result = runner(python, [scriptPath, ...args], {
    stdio: "inherit",
    shell: false,
  });

  if (result.error) {
    console.error(`Failed to run python3: ${result.error.message}`);
    exit(1);
    return;
  }

  exit(result.status === null ? 1 : result.status);
}

if (require.main === module) {
  launch(process.argv.slice(2));
}

module.exports = { launch, resolvePython };
