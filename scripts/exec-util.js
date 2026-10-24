"use strict";

const crossSpawn = require("cross-spawn");

function spawnExecutable(command, args, options = {}) {
  return crossSpawn.sync(command, args, {
    encoding: "utf8",
    shell: false,
    ...options,
  });
}

module.exports = { spawnExecutable };
