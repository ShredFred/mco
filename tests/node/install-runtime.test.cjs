const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const runtime = require("../../scripts/install-runtime.js");
const { spawnExecutable } = require("../../scripts/exec-util.js");

test("buildNpmInstallArgv pins exact package version", () => {
  assert.deepEqual(runtime.buildNpmInstallArgv("0.10.9"), [
    "install",
    "-g",
    "@tt-a1i/mco@0.10.9",
  ]);
});

test("resolveGlobalMcoScript uses npm root -g on Unix", () => {
  const runner = (command, args) => {
    if (command === "npm" && args[0] === "root") {
      return { status: 0, stdout: "/opt/homebrew/lib/node_modules\n", stderr: "", error: null };
    }
    return { status: 1, stdout: "", stderr: "", error: null };
  };
  const expected = "/opt/homebrew/lib/node_modules/@tt-a1i/mco/bin/mco.js";
  const scriptPath = runtime.resolveGlobalMcoScript(runner, {
    existsSync: (candidate) => candidate === expected,
  });
  assert.equal(scriptPath, expected);
});

test("resolveGlobalMcoScript returns null when entry is missing", () => {
  const runner = (command, args) => {
    if (command === "npm" && args[0] === "root") {
      return { status: 0, stdout: "/opt/homebrew/lib/node_modules\n", stderr: "", error: null };
    }
    return { status: 1, stdout: "", stderr: "", error: null };
  };
  assert.equal(runtime.resolveGlobalMcoScript(runner, { existsSync: () => false }), null);
});

test("resolveGlobalMcoScript uses Windows npm prefix fallback", () => {
  const prefix = "C:\\Users\\dev\\AppData\\Roaming\\npm";
  const expected = path.win32.join(prefix, "node_modules", "@tt-a1i", "mco", "bin", "mco.js");
  const runner = (command, args) => {
    if (command === "npm" && args[0] === "root") {
      return { status: 1, stdout: "", stderr: "", error: null };
    }
    if (command === "npm" && args[0] === "prefix") {
      return { status: 0, stdout: `${prefix}\r\n`, stderr: "", error: null };
    }
    return { status: 1, stdout: "", stderr: "", error: null };
  };
  assert.equal(runtime.resolveGlobalMcoScript(runner, {
    platform: "win32",
    existsSync: (candidate) => candidate === expected,
  }), expected);
});

test("skills CLI dependency is pinned to the tested version", () => {
  assert.equal(runtime.SKILLS_CLI_PACKAGE, "skills@1.5.15");
  assert.equal(runtime.buildSkillsCliAddArgv("/pkg/mco", ["codex"])[2], "skills@1.5.15");
});

test("resolveGlobalMcoScript allows dry-run placeholder", () => {
  const runner = () => ({ status: 1, stdout: "", stderr: "", error: null });
  assert.equal(
    runtime.resolveGlobalMcoScript(runner, { allowPlaceholder: true, existsSync: () => false }),
    runtime.DRY_RUN_MCO_PLACEHOLDER,
  );
});

test("detectCallingAgents maps installed binaries to skill agent ids", () => {
  const runner = (command, args) => {
    if (command === "which" && args[0] === "claude") {
      return { status: 0, stdout: "/usr/local/bin/claude\n", stderr: "", error: null };
    }
    if (command === "which" && args[0] === "agent") {
      return { status: 0, stdout: "/usr/local/bin/agent\n", stderr: "", error: null };
    }
    if (command === "which" && args[0] === "pi") {
      return { status: 0, stdout: "/usr/local/bin/pi\n", stderr: "", error: null };
    }
    return { status: 1, stdout: "", stderr: "", error: null };
  };
  assert.deepEqual(runtime.detectCallingAgents(runner), ["claude-code", "cursor", "pi"]);
});

test("normalizeSkillAgents accepts supported MCO calling agents", () => {
  assert.deepEqual(
    runtime.normalizeSkillAgents(["pi", "hermes-agent", "github-copilot", "qwen-code"]),
    ["pi", "hermes-agent", "github-copilot", "qwen-code"],
  );
});

test("normalizeSkillAgents rejects unknown ids", () => {
  assert.throws(
    () => runtime.normalizeSkillAgents(["gemini"]),
    /unknown skill agent: gemini/,
  );
});

test("runMcoScript uses node for .js entrypoints", () => {
  const records = [];
  const runner = (command, args) => {
    records.push({ command, args });
    return { status: 0, stdout: "{}", stderr: "", error: null };
  };
  runtime.runMcoScript("/tmp/npm-global/node_modules/@tt-a1i/mco/bin/mco.js", ["skills", "status"], runner);
  assert.equal(records[0].command, process.execPath);
  assert.equal(records[0].args[0], "/tmp/npm-global/node_modules/@tt-a1i/mco/bin/mco.js");
});

test("runMcoScript fails when placeholder used without allowPlaceholder", () => {
  const result = runtime.runMcoScript(runtime.DRY_RUN_MCO_PLACEHOLDER, ["skills", "sync"], () => ({
    status: 0,
    stdout: "",
    stderr: "",
    error: null,
  }));
  assert.equal(result.status, 1);
  assert.equal(result.failure, "global_mco_not_found");
});

test("spawnExecutable preserves arbitrary argv for native executables", () => {
  const args = ["two words", 'say "hi"', "100% ready", "a&b", "a|b", "a^b"];
  const result = spawnExecutable(
    process.execPath,
    ["-e", "process.stdout.write(JSON.stringify(process.argv.slice(1)))", ...args],
  );

  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), args);
});

test("spawnExecutable safely preserves argv through a Windows cmd shim", {
  skip: process.platform !== "win32",
}, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "mco cmd shim "));
  const capture = path.join(root, "capture.js");
  const shim = path.join(root, "mco-argv-test.cmd");
  const marker = path.join(root, "injected.txt");
  const args = [
    "two words",
    'say "hi"',
    "100% ready",
    "a^b",
    "a|b",
    "a>b",
    `safe&echo PWNED>"${marker}"`,
  ];

  try {
    fs.writeFileSync(capture, "process.stdout.write(JSON.stringify(process.argv.slice(2)));\n");
    fs.writeFileSync(shim, '@echo off\r\n"%NODE_EXE%" "%~dp0capture.js" %*\r\n');
    const env = {
      ...process.env,
      NODE_EXE: process.execPath,
      Path: `${root};${process.env.Path || process.env.PATH || ""}`,
      PATHEXT: ".COM;.EXE;.BAT;.CMD",
    };

    const result = spawnExecutable("mco-argv-test", args, { env });

    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), args);
    assert.equal(fs.existsSync(marker), false, "cmd.exe executed an argument as shell input");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
