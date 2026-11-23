"""Skin a rendered target with legacy or bounded image-dependent brushstrokes."""
import hashlib
import json
import math
import numpy as np
from scipy.ndimage import gaussian_filter, gaussian_filter1d, sobel
from .util import blur
from . import limits, native

# stroke count, width range, length range, opacity, bristle, colour pickup blur, placed by error?
# Sizes are pixels at scale 2.4 (a 3840-wide canvas); the two finest passes stay at that size at any scale.
PASSES = [
    (6000, (80, 150), (250, 650), 0.96, 0.30, 26, False),
    (20000, (34, 60), (100, 240), 0.92, 0.30, 11, False),
    (70000, (15, 27), (46, 105), 0.90, 0.28, 5, True),
    (200000, (6.5, 12), (20, 44), 0.90, 0.26, 2.2, True),
    (500000, (3.2, 5.6), (9, 21), 0.90, 0.22, 1.0, True),
    (1000000, (1.8, 2.9), (5, 11), 0.90, 0.16, 0.5, True),
    (1200000, (1.2, 1.8), (3, 6.5), 0.92, 0.10, 0.0, True),
]


def _pass_counts(S):
    """(strokes per batch, batches) for each pass at effective scale S, exactly as paint() draws them."""
    ka = (S / 2.4) ** 2
    out = []
    for n, _, _, _, _, _, by_err in PASSES:
        n = max(int(n * ka), 1)
        nb = 4 if by_err else 1
        out.append((max(n // nb, 1), nb))
    return out


def planned_strokes(S):
    """Total brushstrokes paint() will lay across all passes at effective scale S."""
    return sum(m * nb for m, nb in _pass_counts(S))


def paint(TGT, REG, stroke_angle, S, horizon_y, rng, log, sea_region=1, grain=0.010, checkpoint=None,
          policy='legacy-seven', finish=None, config=None, max_strokes=None, max_passes=None,
          seed=None, decision_log=None):
    """TGT: (H, W, 3) reference in 0..1. REG: (H, W) region ids. Returns the painted canvas and stroke count.
    checkpoint(canvas, passes_done, strokes_done), if given, is called after each completed pass."""
    if policy == 'adaptive-v1':
        return _adaptive(TGT, REG, stroke_angle, S, horizon_y, rng, log, sea_region, grain,
                         checkpoint, finish or 'photographic', config, max_strokes, max_passes,
                         seed, decision_log)
    if policy != 'legacy-seven':
        raise ValueError('unknown paint policy: '+str(policy))
    if any(v is not None for v in (finish, config, max_strokes, max_passes)):
        raise ValueError('legacy-seven does not accept adaptive customization')
    limits.stage('paint (planning buffers)')
    H, W = REG.shape
    limits.check_pixels("paint (canvas)", W, H)
    limits.check_strokes("paint (planning the passes)", planned_strokes(S))   # before any stroke is generated
    region_ids, dense = np.unique(REG, return_inverse=True)
    dense = dense.reshape(REG.shape).astype(np.int32)
    nreg = len(region_ids)
    report = dict(policy=policy, schema_version=1, terminal_reason=None,
                  admitted_strokes=planned_strokes(S), attempted_strokes=0, completed_strokes=0,
                  region_ids=[int(k) for k in region_ids], mask_planes=nreg, decisions=[])
    limits.active().painting = report
    canvas = np.empty((H, W, 3), np.float32); canvas[:] = (0.30, 0.22, 0.22)
    height = np.zeros((H, W), np.float32)
    BR = gaussian_filter1d(rng.random(8192).astype(np.float32), 1.1, mode="wrap")
    BR = np.ascontiguousarray((BR - BR.min()) / (BR.max() - BR.min()), np.float32)
    masks = np.ascontiguousarray(np.stack([gaussian_filter((REG == k).astype(np.float32), 0.8) for k in region_ids]))
    # stroke direction: along the form, i.e. across the local colour gradient
    lumi = gaussian_filter(TGT @ np.array((0.3, 0.6, 0.1), np.float32), 1.5 * S)
    gx, gy = sobel(lumi, 1), sobel(lumi, 0)
    Jxx, Jxy, Jyy = blur(gx * gx, 5 * S), blur(gx * gy, 5 * S), blur(gy * gy, 5 * S)
    ANG = 0.5 * np.arctan2(2 * Jxy, Jxx - Jyy) + np.pi / 2
    COH = np.sqrt((Jxx - Jyy) ** 2 + 4 * Jxy ** 2) / (Jxx + Jyy + 1e-6)
    del gx, gy, lumi, Jxx, Jxy, Jyy
    DEF = np.array([stroke_angle.get(int(k), 0.0) for k in region_ids])
    regmasks = [(REG == k).astype(np.float32) for k in region_ids]

    def region_blur(sig):
        """Blur the colour reference without letting colours leak across surfaces."""
        if sig <= 0:
            return TGT
        out = np.zeros_like(TGT)
        for mk in regmasks:
            if not mk.any():
                continue
            den = blur(mk, sig) + 1e-5
            for ch in range(3):
                out[..., ch] += mk * blur(TGT[..., ch] * mk, sig) / den
        return out

    ka = (S / 2.4) ** 2; ks = S / 2.4
    total = 0
    for pi, (n, wr, Lr, op, bri, sig, by_err) in enumerate(PASSES):
        limits.stage(f"paint (pass {pi + 1} of {len(PASSES)})")
        fine = pi >= len(PASSES) - 2
        n = max(int(n * ka), 1)
        if not fine:
            wr = (max(wr[0] * ks, 1.2), max(wr[1] * ks, 1.8)); Lr = (max(Lr[0] * ks, 3), max(Lr[1] * ks, 6.5)); sig *= ks
        ref = region_blur(sig)
        nb = 4 if by_err else 1
        for _ in range(nb):
            m = max(n // nb, 1)
            if by_err:                                   # look at the canvas, paint where it is wrong
                err = blur(np.abs(canvas - ref).sum(2), max(wr[0] * 0.4, 0.6)) + 0.004
                pe = err.ravel().astype(np.float64)
                pidx = rng.choice(H * W, m, p=pe / pe.sum())
                ys = (pidx // W).astype(np.float32) + rng.uniform(-0.5, 0.5, m)
                xs = (pidx % W).astype(np.float32) + rng.uniform(-0.5, 0.5, m)
            else:
                xs = rng.uniform(0, W, m); ys = rng.uniform(0, H, m)
            xi = np.clip(xs.astype(int), 0, W - 1); yi = np.clip(ys.astype(int), 0, H - 1)
            reg = dense[yi, xi]
            ang = np.where(COH[yi, xi] > 0.18, ANG[yi, xi], DEF[reg]) + rng.normal(0, 0.12, m)
            sea = REG[yi, xi] == sea_region
            ang = np.where(sea, rng.normal(0, 0.03, m), ang)          # water lies flat
            Ls = rng.uniform(*Lr, m) * np.where(sea, 1.5, 1.0)
            ws = rng.uniform(*wr, m)
            if pi < 3:                                               # distant water gets smaller marks
                persp = np.where(sea, np.clip(0.35 + (ys - horizon_y) / (260 * S), 0.35, 1.0), 1.0)
                Ls = Ls * persp; ws = ws * np.maximum(persp, 0.5)
            cols = ref[yi, xi] + rng.normal(0, 0.012 if pi < 4 else 0.006, (m, 1))
            P = np.column_stack([xs, ys, ang, Ls, ws, cols, np.full(m, op), rng.normal(0, 1, m) * Ls * 0.05,
                                 np.full(m, bri)]).astype(np.float32)
            report['attempted_strokes'] += m
            native.strokes(canvas, height, P, reg, masks, BR, int(rng.integers(1, 2 ** 31)))
            total += m
            report['completed_strokes'] = total
            limits.active().strokes_done = total
        log(f"pass {pi + 1}/{len(PASSES)}: {total} strokes so far, mean error {np.abs(canvas - TGT).mean():.4f}")
        if checkpoint:
            checkpoint(canvas, pi + 1, total)
    limits.stage("paint (relief and grain)")
    h = gaussian_filter(height, 0.7)
    canvas *= np.clip(1 - 0.15 * (np.gradient(h, axis=1) + np.gradient(h, axis=0)), 0.93, 1.07)[..., None]
    if grain:                                            # sensor grain: the one step that is not brushwork
        gr = gaussian_filter(rng.normal(0, 1, (H, W)).astype(np.float32), 0.38 * S); gr /= gr.std()
        lum = canvas @ np.array((0.3, 0.6, 0.1), np.float32)
        canvas += (gr * grain * (1 - 0.55 * lum))[..., None] + rng.normal(0, grain * 0.2, canvas.shape).astype(np.float32)
    report['terminal_reason'] = 'preset_complete'
    return np.clip(canvas, 0, 1), total


def _stream(seed, digest, region, tier, batch, name):
    """Stable independent streams; never depend on Python's randomized hash()."""
    key = json.dumps([str(seed), digest, int(region), int(tier), int(batch), name],
                     separators=(',', ':')).encode()
    words = np.frombuffer(hashlib.sha256(key).digest(), dtype='<u4')
    return np.random.default_rng(np.random.SeedSequence(words.tolist()))


def _adaptive(TGT, REG, stroke_angle, S, horizon_y, rng, log, sea_region, grain,
              checkpoint, finish, config, max_strokes, max_passes, seed, decision_log):
    from .painting_policy import resolve_config, stroke_ceiling, brush_tiers, config_digest
    from .painting_metrics import PaintingMetrics

    TGT, REG = np.asarray(TGT), np.asarray(REG)
    if (TGT.ndim != 3 or TGT.shape[2] != 3 or REG.shape != TGT.shape[:2]
            or REG.dtype.kind not in 'iu' or not np.isfinite(TGT).all()
            or TGT.min() < 0 or TGT.max() > 1 or not math.isfinite(S) or S <= 0):
        raise ValueError('adaptive target must be finite RGB in 0..1 with matching integer regions and positive scale')
    cfg = resolve_config(config, finish)
    limits.stage('paint (adaptive planning)')
    H, W = REG.shape
    limits.check_pixels('paint (adaptive canvas)', W, H)
    admitted = stroke_ceiling(W, H, max_strokes)
    limits.check_strokes('paint (adaptive allocation)', admitted)
    max_batches = cfg.max_batches
    if max_passes is not None:
        if isinstance(max_passes, bool) or not isinstance(max_passes, (int, np.integer)) or not 0 < max_passes <= max_batches:
            raise ValueError(f'max_passes must lower the batch guard {max_batches}')
        max_batches = int(max_passes)
    if seed is None:
        seed = int.from_bytes(hashlib.sha256(json.dumps(rng.bit_generator.state, sort_keys=True,
                                                       default=str).encode()).digest()[:16], 'little')
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError('adaptive seed must be a nonnegative integer')
    digest = config_digest(cfg, finish)
    tiers = brush_tiers(W, H, S, cfg, finish)
    ids, inverse = np.unique(REG, return_inverse=True)
    dense = inverse.reshape(REG.shape).astype(np.int32)
    regmasks = [(REG == key).astype(np.float32) for key in ids]
    masks = np.ascontiguousarray(np.stack([gaussian_filter(mk, .8) for mk in regmasks]))
    canvas = np.empty((H, W, 3), np.float32); canvas[:] = (.30, .22, .22)
    height = np.zeros((H, W), np.float32)
    brng = _stream(seed, digest, 0, 0, 0, 'bristle')
    BR = gaussian_filter1d(brng.random(8192).astype(np.float32), 1.1, mode='wrap')
    BR = np.ascontiguousarray((BR-BR.min())/(BR.max()-BR.min()), np.float32)
    lumi = gaussian_filter(TGT @ np.array((.3, .6, .1), np.float32), 1.5*S)
    gx, gy = sobel(lumi, 1), sobel(lumi, 0)
    xx, xy, yy = blur(gx*gx, 5*S), blur(gx*gy, 5*S), blur(gy*gy, 5*S)
    angle = .5*np.arctan2(2*xy, xx-yy)+np.pi/2
    coherence = np.sqrt((xx-yy)**2+4*xy**2)/(xx+yy+1e-6)
    del lumi, gx, gy, xx, xy, yy
    metrics = PaintingMetrics(TGT, REG, cfg.evaluation_pixels)
    state, error = metrics.evaluate(canvas, height, TGT)
    b = limits.active()
    report = dict(schema_version=1, policy='adaptive-v1', finish=finish, config=cfg.as_dict(),
                  config_sha256=digest, seed=int(seed), stream_derivation='sha256-json-seedsequence-v1',
                  admitted_strokes=admitted, attempted_strokes=0, completed_strokes=0,
                  batches_complete=0, terminal_reason=None, region_ids=[int(k) for k in ids],
                  mask_planes=len(ids), initial_metrics=state, decisions=[])
    b.painting = report
    attempted = completed = 0
    counts = {int(k): 0 for k in ids}
    tier_index = 0
    tier_work = 0
    plateau = 0
    ref = None
    previous_tier = None
    batch_size = min(cfg.batch_size, max(16, math.ceil(H*W/256)))
    gain_rates = {int(k): max(cfg.min_gain, state['regions'][int(k)])/batch_size for k in ids}

    def covered():
        return all(counts[k] > 0 and state['region_coverage'][k] >= cfg.min_coverage for k in counts)

    def quality_ready():
        return (covered() and state['objective'] <= cfg.target_error
                and all(value <= cfg.target_error for value in state['regions'].values())
                and state['low'] <= cfg.low_error+cfg.texture_tolerance*metrics.texture
                and all(state['region_low'][k] <= cfg.low_error+cfg.texture_tolerance*metrics.region_texture[k]
                        for k in counts))

    def finish_report(reason):
        report.update(terminal_reason=reason, attempted_strokes=attempted,
                      completed_strokes=completed, final_metrics=state,
                      batches_complete=sum(d['accepted'] for d in report['decisions']))

    try:
        for batch in range(max_batches):
            if quality_ready():
                finish_report('quality_target'); break
            if attempted >= admitted:
                finish_report('stroke_budget')
                raise limits.LimitExceeded('paint (adaptive allocation)', 'strokes', admitted,
                                           used=attempted, detail='adaptive work ceiling reached before convergence')
            tier = tiers[tier_index]
            missing = [k for k in counts if counts[k] == 0]
            eligible = [k for k in counts if state['regions'][k] > cfg.target_error*.5
                        or state['region_coverage'][k] < cfg.min_coverage]
            eligible = eligible or list(counts)
            if missing:
                region = min(missing, key=lambda k: (-metrics.area[k], k))
            elif batch % 4 == 0:
                region = min(eligible, key=lambda k: (counts[k], k))
            else:
                region = max(eligible, key=lambda k: (state['regions'][k]*(.75*metrics.area[k]+.25/len(ids))
                                                     * max(gain_rates[k], cfg.min_gain/batch_size), -k))
            prng = _stream(seed, digest, region, tier.id, batch, 'placement')
            m = min(batch_size, admitted-attempted)
            if previous_tier != tier.id:
                ref = TGT
                if tier.pickup_blur > 0:
                    ref = np.zeros_like(TGT)
                    for mk in regmasks:
                        den = blur(mk, tier.pickup_blur)+1e-5
                        for ch in range(3):
                            ref[..., ch] += mk*blur(TGT[..., ch]*mk, tier.pickup_blur)/den
                previous_tier = tier.id
            loc = metrics.locations[region]
            samples = prng.choice(loc, min(8192, len(loc)))
            sy, sx = samples//W, samples%W
            residual = np.abs(canvas[sy, sx]-TGT[sy, sx]).mean(axis=1).astype(np.float64)+1e-6
            uniform_count = max(1, math.ceil(m*cfg.uniform_fraction))
            selected = np.concatenate((prng.choice(samples, m-uniform_count, p=residual/residual.sum()),
                                       prng.choice(loc, uniform_count)))
            prng.shuffle(selected)
            yi, xi = selected//W, selected%W
            xs = xi+prng.uniform(-.5, .5, m); ys = yi+prng.uniform(-.5, .5, m)
            direction = np.where(coherence[yi, xi] > .18, angle[yi, xi], stroke_angle.get(region, 0.0))
            direction = direction+prng.normal(0, .12, m)
            sea = region == sea_region
            if sea:
                direction = prng.normal(0, .03, m)
            length = prng.uniform(*tier.length, m)*(1.5 if sea else 1)
            width = prng.uniform(*tier.width, m)
            if sea and tier.perspective:
                perspective = np.clip(.35+(ys-horizon_y)/(260*S), .35, 1)
                length *= perspective; width *= np.maximum(perspective, .5)
            colours = np.clip(ref[yi, xi]+prng.normal(0, tier.colour_jitter, (m, 1)), 0, 1)
            strokes = np.column_stack((xs, ys, direction, length, width, colours,
                                      np.full(m, tier.opacity), prng.normal(0, 1, m)*length*.05,
                                      np.full(m, tier.bristle))).astype(np.float32)
            old_canvas, old_height = canvas.copy(), height.copy()
            before = state
            attempted += m
            report['attempted_strokes'] = attempted
            b.strokes_done = attempted
            limits.stage(f'paint (adaptive batch {batch+1}, tier {tier.id}, region {region})')
            native.strokes(canvas, height, strokes, dense[yi, xi], masks, BR,
                           int(prng.integers(1, 2**31)))
            candidate, candidate_error = metrics.evaluate(canvas, height, TGT)
            accepted = candidate['objective'] <= before['objective']+cfg.rejection_tolerance
            if accepted:
                state, error = candidate, candidate_error
                completed += m; counts[region] += 1
            else:
                canvas[:], height[:] = old_canvas, old_height
            del old_canvas, old_height
            gain = before['objective']-state['objective']
            regional_gain = before['regions'][region]-state['regions'][region]
            gain_rates[region] = .5*gain_rates[region]+.5*max(0, regional_gain)/m
            tier_work += m
            decision = dict(batch=batch+1, tier=tier.id, region=region, attempted=m,
                            accepted=bool(accepted), attempted_total=attempted,
                            completed_total=completed, gain=float(gain),
                            regional_gain_per_stroke=float(regional_gain/m),
                            metrics={key:state[key] for key in ('objective','colour','low','edge','coverage')},
                            selected_region=dict(error=state['regions'][region],
                                                 low=state['region_low'][region],
                                                 coverage=state['region_coverage'][region]))
            report['decisions'].append(decision)
            report.update(completed_strokes=completed, final_metrics=state)
            if decision_log:
                decision_log(decision)
            if accepted and checkpoint:
                checkpoint(canvas, batch+1, completed)
            threshold = max(cfg.min_gain, cfg.relative_gain*before['objective'])
            plateau = plateau+1 if gain <= threshold else 0
            log(f'adaptive batch {batch+1}: {attempted} attempted, {completed} accepted, error {state["objective"]:.5f}')
            if quality_ready():
                finish_report('quality_target'); break
            coverage_work = H*W/max(1, sum(tier.width)*sum(tier.length)*.10)
            if plateau >= cfg.patience or tier_work >= max(batch_size, 2*coverage_work):
                if tier_index+1 < len(tiers):
                    tier_index += 1; tier_work = 0; plateau = 0
                elif plateau >= cfg.patience and covered():
                    finish_report('diminishing_returns'); break
        else:
            finish_report('pass_guard')
            raise limits.LimitExceeded('paint (adaptive batch guard)', 'strokes', admitted,
                                       used=attempted, detail=f'adaptive batch guard {max_batches} reached')
    except BaseException as exc:
        if not report['terminal_reason']:
            stop = b.first_stop
            reason = ('memory' if isinstance(exc, MemoryError) else stop.limit if stop is not None
                      else 'cancelled' if b.interrupted else 'output_error' if isinstance(exc, OSError)
                      else 'input_error')
            finish_report(reason)
        raise
    if report['terminal_reason'] not in ('quality_target', 'diminishing_returns'):
        raise RuntimeError('adaptive painter lacks a converged outcome')
    limits.stage('paint (adaptive relief and grain)')
    h = gaussian_filter(height, .7)
    canvas *= np.clip(1-.15*(np.gradient(h, axis=1)+np.gradient(h, axis=0)), .93, 1.07)[..., None]
    if grain:
        grng = _stream(seed, digest, 0, 0, 0, 'finish-grain')
        gr = gaussian_filter(grng.normal(0, 1, (H, W)).astype(np.float32), .38*S)
        gr /= max(float(gr.std()), 1e-12)
        lum = canvas @ np.array((.3, .6, .1), np.float32)
        canvas += (gr*grain*(1-.55*lum))[..., None]+grng.normal(0, grain*.2, canvas.shape).astype(np.float32)
    return np.clip(canvas, 0, 1), attempted
