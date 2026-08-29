"""Regression tests for _port_split's parse fallbacks and the zero-pad merge.

_port_split turns a filename stem into (prefix, port).  The prefix doubles as
the route/direction group for _neighbor_decay, and the port is the gap
coordinate.  A sweep over every .sor filename on disk found three defects:

  NO PORT PARSED.  A trailing run of non-digits hides the port from
    _PORT_TAIL_RE, so 'DNW5DNW10271withstartstop' returned (whole-name, None)
    and every file in the folder became its own one-member group.  347 files
    across 9 folders, 331 of them in folders where EVERY file collapsed.
    Ports under 100 do the same: 'RCHDNW-A-66' is only two digits.

  GREEDY TAIL.  Inconsistent zero-padding lets the 4-digit tail eat a padding
    zero: 'PTL5PTL1sh0232' -> ('PTL5PTL1sh', 232) but 'PTL5PTL1sh00309' ->
    ('PTL5PTL1sh0', 309), orphaning one file into its own group.

Both were FAIL-SAFE for _neighbor_decay rather than wrong-answer: a file with
no port is excluded from both buckets, and when every file is portless the
buckets empty and the rule returns None.  They made the rule silently blind,
not misrouted.  See test_no_port_is_fail_safe_not_wrong.

The ROUTE under-split (one filename prefix spanning two GenParams routes) was
investigated and deliberately NOT fixed: the serials argument added in PR #122
already separates 18 of the 24 cases, the same-instrument ones never reach
_neighbor_decay at all, and forcing the measurement on one of them moves the
drop by 0.002 against a 0.30 trigger.  GenParams is not a safe substitute for
the filename either - BKF<->DEL carries no location codes at all, EMVSUI's own
codes disagree with themselves ('SUI->EMV' vs 'SUISUN->EMERYVILLE'), and
Duran<->Ancho had both directions carrying identical A->B codes.

Namespace isolation rule: the engine is only exercised through subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script: str):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR)],
                       capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_SPLIT_SCRIPT = r"""
import sys, json, re
sys.path.insert(0, sys.argv[1])
from report_sor import _port_split, _merge_zero_pad_prefixes, _neighbor_decay
import numpy as np

# The parser exactly as it shipped before the fix, to prove additivity.
_WL = re.compile(r'(\d{3,4})_\d{3,4}\b')
_TAIL = re.compile(r'(\d{3,4})$')
def OLD(name):
    m = _WL.search(name) or _TAIL.search(name)
    if not m:
        return name, None
    return name[:m.start(1)], int(m.group(1))

out = {}
NAMES = [
    # the three literals the original unit test locks
    'ELMMIL0001_1550', 'BCK1BCK60145', 'NOPORT',
    # real stems from disk that used to parse, and must be untouched
    'SEANOR058_1550', 'EMVSUI0079_1550', 'NILMEC498_1550', 'MILELMsh0793_1310',
    'WSC_SUI_0620', 'PTL5PTL1sh0232',
    # the ones the fix is for
    'DNW5DNW10271withstartstop', 'DNW1DNW50007withstartstop',
    'RCHDNW-A-66', 'DNWRCH-A-55', 'DELBKF071_TABLEPOKE', 'PTL5PTL1sh00309',
    # must stay unparsed: a word ending in a digit is not a port
    'panel1', 'launch_set', '04_our_trace',
]
out['old'] = {n: list(OLD(n)) for n in NAMES}
out['new'] = {n: list(_port_split(n)) for n in NAMES}

# Zero-pad merge is folder-scoped and data-driven.
out['merge_fires'] = _merge_zero_pad_prefixes(
    ['PTL5PTL1sh', 'PTL5PTL1sh0', 'PTL5PTL1sh'])
out['merge_no_sibling'] = _merge_zero_pad_prefixes(['ABC0', 'ABC0', 'ABC0'])
out['merge_chain'] = _merge_zero_pad_prefixes(['X', 'X0', 'X00'])

# Fail-safety of the OLD behaviour, with an adversarial r-matrix built to
# give a perfect near=1.0 / far=0.0 decay if any pair were eligible.
old_names = ['DNW5DNW1%04dwithstartstop' % i for i in range(1, 61)]
K = len(old_names)
r = np.zeros((K, K))
for i in range(K):
    for j in range(K):
        r[i, j] = 1.0 if abs(i - j) <= 3 else 0.0
