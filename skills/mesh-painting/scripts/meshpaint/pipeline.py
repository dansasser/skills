"""Scene -> lit render (the painting target) -> brushstroke painting."""
import time
from types import SimpleNamespace

import numpy as np
from scipy.ndimage import gaussian_filter

from . import limits, native, lens, util
from .geometry import SunShadow, DownShadow, clip_near
from .resources import JobPlan, ResourcePlan
from .scene import validate_scene, validate_shading
from .validation import finite, indices, mesh_arrays
from .sky import Sky, LOOKS
from .util import norm, smooth, blur
from .water import Shore, shade_sea


def make_log():
    t0 = time.time()

    def log(msg):
        print(f"[{time.time() - t0:6.1f}s] {msg}", flush=True)
    return log


def _mirror_pass(sc, cam, sky, sun_sh, VV, FF, VN, FM, VA, sea_mask, W, H):
    """Render the scene upside down below the waterline, as the sea would mirror it.
    Returns (colour image, coverage mask) holding the reflected land and structures at each
    sea pixel, lit by sky and sun (no gloss, no crease shading)."""
    Vr = VV.copy(); Vr[:, 2] = np.maximum(Vr[:, 2], 0)          # the real points, clamped to the waterline
    Vm = Vr.copy(); Vm[:, 2] = -Vm[:, 2]                         # ...and their mirror images
    _,_,zc = cam.project(Vm)
    eligible=(VV[FF][:,:,2]>.05).any(1)
    projected,faces,attrs,source=clip_near(Vm,FF[eligible],dict(world=Vr,N=VN,A=VA),zc)
    xs,ys,zc=cam.project(projected)
    with np.errstate(all="ignore"):
        TID, B1, B2, _ = native.raster(xs, ys, 1 / np.maximum(zc, 1e-3), faces, np.ones(len(faces),bool), W, H)
    g = (TID >= 0) & sea_mask
    img = np.zeros((H, W, 3), np.float32)
    if not g.any():
        return img, g.astype(np.float32)
    t_ = TID[g]; b1 = B1[g][:, None]; b2 = B2[g][:, None]; b0 = 1 - b1 - b2
    f = faces[t_]; world=attrs['world'];normal=attrs['N'];value=attrs['A']
    P = b0 * world[f[:, 0]] + b1 * world[f[:, 1]] + b2 * world[f[:, 2]]
    N = norm(b0 * normal[f[:, 0]] + b1 * normal[f[:, 1]] + b2 * normal[f[:, 2]])
    attr = b0[:, 0] * value[f[:, 0]] + b1[:, 0] * value[f[:, 1]] + b2[:, 0] * value[f[:, 2]]
    mat = FM[eligible][source[t_]]
    mirror_eye = cam.eye * np.array((1, 1, -1), np.float32)
    dist = np.linalg.norm(P - mirror_eye, axis=1)
    gb = SimpleNamespace(P=P, N=N, mat=mat, attr=attr, dist=dist, foc=cam.foc, x=P[:, 0], y=P[:, 1], z=P[:, 2])
    gb.fade = lambda size: np.clip(cam.foc * size / (dist * 6), 0, 1)
    alb, _, emit = validate_shading(sc.shade(gb),len(P))
    col = alb * (sky.ambient(N) + sky.sun_colour * (np.clip(N @ sky.sun, 0, 1) * sun_sh.lit(P + N * 0.15))[:, None]) + emit
    col = col + (sky.haze - col) * (1 - np.exp(-dist / 2600))[:, None]
    img[g] = col
    return img, g.astype(np.float32)


