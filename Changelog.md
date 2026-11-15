# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased
- Packaging moved to `pyproject.toml` managed by uv; Python 3.12+
- Checks are ruff, mypy and pytest, run by `scripts/check`; CI moved to GitHub Actions
- Docker test image based on Ubuntu 24.04 packages
- Fix `Player.play_until_eos` on Python 3.11+

## 0.5.1 (2020-07-18)
- Remove pygobject-stubs as test dependency

## 0.5.0 (2020-07-14)
- Added typing
- Simplified event handling (Precursor for plugin based handling)
- Removed Bus wrapper
- Added basic error handling 

## 0.4.0 (2020-02-24)

- Refactored asyncplayer to use co-routines by default
- Using async bus-based events for asyncio state change 
