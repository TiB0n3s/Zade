# Governed build-runner images

These definitions create the exact local tags used by Zade's Python and Node
verification profiles:

- `python:3.12-local`
- `node:22-local`

Both official bases are digest-pinned. The Python image adds only `pytest`; the
Node image uses the npm bundled with Node 22. Neither image installs project
dependencies. A project must already contain everything its approved test
scripts require, or use a separately reviewed project-specific image.

Build and smoke-test both images from the repository root:

```powershell
.\scripts\build-runner-images.ps1
```

The script is a founder-run setup operation. Zade's runtime never pulls or
builds images automatically. Runtime containers have no network, a read-only
root filesystem, dropped capabilities, no privilege escalation, a PID limit,
and a bounded temporary filesystem. The assessed workspace is the only writable
bind mount.
