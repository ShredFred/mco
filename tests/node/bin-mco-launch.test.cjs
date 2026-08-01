const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

test("ordinary commands invoke the resolved compatible Python with package mco script", () => {
  const { launch } = require("../../bin/mco.js");
  const records = [];
  const runner = (command, args, options) => {
    records.push({ command, args, options });
    return { status: 0, error: null };
  };
  const exits = [];
  launch(["--help"], {
    runner,
    python: "python3.12",
    exit: (code) => exits.push(code),
  });
  assert.equal(records.length, 1);
  assert.deepEqual(exits, [0]);
  assert.equal(records[0].command, "python3.12");
  assert.equal(records[0].args[0], path.resolve(__dirname, "../..", "mco"));
  assert.equal(records[0].args[1], "--help");
});

test("Python resolver uses a current versioned Python before macOS system Python 3.9", () => {
  const { resolvePython } = require("../../bin/mco.js");
  const runner = (command, args) => {
    if (command === "python3.14") {
      return { status: 0, stdout: "", stderr: "", error: null };
    }
    if (command === "python3") {
      return { status: 1, stdout: "", stderr: "", error: null };
    }
    return { status: 1, stdout: "", stderr: "", error: null };
  };
  assert.equal(resolvePython({ runner, env: {} }), "python3.14");
});

test("Python resolver honors a compatible MCO_PYTHON override", () => {
  const { resolvePython } = require("../../bin/mco.js");
  const runner = (command) => ({
    status: command === "/opt/custom/python" ? 0 : 1,
    stdout: "",
    stderr: "",
    error: null,
  });
  assert.equal(
    resolvePython({ runner, env: { MCO_PYTHON: "/opt/custom/python" } }),
    "/opt/custom/python",
  );
});

test("Python resolver uses the Windows py launcher executable", () => {
  const { resolvePython } = require("../../bin/mco.js");
  const pythonPath = "C:\\Python314\\python.exe";
  const runner = (command) => {
    if (command === "py") {
      return { status: 0, stdout: `${pythonPath}\n`, stderr: "", error: null };
    }
    if (command === pythonPath) {
      return { status: 0, stdout: "", stderr: "", error: null };
    }
    return { status: 1, stdout: "", stderr: "", error: null };
  };
  assert.equal(resolvePython({ runner, env: {} }), pythonPath);
});

test("Python resolver falls back to an installed uv runtime", () => {
  const { resolvePython } = require("../../bin/mco.js");
  const pythonPath = "/Users/test/.local/share/uv/python/python3.12";
  const runner = (command, args) => {
    if (command === "uv" && args[0] === "python") {
      return { status: 0, stdout: `${pythonPath}\n`, stderr: "", error: null };
    }
    if (command === pythonPath) {
      return { status: 0, stdout: "", stderr: "", error: null };
    }
    return { status: 1, stdout: "", stderr: "", error: null };
  };
  assert.equal(resolvePython({ runner, env: {} }), pythonPath);
});

test("Python resolver returns null when no compatible runtime exists", () => {
  const { resolvePython } = require("../../bin/mco.js");
  const runner = () => ({ status: 1, stdout: "", stderr: "", error: null });
  assert.equal(resolvePython({ runner, env: {} }), null);
});
