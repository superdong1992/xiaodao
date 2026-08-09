#!/usr/bin/env node
process.env.TEST_FLOW_FIRST_PARTY_CLIENT = "windows";
process.env.TEST_FLOW_FIRST_PARTY_HOST_PLATFORM = "win32";
process.env.TEST_FLOW_FIRST_PARTY_DOCKER_CONTEXT = "default";
await import("./cross-job-core.mjs");
