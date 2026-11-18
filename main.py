#!/usr/bin/env python3
"""Forecast a game launch day's hourly Twitch viewers (hours 2..24) as
five quantiles per hour, from the launch's hour-1 print and a pool of
comparable past launches' release-day curves.

Model: each comparable contributes a log-ratio profile
    d_h = log(viewers_h) - log(viewers_1)   (h = 2..24, UTC-aligned)
Comparables are weighted by similarity to the target: hour-1 scale
(log distance), pre-launch viewer level relative to hour 1, and whether
they launched on PC as well as console.  The forecast per hour is a
pooled location/scale fit: a weighted median profile plus a per-hour
spread, with the five quantile offsets taken from the pooled
standardised residuals across all hours and comparables.  The quantile
curves are lightly smoothed across neighbouring hours, clamped to the
comparables' observed envelope, and anchored at the target's hour-1
print.  Fully deterministic, standard library only, no network.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
Z = (-1.6448536269514722, -0.6744897501960817, 0.0,
     0.6744897501960817, 1.6448536269514722)

SCALE_SIGMA = 1.0    # weight decay for |log(v1_comp) - log(p1)|
PRE_SIGMA = 1.2      # weight decay for pre-launch level mismatch
PLAT_W = 3.0         # weight multiplier when PC-at-launch matches target
SIGMA_FLOOR = 0.05   # minimum per-hour log spread
ENVELOPE_PAD = 0.35  # log slack beyond the pool's observed range per hour
MAX_Q = 1e9


def parse_ts(ts):
    return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(
        tzinfo=timezone.utc)


def fmt_ts(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def wquantile(items, tau):
    """Weighted quantile: linear interpolation on midpoint plotting
    positions.  items is a list of (value, weight)."""
    if not items:
        return None
    items = sorted(items)
    total = sum(w for _, w in items)
    if total <= 0:
        return items[len(items) // 2][0]
    pts = []
    cw = 0.0
    for v, w in items:
        pts.append(((cw + w / 2.0) / total, v))
        cw += w
    if tau <= pts[0][0]:
        return pts[0][1]
    if tau >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if pts[i][0] >= tau:
            p0, v0 = pts[i - 1]
            p1_, v1_ = pts[i]
            if p1_ == p0:
                return v1_
            return v0 + (v1_ - v0) * (tau - p0) / (p1_ - p0)
    return pts[-1][1]


def comp_day_map(comp):
    """Map hour offset from the comparable's day_start to viewers."""
    try:
        ds = parse_ts(comp['day_start'])
    except Exception:
        return {}
    out = {}
    for row in comp.get('hourly') or []:
        v = row.get('viewers')
        if v is None:
            continue
        try:
            off = (parse_ts(row['from']) - ds).total_seconds() / 3600.0
        except Exception:
            continue
        if off == int(off):
            out[int(off)] = v
    return out


def comp_features(comp):
    """(profile, v1, pre_mean, has_pc, is_launch) for one comparable."""
    day = comp_day_map(comp)
    v1 = day.get(0)
    unlock = (comp.get('unlock') or '').lower()
    is_launch = 'not a launch' not in unlock
    if v1 is None or v1 <= 0:
        return None, None, None, None, is_launch
    prof = {}
    for h in range(2, 25):
        v = day.get(h - 1)
        if v is not None and v > 0:
            prof[h] = math.log(v) - math.log(v1)
    pre_vals = [day[k] for k in range(-14, 0)
                if day.get(k) is not None and day[k] > 0]
    pre_mean = sum(pre_vals) / len(pre_vals) if pre_vals else None
    has_pc = 'PC' in (comp.get('platforms') or '')
    return prof, v1, pre_mean, has_pc, is_launch


def target_features(case):
    """(p1, pre_mean, has_pc) for the case being forecast."""
    p1 = None
    for row in case.get('known') or []:
        if row.get('viewers') is not None:
            p1 = row['viewers']
            break
    if p1 is None:
        hist = case.get('history') or []
        for row in reversed(hist):
            if row.get('viewers') is not None:
                p1 = row['viewers']
                break
    if p1 is None or p1 <= 0:
        p1 = 1
    ds = parse_ts(case['day_start'])
    pre_vals = []
    for row in case.get('history_hourly') or []:
        try:
            start = parse_ts(row['from'])
        except Exception:
            continue
        off = (start - ds).total_seconds() / 3600.0
        if -14 <= off < 0 and row.get('viewers') is not None \
                and row['viewers'] > 0:
            pre_vals.append(row['viewers'])
    pre_mean = sum(pre_vals) / len(pre_vals) if pre_vals else None
    # Target platforms: a comparable named after the target game (the
    # target category's own history entry) carries its platform list.
    gname = ((case.get('game') or {}).get('name') or '').lower()
    has_pc = None
    for comp in case.get('comparables') or []:
        cname = (comp.get('name') or '').lower()
        if gname and cname.startswith(gname):
            has_pc = 'PC' in (comp.get('platforms') or '')
            break
    if has_pc is None:
        notes = (case.get('notes') or '').lower()
        if 'ps5' in notes or 'playstation' in notes or 'xbox' in notes \
                or 'switch' in notes:
            has_pc = (' pc ' in ' ' + notes + ' ') \
                and 'console only' not in notes
        else:
            has_pc = False
    return p1, pre_mean, has_pc


