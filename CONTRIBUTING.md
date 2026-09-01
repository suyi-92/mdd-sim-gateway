# Contributing

Open an issue before a large behavior or hardware change. Keep device operations fail-closed, never add real subscriber data to fixtures, and preserve upstream attribution.

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
