#!/usr/bin/env node
process.env.TEST_FLOW_FIRST_PARTY_CLIENT = "macos";
process.env.TEST_FLOW_FIRST_PARTY_HOST_PLATFORM = "darwin";
process.env.TEST_FLOW_FIRST_PARTY_DOCKER_CONTEXT = "colima";
await import("./cross-job-core.mjs");