def persistence(p1):
    p = max(float(p1), 1.0)
    return {h: [p * math.exp(z * 0.15 * math.sqrt(h - 1)) for z in Z]
            for h in range(2, 25)}


def forecast(case):
    """Return {hour: [q05, q25, q50, q75, q95]} for hours 2..24."""
    p1, t_pre, t_pc = target_features(case)
    p = max(float(p1), 1.0)
    gname = ((case.get('game') or {}).get('name') or '').lower()
    profs = []
    for comp in case.get('comparables') or []:
        cname = (comp.get('name') or '').lower()
        if gname and cname.startswith(gname):
            continue  # the target's own category entry is not a launch
        prof, v1, c_pre, c_pc, is_launch = comp_features(comp)
        if not is_launch or not prof or len(prof) < 8:
            continue
        w = 1.0
        dd = math.log(v1) - math.log(p)
        w *= math.exp(-(dd * dd) / (2.0 * SCALE_SIGMA ** 2))
        if t_pre and c_pre:
            dd = math.log(c_pre / v1) - math.log(t_pre / p)
            w *= math.exp(-(dd * dd) / (2.0 * PRE_SIGMA ** 2))
        if c_pc == t_pc:
            w *= PLAT_W
        if w > 1e-9:
            profs.append((prof, w))
    if not profs:
        return persistence(p)
    med, sig, env = {}, {}, {}
    for h in range(2, 25):
        items = [(d[h], w) for d, w in profs if h in d]
        if not items:
            continue
        med[h] = wquantile(items, 0.5)
        q25 = wquantile(items, 0.25)
        q75 = wquantile(items, 0.75)
        sig[h] = max((q75 - q25) / 1.349, SIGMA_FLOOR)
        env[h] = (min(v for v, _ in items) - ENVELOPE_PAD,
                  max(v for v, _ in items) + ENVELOPE_PAD)
    if not med:
        return persistence(p)
    resid = []
    for d, w in profs:
        for h, v in d.items():
            if h in med:
                resid.append(((v - med[h]) / sig[h], w))
    rq = [wquantile(resid, t) for t in LEVELS]
    logq = {}
    for h in range(2, 25):
        if h not in med:
            logq[h] = [0.0] * 5
            continue
        lo, hi = env[h]
        logq[h] = [min(max(med[h] + sig[h] * r, lo), hi) for r in rq]
    # light smoothing of each quantile curve across neighbouring hours
    sm = {}
    for h in range(2, 25):
        row = []
        for i in range(5):
            num, den = 0.0, 0.0
            for dh, wgt in ((-1, 0.25), (0, 0.5), (1, 0.25)):
                hh = h + dh
                if hh in logq:
                    num += wgt * logq[hh][i]
                    den += wgt
            row.append(num / den if den else 0.0)
        sm[h] = row
    out = {}
    for h in range(2, 25):
        qs = []
        prev = 0.0
        for dq in sm[h]:
            v = p * math.exp(dq)
            v = min(max(v, prev), MAX_Q)
            if v <= 0:
                v = prev if prev > 0 else 1.0
            qs.append(round(v, 2))
            prev = qs[-1]
        out[h] = qs
    return out


def run(indir, outdir):
    names = sorted(n for n in os.listdir(indir) if n.endswith('.in'))
    for name in names:
        with open(os.path.join(indir, name), encoding='utf-8') as f:
            case = json.load(f)
        fc = forecast(case)
        doc = {'hours': [{'hour': h, 'q': fc[h]} for h in range(2, 25)]}
        out_path = os.path.join(outdir, name[:-3] + '.out')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(doc, separators=(',', ':')))


def main():
    indir = os.environ.get('MAKEFUN_INPUT_DIR', '/input')
    outdir = os.environ.get('MAKEFUN_OUTPUT_DIR', '/output')
    os.makedirs(outdir, exist_ok=True)
    run(indir, outdir)


if __name__ == '__main__':
    main()
