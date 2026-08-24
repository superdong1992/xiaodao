#!/usr/bin/env node
process.env.TEST_FLOW_FIRST_PARTY_CLIENT = "linux";
process.env.TEST_FLOW_FIRST_PARTY_HOST_PLATFORM = "linux";
process.env.TEST_FLOW_FIRST_PARTY_DOCKER_CONTEXT = "default";
process.env.TEST_FLOW_FIRST_PARTY_TOPOLOGY = "host-client";
await import("./cross-job-core.mjs");
