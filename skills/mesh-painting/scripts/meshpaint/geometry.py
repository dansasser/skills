"""Camera, and shadow maps built by rasterising the same meshes from each light."""
import numpy as np
from .util import norm
from . import native, limits
from .validation import finite, integer, mesh_arrays
from .resources import fit_map


class Camera:
    """eye in metres (Z up). bearing: degrees clockwise from +Y. pitch: degrees up.
    focal: pixels at a 1600-wide frame (1537 is about a 55 degree view). focus: metres to the sharp plane.
    aperture: depth-of-field strength; 1 is a gentle default, 0 keeps everything sharp."""

    def __init__(self, eye, bearing, pitch, focal=1537.0, focus=185.0, aperture=1.0):
        self.eye = np.asarray(eye, np.float32)
        self.bearing, self.pitch, self.focal, self.focus = float(bearing), float(pitch), float(focal), float(focus)
        self.aperture = float(aperture)

    def validate(self):
        finite(self.eye,'Camera.eye',(3,))
        for field in ('bearing','pitch'):
            finite(getattr(self,field),'Camera.'+field,())
        for field in ('focal','focus'):
            finite(getattr(self,field),'Camera.'+field,(),positive=True)
        finite(self.aperture,'Camera.aperture',(),nonnegative=True)
        return self

    def setup(self, W, H, S):
        self.validate()
        integer(W,'Camera.width',1);integer(H,'Camera.height',1)
        finite(S,'Camera.scale',(),positive=True)
        self.W, self.H, self.S = W, H, S
        self.foc = self.focal * S
        b, p = np.radians(self.bearing), np.radians(self.pitch)
        self.fwd = np.array([np.sin(b) * np.cos(p), np.cos(b) * np.cos(p), np.sin(p)], np.float32)
        axis = [0,0,1] if abs(float(self.fwd[2])) < 0.999999 else [0,1,0]
        self.rgt = norm(np.cross(self.fwd, axis)).astype(np.float32)
        self.upv = np.cross(self.rgt, self.fwd).astype(np.float32)
        self.horizon_y = H / 2 + self.foc * np.tan(p)

    def project(self, P):
        c = np.asarray(P, np.float32) - self.eye
        zc = c @ self.fwd
        divisor = np.where(np.abs(zc)>1e-12,zc,np.where(zc<0,-1e-12,1e-12)).astype(zc.dtype,copy=False)
        return self.W / 2 + self.foc * (c @ self.rgt) / divisor, self.H / 2 - self.foc * (c @ self.upv) / divisor, zc

    def rays(self):
        PX, PY = np.meshgrid(np.arange(self.W, dtype=np.float32) + 0.5, np.arange(self.H, dtype=np.float32) + 0.5)
        D = norm(self.fwd + self.rgt * ((PX - self.W / 2) / self.foc)[..., None]
                 + self.upv * ((self.H / 2 - PY) / self.foc)[..., None]).astype(np.float32)
        return D, PX, PY


def clip_near(V,F,attributes,depths,near=1.0):
    """Clip crossings in world space and interpolate each vertex attribute.

    Returns vertices, faces, attribute mapping and original face IDs. The ordinary
    all-front path returns the original arrays unchanged. Each crossing adds at
    most two vertices and one triangle, checked before generated buffers exist.
    """
    V,F = mesh_arrays(V,F,'clip_near')
    depths = finite(depths,'clip_near.depths',(len(V),))
    finite(near,'clip_near.near',(),positive=True)
    attrs = {k:finite(a,'clip_near.'+k) for k,a in attributes.items()}
    for k,a in attrs.items():
        if a.ndim<1 or len(a)!=len(V):
            raise ValueError(f'clip_near.{k} must have one value per vertex')
    inside = depths[F] >= near
    front = inside.all(1); crossing = inside.any(1)&~front
    source = np.arange(len(F),dtype=np.int32)
    if front.all():
        return V,F,attrs,source
    n_cross = int(crossing.sum())
    max_v = len(V)+2*n_cross
    max_f = int(front.sum())+2*n_cross
    limits.check_projection('render (near-plane clipping)',max_v,max_f)
    if not n_cross:
        return V,F[front],attrs,source[front]
    extra=[]; extra_attrs={k:[] for k in attrs}; triangles=[]; origins=[]
    for original in np.flatnonzero(crossing):
        face=F[original]; polygon=[]
        for i in range(3):
            a,b=int(face[i-1]),int(face[i])
            ina,inb=bool(depths[a]>=near),bool(depths[b]>=near)
            if ina != inb:
                t=float((near-depths[a])/(depths[b]-depths[a]))
                index=len(V)+len(extra)
                extra.append((V[a]+t*(V[b]-V[a])).astype(V.dtype))
                for key,values in attrs.items():
                    extra_attrs[key].append((values[a]+t*(values[b]-values[a])).astype(values.dtype))
                polygon.append(index)
            if inb:
                polygon.append(b)
        for i in range(1,len(polygon)-1):
            triangles.append((polygon[0],polygon[i],polygon[i+1]));origins.append(original)
    vertices=np.concatenate([V,np.asarray(extra,dtype=V.dtype)])
    faces=np.concatenate([F[front],np.asarray(triangles,dtype=F.dtype).reshape(-1,3)])
    out_attrs={k:np.concatenate([a,np.asarray(extra_attrs[k],dtype=a.dtype)]) for k,a in attrs.items()}
    return vertices,faces,out_attrs,np.concatenate([source[front],np.asarray(origins,np.int32)])


