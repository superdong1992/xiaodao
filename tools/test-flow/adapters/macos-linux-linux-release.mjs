#!/usr/bin/env node
// Darwin is only the orchestrator. Both the first-party Client and the Server
// execute in distinct Linux/amd64 containers on one run-owned Docker network.
process.env.TEST_FLOW_FIRST_PARTY_CLIENT = "linux";
process.env.TEST_FLOW_FIRST_PARTY_HOST_PLATFORM = "darwin";
process.env.TEST_FLOW_FIRST_PARTY_DOCKER_CONTEXT = "colima";
process.env.TEST_FLOW_FIRST_PARTY_TOPOLOGY = "dual-linux-containers";
await import("./cross-job-core.mjs");
