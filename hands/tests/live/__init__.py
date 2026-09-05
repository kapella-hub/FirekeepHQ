"""Live tests: they drive the real machine and are skipped unless
`FIREKEEP_HANDS_LIVE=1`. They are a package so the default run
(`testpaths = ["tests"]`) can collect and skip them without importing any
platform framework."""