class SunShadow:
    """Orthographic depth map from the sun's direction."""

    def __init__(self, V, F, sun, res=4096, plan=None):
        self.sun = sun
        axis=[0,0,1] if abs(float(sun[2]))<.999999 else [0,1,0]
        self.lx = norm(np.cross(sun, axis)).astype(np.float32)
        self.ly = np.cross(self.lx, sun).astype(np.float32)
        u, v, s = V @ self.lx, V @ self.ly, V @ sun
        wanted_h=int((v.max()-v.min()+4)/((u.max()-u.min()+4)/res))+1
        chosen=fit_map('sun',res,wanted_h,max_pixels=plan.pixel_limit if plan else limits.active().level.pixels,
                       max_dimension=plan.max_dimension if plan else 8192)
        self.cell = max((u.max()-u.min()+4)/chosen.width,(v.max()-v.min()+4)/chosen.height)
        self.u0, self.v1 = u.min() - 2, v.max() + 2
        self.w,self.h=chosen.width,chosen.height
        self.plan=chosen
        limits.check_auxiliary('sun shadow map',self.w,self.h,chosen.pixel_limit)
        self.Q = native.raster((u - self.u0) / self.cell, (self.v1 - v) / self.cell, s, F,
                               np.ones(len(F), bool), self.w, self.h, init=-1e9, persp=False)[3]

    def lit(self, P):
        """Fraction of the sun each point can see (9 taps give a soft edge)."""
        u = (P @ self.lx - self.u0) / self.cell; v = (self.v1 - P @ self.ly) / self.cell; sd = P @ self.sun
        lit = np.zeros(len(P), np.float32)
        for du in (-1.3, 0, 1.3):
            for dv in (-1.3, 0, 1.3):
                ui = (u + du).astype(np.int64); vi = (v + dv).astype(np.int64)
                inside = (ui >= 0) & (ui < self.w) & (vi >= 0) & (vi < self.h)
                occ = self.Q[np.clip(vi, 0, self.h - 1), np.clip(ui, 0, self.w - 1)]
                lit += np.where(inside & (occ > sd + 0.35), 0.0, 1.0)
        return lit / 9


class DownShadow:
    """Shadow map for a point light, looking straight down from it (covers about 72 degrees off vertical).
    Triangles of `transparent` materials (lamp glass) do not block the light."""

    def __init__(self, V, F, FM, pos, transparent=(), res=2048, focal=333.0, plan=None):
        chosen=fit_map('point',res,res,max_pixels=plan.pixel_limit if plan else limits.active().level.pixels,
                       max_dimension=plan.max_dimension if plan else 8192)
        focal=focal*chosen.width/res
        res=chosen.width
        self.plan=chosen
        limits.check_auxiliary('point shadow map',res,res,chosen.pixel_limit)
        self.pos, self.res, self.f = np.asarray(pos, np.float32), res, focal
        c = V - self.pos
        lz = -c[:, 2]
        eligible=~np.isin(FM,list(transparent))
        projected,faces,_,_=clip_near(V,F[eligible],{},lz,near=.2)
        c=projected-self.pos;lz=-c[:,2]
        with np.errstate(all="ignore"):
            safe=np.where(np.abs(lz)>1e-12,lz,np.where(lz<0,-1e-12,1e-12)).astype(lz.dtype,copy=False)
            self.Q = native.raster(res / 2 + focal * c[:, 0] / safe, res / 2 - focal * c[:, 1] / safe,
                                   1 / np.maximum(lz, 1e-3), faces, np.ones(len(faces),bool), res, res, init=0.0)[3]

    def lit(self, P):
        c = P - self.pos
        lz = np.maximum(-c[:, 2], 1e-3)
        u = self.res / 2 + self.f * c[:, 0] / lz; v = self.res / 2 - self.f * c[:, 1] / lz
        lit = np.zeros(len(P), np.float32)
        for du in (-1.2, 0, 1.2):
            for dv in (-1.2, 0, 1.2):
                ui = (u + du).astype(np.int64); vi = (v + dv).astype(np.int64)
                inside = (ui >= 0) & (ui < self.res) & (vi >= 0) & (vi < self.res) & (-c[:, 2] > 0.2)
                occ = self.Q[np.clip(vi, 0, self.res - 1), np.clip(ui, 0, self.res - 1)]
                lit += np.where(inside & (occ * (lz * 0.97 - 0.2) > 1.0), 0.0, 1.0)
        return lit / 9