def render(sc, S, log, look=None):
    """Returns dict(target, regions, depth, horizon_y). target is (H, W, 3) in 0..1 at scale S."""
    validate_scene(sc)
    job=JobPlan.resolve(sc.size,S)
    limits.active().job_plan=job.as_dict()
    W,H=job.working_size
    limits.check_pixels("render (canvas)", W, H)      # before any canvas-sized buffer exists
    limits.stage("render (rasterising)")
    rng = util.rng()
    cam = sc.camera
    cam.setup(W,H,job.camera_scale)
    S = cam.S
    lk = look or sc.look
    if isinstance(lk, str):
        if lk not in LOOKS:
            raise SystemExit(f"meshpaint: unknown look '{lk}'; valid looks: {', '.join(LOOKS)}")
        lk = LOOKS[lk]
    sky = Sky(cam.bearing + lk["sun_offset"], lk["sun_elev"], cam.eye, sc.clouds)
    SUN = sky.sun
    VV, FF, VN, FM, VA = sc.meshes.done()
    VV,FF=mesh_arrays(VV,FF,'Scene.meshes.done')
    if not len(FF):
        raise ValueError('Scene.meshes.done: empty geometry is unsupported')
    VN=finite(VN,'Scene.meshes.done normals',(len(VV),3))
    VA=finite(VA,'Scene.meshes.done attributes',(len(VV),))
    FM=indices(FM,'Scene.meshes.done materials',(len(FF),))
    limits.confirm_mesh(len(VV), len(FF))
    axis=[0,0,1] if abs(float(SUN[2]))<.999999 else [0,1,0]
    lx=norm(np.cross(SUN,axis)).astype(np.float32);ly=np.cross(lx,SUN).astype(np.float32)
    u,v=VV@lx,VV@ly
    sun_size=(4096,int((v.max()-v.min()+4)/((u.max()-u.min()+4)/4096))+1)
    xy=VV[:,:2].max(0)-VV[:,:2].min(0)+60
    shore_cell=max(.25,float(xy.max())/4096)
    shore_size=(int(xy[0]/shore_cell)+1,int(xy[1]/shore_cell)+1)
    resource_plan=ResourcePlan.for_scene(sc,job,len(VV),len(FF),sun_size=sun_size,shore_size=shore_size,
                                         used_materials=np.unique(FM))
    limits.active().resource_plan=resource_plan.as_dict()
    log(f"meshes: {len(VV)} vertices, {len(FF)} triangles ({'compiled' if native._load() else 'python'} core)")

    _,_,zc=cam.project(VV)
    PV,PF,pa,source=clip_near(VV,FF,dict(N=VN,A=VA),zc)
    PN,PA,PM=pa['N'],pa['A'],FM[source]
    xs,ys,zc=cam.project(PV)
    with np.errstate(all="ignore"):
        TID,B1,B2,_=native.raster(xs,ys,1/np.maximum(zc,1e-3),PF,np.ones(len(PF),bool),W,H)
    sun_sh=SunShadow(VV,FF,SUN,plan=resource_plan.sun_map)
    light_sh=[DownShadow(VV,FF,FM,L.pos,L.transparent_mats,plan=p) for L,p in zip(sc.lights,resource_plan.point_maps)]
    limits.active().auxiliary_maps=[sun_sh.plan.as_dict()]+[sh.plan.as_dict() for sh in light_sh]
    log("rasterised camera view and shadow maps")
    limits.stage("render (shading geometry)")

    D, PX, PY = cam.rays()
    img = np.zeros((H, W, 3), np.float32)
    REG = np.zeros((H, W), np.int8)                    # 0 sky, 1 sea, 2+ scene surfaces
    DEPTH = np.full((H, W), 5000.0, np.float32)

    g = TID >= 0
    t_ = TID[g]; b1 = B1[g][:, None]; b2 = B2[g][:, None]; b0 = 1 - b1 - b2
    f = PF[t_]
    P = b0 * PV[f[:, 0]] + b1 * PV[f[:, 1]] + b2 * PV[f[:, 2]]
    keep = P[:, 2] >= 0 if sc.sea else np.ones(len(P), bool)   # anything under the waterline is sea
    gi = np.nonzero(g.ravel())[0]
    g = g.ravel().copy(); g[gi[~keep]] = False; g = g.reshape(H, W)
    P = P[keep]; f = f[keep]; b0, b1, b2 = b0[keep], b1[keep], b2[keep]
    N = norm(b0 * PN[f[:, 0]] + b1 * PN[f[:, 1]] + b2 * PN[f[:, 2]])
    attr = b0[:, 0] * PA[f[:, 0]] + b1[:, 0] * PA[f[:, 1]] + b2[:, 0] * PA[f[:, 2]]
    mat = PM[t_[keep]]
    dist = np.linalg.norm(P - cam.eye, axis=1)
    gb = SimpleNamespace(P=P, N=N, mat=mat, attr=attr, dist=dist, foc=cam.foc, x=P[:, 0], y=P[:, 1], z=P[:, 2])
    facing = np.sqrt(np.clip(np.abs((N * D[g]).sum(1)), 0.02, 1))          # 1 face-on, small at a grazing angle

    def fade(size):
        """0..1 weight for surface detail `size` metres across: 1 where it covers several pixels,
        falling to 0 where it would be smaller than a pixel, allowing for grazing view angles."""
        return np.clip(cam.foc * size * facing / (dist * 6), 0, 1)
    gb.fade = fade
    if sc.bump:
        sc.bump(gb)
        N=finite(gb.N,'Scene.bump normals',(len(P),3))
    Dg = D[g]
    N[((N * Dg).sum(1) > 0) & ~np.isin(mat, list(sc.no_flip))] *= -1
    gb.N = N
    DEPTH[g] = dist

    dl = np.minimum(DEPTH, 700)                         # crease shading from the depth buffer
    AO = np.clip(1 - 0.75 * np.clip((dl - blur(dl, 5 * S)) / (0.012 * dl), 0, 1), 0.25, 1)
    AO *= np.clip(1 - 0.5 * np.clip((dl - blur(dl, 18 * S)) / (0.03 * dl), 0, 1), 0.4, 1)
    ao = AO[g]

    alb,gloss,emit=validate_shading(sc.shade(gb),len(P))
    col = alb * (sky.ambient(N) * ao[:, None] + sky.sun_colour * (np.clip(N @ SUN, 0, 1) * sun_sh.lit(P + N * 0.15))[:, None])
    for L, sh in zip(sc.lights, light_sh):
        Lv = np.asarray(L.pos, np.float32) - P
        ld = np.linalg.norm(Lv, axis=1)
        lam = np.clip((N * Lv).sum(1) / ld, 0, 1) * L.power / (ld ** 2 + 4)
        lam[np.isin(mat, list(L.skip_mats))] = 0.0
        col += alb * np.asarray(L.colour, np.float32) * (lam * sh.lit(P + N * 0.1))[:, None]
    R = Dg - 2 * (Dg * N).sum(1, keepdims=True) * N
    R[:, 2] = np.abs(R[:, 2])
    fres = 0.03 + 0.97 * (1 - np.clip(-(Dg * N).sum(1), 0, 1)) ** 5
    col += sky.radiance(norm(R), clouds=False, disk=False) * (gloss * fres * ao)[:, None]
    col += emit
    col = col + (sky.haze - col) * (1 - np.exp(-dist / 2600))[:, None]
    img[g] = col
    materials=np.unique(FM)
    regmap=np.array([sc.regions.get(int(m),sc.default_region) for m in materials],np.int8)
    REG[g]=regmap[np.searchsorted(materials,mat)]
    log("geometry shaded")
    limits.stage("render (sky and sea)")

    s = (~g) & ((D[..., 2] > 0) | (not sc.sea))
    img[s] = sky.radiance(D[s])
    star = (rng.random(s.sum()) < 0.0007 / S ** 2 * smooth(0.12, 0.5, D[s][:, 2])) & (img[s].sum(1) < 0.45)
    sl = np.zeros(s.sum(), np.float32)
    sl[star] = rng.uniform(0.25, 1.3, star.sum()) * 6.3 * (0.5 * S) ** 2 * (1 - sky.day) ** 4
    SL = np.zeros((H, W), np.float32); SL[s] = sl
    img += gaussian_filter(SL, 0.5 * S)[..., None] * np.array((0.9, 0.95, 1.0), np.float32)

    if sc.sea:
        w = (~g) & (D[..., 2] <= 0)
        shore=Shore(VV,FF,plan=resource_plan.shore_map)
        limits.active().auxiliary_maps.append(shore.plan.as_dict())
        mirror = _mirror_pass(sc, cam, sky, sun_sh, VV, FF, VN, FM, VA, w, W, H) if sc.reflections else None
        seac, tw = shade_sea(D[w], cam, sky, shore, sun_sh, rng, (H, W), w, sc.sea_state, sc.foam, mirror, PX[w], PY[w])
        img[w] = seac
        REG[w] = 1
        DEPTH[w] = np.minimum(tw, 5000)
    log("sky and sea shaded" if sc.sea else "sky shaded")
    limits.stage("render (lens)")

    points = []
    for G in sc.glows:
        lx, ly, lz = cam.project(np.asarray(G.pos, np.float32)[None])
        if lz[0] <= 0:
            continue
        lx, ly = float(lx[0]), float(ly[0])
        colr = np.asarray(G.colour, np.float32)
        r = np.hypot(PX - lx, PY - ly) / S
        for amp, rad in G.halo:
            img += colr * (amp * np.exp(-(r / rad) ** 2))[..., None]
        for a0, strength in G.beams:
            al = ((PX - lx) * np.cos(a0) + (PY - ly) * np.sin(a0)) / S
            pe = np.abs(-(PX - lx) * np.sin(a0) + (PY - ly) * np.cos(a0)) / S
            wd = 5 + 0.045 * np.maximum(al, 0)
            img += colr * (strength * np.exp(-(pe / wd) ** 2) * np.exp(-np.maximum(al, 0) / 520) * (al > 0) * (12 / (wd + 7)))[..., None]
        if G.starburst:
            points.append((lx, ly, G.starburst[0], G.starburst[1]))
    img = lens.apply(img, DEPTH, cam, sky, PX, PY, points, ghosts=sc.lens_ghosts)
    exposure = sc.exposure if sc.exposure is not None else 0.98 - 0.18 * sky.day
    log("lens applied")
    return dict(target=lens.film(img, exposure), regions=REG, depth=DEPTH, horizon_y=cam.horizon_y, S=S)
