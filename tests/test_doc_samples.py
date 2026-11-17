"""Run the commands in the documentation and check the output it shows: see
tests/doc_samples.py for the rules, and tests/samples/ for the samples of each page."""

from pathlib import Path

import pytest

from tests.doc_samples import (
    BLOCKS,
    ILLUSTRATIVE,
    AUTO,
    Block,
    Sample,
    expected_statuses,
    matching,
    run_command,
    shown_block,
    skip_reason,
)
from tests.samples import SAMPLES

SAMPLES_BY_ID = {s.id: s for s in SAMPLES}
assert len(SAMPLES_BY_ID) == len(SAMPLES), "two samples have the same id"


def test_every_block_is_covered() -> None:
    problems = []
    own: dict[Block, list[Sample]] = {}  # block -> the samples that match it
    shown: dict[Block, list[Sample]] = {}  # output block -> the run samples that show it
    for sample in SAMPLES:
        found = matching(sample)
        if len(found) != 1:
            problems.append(
                f"{sample.id}: {sample.contains!r} matches"
                f" {len(found)} {sample.shown_category} blocks"
            )
            continue
        if sample.kind == "run":
            own.setdefault(found[0], []).append(sample)
            block = shown_block(sample)
            if block:
                shown.setdefault(block, []).append(sample)
            elif sample.shows not in (None, AUTO):
                problems.append(
                    f"{sample.id}: shows={sample.shows!r} matches no single output block"
                )
        else:
            own.setdefault(found[0], []).append(sample)
    for block in BLOCKS:
        if block.category == "example":
            continue
        mine, showing = own.get(block, []), shown.get(block, [])
        if len(mine) + bool(showing) == 0:
            problems.append(
                f"{block.where}: this {block.category} block has no sample in tests/samples/"
            )
        elif len(mine) + bool(showing) > 1:
            problems.append(
                f"{block.where}: this {block.category} block has"
                f" several samples: {[s.id for s in mine + showing]}"
            )
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "sample",
    [
        # pytest-timeout arms its timer before the test runs: the marker has to be here
        pytest.param(s, id=s.id, marks=pytest.mark.timeout(s.timeout + 30))
        for s in SAMPLES
        if s.kind in ("run", "output")
    ],
)
def test_sample(sample: Sample, tmp_path: Path) -> None:
    reason = skip_reason(sample)
    if reason:
        pytest.skip(reason)
    shown = shown_block(sample)
    assert not (shown and shown.title.startswith(ILLUSTRATIVE)), (
        f"{shown and shown.where}: a checked output must not be titled illustrative"
    )
    command = sample.command or matching(sample)[0].text
    out, status, timed_out = run_command(command, tmp_path, sample.timeout)
    assert timed_out == sample.hangs, f"timed_out={timed_out}:\n{out}"
    if not sample.hangs:
        assert status in expected_statuses(sample), f"exit status {status}:\n{out}"
    for text in sample.expect:
        assert text in out, f"{text!r} is not in the real output:\n{out}"
        if shown:
            assert text in shown.text, f"{text!r} is not in {shown.where}"
    if sample.exact:
        assert shown, f"{sample.id}: no output block to compare with"
        assert out.strip() == shown.text.strip(), f"differs from {shown.where}:\n{out}"
    if sample.check:
        sample.check(out)
        if sample.check_shown:
            assert shown, f"{sample.id}: no output block to check"
            sample.check(shown.text)


@pytest.mark.parametrize(
    "sample", [s for s in SAMPLES if s.kind == "illustrative"], ids=lambda s: s.id
)
def test_illustrative_block_says_so(sample: Sample) -> None:
    block = matching(sample)[0]
    assert block.title.startswith(ILLUSTRATIVE), (
        f'{block.where}: add title="{ILLUSTRATIVE}: {sample.why}" to the fence'
    )


def test_samples_say_why_they_are_not_run() -> None:
    assert [s.id for s in SAMPLES if s.kind in ("not-run", "illustrative") and not s.why] == []
