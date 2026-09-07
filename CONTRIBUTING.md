# Contributing

This is an independently maintained VMware fork. Substantial VMware changes start with an
approved implementation plan and explicit implementation, commit, push and deployment scope.
Do not create an Issue or publish a message automatically. Contributions to the authoritative
upstream follow its Issue and `develop` workflow separately.

Keep device operations fail-closed, never add real subscriber data to fixtures, and preserve
upstream attribution. Integrate released upstream commits by ordinary merge into an isolated
branch, then fast-forward the reviewed result to `vmware`; `main` remains the upstream mirror.
Commit messages follow this repository's `AGENTS.md`, using `【苏忆】` and 1–5 numbered items.

Before submitting a change:

```bash
bash -n bootstrap.sh install.sh scripts/mddctl engine/entrypoint.sh
python3 -m compileall -q control engine host scripts tests
python3 -m unittest discover -s tests -p 'test_*.py'
sh tools/check-subscriber-identifiers.sh
cd webui
npm ci
npm run build
```

Use focused commits and add tests for routing, authentication, device state, installer rollback and
secret redaction. Engine-input changes also require an amd64 no-cache Engine build and TUN/NET_ADMIN
gate in a supported Linux guest. Do not add workflows, Release assets, Docker Control, or prebuilt
project archives to the `vmware` branch.

For this delivery, enable the privacy hook only for the push command; keep clone and global
Git settings unchanged:

```bash
git -c core.hooksPath=hooks push origin vmware
```

The hook scans all newly introduced blobs, including values removed by later commits. Do not
bypass a failure or rewrite deployed history; inspect the finding before continuing.