np.fill_diagonal(r, 1.0)
sers = ['991595'] * K
# Names NEITHER parser can read (no trailing digits, no separator+digits)
# stand in for what the whole Dinwiddie folder looked like before the fix.
portless = ['DNW5DNW1withstartstop'] * K
out['portless_decay_is_none'] = _neighbor_decay(portless, r, sers) is None
# One portless file among parseable ones must be dropped from the buckets,
# not sentinel-matched against the other portless one.
mixed = old_names[:-2] + ['alpha', 'beta']
d_all = _neighbor_decay(old_names, r, sers)
d_mix = _neighbor_decay(mixed, r, sers)
out['mixed_drops_only_the_portless'] = [d_all[2] - d_mix[2], d_all[3] - d_mix[3]]
out['new_decay_measures'] = d_all is not None
print(json.dumps(out))
"""


def test_every_name_that_parsed_before_parses_identically():
    """The fix is strictly additive: the two original patterns are tried
    first and unchanged, so no working parse can move."""
    out = _run(_SPLIT_SCRIPT)
    for name, old in out["old"].items():
        if old[1] is not None:
            assert out["new"][name] == old, (name, old, out["new"][name])


def test_the_three_locked_literals_are_unchanged():
    out = _run(_SPLIT_SCRIPT)
    assert out["new"]["ELMMIL0001_1550"] == ["ELMMIL", 1]
    assert out["new"]["BCK1BCK60145"] == ["BCK1BCK6", 145]
    assert out["new"]["NOPORT"] == ["NOPORT", None]


def test_trailing_suffix_no_longer_hides_the_port():
    """318 Dinwiddie files parsed to (whole-name, None), so every file became
    its own group.  The suffix goes back on the PREFIX so a folder mixing
    'X0001' and 'X0001withstartstop' still gets two groups."""
    out = _run(_SPLIT_SCRIPT)
    assert out["new"]["DNW5DNW10271withstartstop"] == ["DNW5DNW1withstartstop", 271]
    assert out["new"]["DNW1DNW50007withstartstop"] == ["DNW1DNW5withstartstop", 7]
    assert out["new"]["DELBKF071_TABLEPOKE"] == ["DELBKF_TABLEPOKE", 71]


def test_two_digit_ports_parse():
    out = _run(_SPLIT_SCRIPT)
    assert out["new"]["RCHDNW-A-66"] == ["RCHDNW-A-", 66]
    assert out["new"]["DNWRCH-A-55"] == ["DNWRCH-A-", 55]


def test_a_word_ending_in_a_digit_is_not_a_port():
    """The separator lookbehind is what stops 'panel1' and the FR probe
    stems from donating their last digit.  Without it the short-tail rule
    re-parses 10 more stems that should stay alone."""
    out = _run(_SPLIT_SCRIPT)
    for n in ("panel1", "launch_set", "04_our_trace"):
        assert out["new"][n][1] is None, (n, out["new"][n])


def test_zero_pad_merge_is_folder_scoped_and_data_driven():
    out = _run(_SPLIT_SCRIPT)
    assert out["merge_fires"] == ["PTL5PTL1sh"] * 3
    # A prefix that legitimately ends in 0 with no shorter sibling is safe.
    assert out["merge_no_sibling"] == ["ABC0"] * 3
    assert out["merge_chain"] == ["X", "X", "X"]


def test_no_port_is_fail_safe_not_wrong():
    """Proof a portless file goes BLIND rather than wrong: with an r-matrix
    built to give a perfect near/far decay, a folder of unparseable names
    yields no eligible pairs at all and _neighbor_decay returns None."""
    out = _run(_SPLIT_SCRIPT)
    assert out["portless_decay_is_none"] is True
    assert out["new_decay_measures"] is True
    # Portless files come OUT of both buckets rather than colliding on the
    # -1 sentinel, so removing two of them can only shrink the counts.
    d_near, d_far = out["mixed_drops_only_the_portless"]
    assert d_near > 0 and d_far > 0, out["mixed_drops_only_the_portless"]


def test_source_locks_the_parse_fallbacks():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "_PORT_SUFFIX_RE" in src
    assert "_PORT_SHORT_TAIL_RE" in src
    # The lookbehind is load-bearing; without it 'panel1' parses.
    assert r"(?<=[^0-9A-Za-z])(\d{1,2})$" in src
    # The original patterns must still be tried FIRST or additivity is lost.
    i = src.index("def _port_split(")
    body = src[i:i + 1200]
    assert body.index("_PORT_WL_RE.search") < body.index("_PORT_SUFFIX_RE.search")
    assert body.index("_PORT_SUFFIX_RE.search") < body.index("_PORT_SHORT_TAIL_RE.search")
    # The merge must actually be wired into the decay bucketing.
    assert "prefixes = _merge_zero_pad_prefixes(prefixes)" in src
