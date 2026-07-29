"""firekeep-client — Firekeep portable client kit.

`__version__` is the CLIENT KIT's version, released on `client-v*` tags. It is
NOT shared with the server, which releases independently on `v[0-9]+.[0-9]+.[0-9]+`
(.github/workflows/release.yml vs server-release.yml). Comparing the two for
equality is meaningless — doing so is what made `firekeep doctor` emit a
`version-skew: warn` on every correct install. `firekeep doctor` now reports both
versions without a verdict (`_check_versions`) and judges staleness against the
release manifest instead (`_check_client_version`).
"""

__version__ = "0.1.27"
