# Third-Party Notices

Argus is licensed under the MIT License. See [LICENSE](LICENSE).

This file lists declared Python runtime dependencies and common transitive
packages resolved during release smoke. Package versions may change when users
install Argus before a lockfile or pinned release bundle is used. Re-check this
file before every public release.

## Logo Assets

README platform logo SVGs are sourced from Simple Icons and LobeHub Icons.
Simple Icons is licensed under CC0-1.0. LobeHub Icons is licensed under MIT.
Slack, WhatsApp, Telegram, and Gmail logo SVGs are copied from SVG Repo.
Product names and marks remain property of their respective owners.

- Simple Icons: <https://simpleicons.org/>
- LobeHub Icons: <https://github.com/lobehub/lobe-icons>
- SVG Repo: <https://www.svgrepo.com/>

## Declared Runtime Dependencies

| Package | License metadata | Project |
|---|---|---|
| `psycopg` | LGPL-3.0-only | <https://github.com/psycopg/psycopg> |
| `psycopg-binary` | LGPL-3.0-only | <https://github.com/psycopg/psycopg> |
| `pydantic` | MIT | <https://github.com/pydantic/pydantic> |
| `PyYAML` | MIT | <https://github.com/yaml/pyyaml> |
| `httpx` | BSD-3-Clause | <https://github.com/encode/httpx> |

## Common Transitive Dependencies

| Package | License metadata | Project |
|---|---|---|
| `annotated-types` | MIT | <https://github.com/annotated-types/annotated-types> |
| `anyio` | MIT | <https://github.com/agronholm/anyio> |
| `certifi` | MPL-2.0 | <https://github.com/certifi/python-certifi> |
| `h11` | MIT | <https://github.com/python-hyper/h11> |
| `httpcore` | BSD-3-Clause | <https://github.com/encode/httpcore> |
| `idna` | BSD-3-Clause | <https://github.com/kjd/idna> |
| `pydantic-core` | MIT | <https://github.com/pydantic/pydantic/tree/main/pydantic-core> |
| `typing-extensions` | PSF-2.0 | <https://github.com/python/typing_extensions> |
| `typing-inspection` | MIT | <https://github.com/pydantic/typing-inspection> |

## Operator Notes

- Argus does not vendor these packages in this repository.
- The Docker image installs packages from Python package indexes at build time.
- `psycopg-binary` is convenient for local setup. Operators with stricter
  packaging requirements can build from source or provide their own image.
