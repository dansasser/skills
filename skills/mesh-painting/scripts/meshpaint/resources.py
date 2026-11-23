"""Resolved dimensions and conservative live-buffer admission under existing caps.

Estimates are admission checks, not physical-memory reservations. RLIMIT_AS and the
supervisor remain authoritative. Auxiliary maps share one bounded pixel envelope.
"""
from dataclasses import dataclass, asdict
import math
from numbers import Integral
from . import limits


@dataclass(frozen=True)
class JobPlan:
    delivery_size: tuple
    working_size: tuple
    camera_scale: float
    pixel_area: int
    scale: float
    stage: str

    @classmethod
    def resolve(cls, size, scale, stage='render', budget=None):
        b = budget or limits.active()
        if not isinstance(size, (tuple, list)) or len(size) != 2:
            raise ValueError('Scene.size must contain integral positive width and height')
        if any(isinstance(x, bool) or not isinstance(x, Integral) or not 1 <= int(x) <= 2**31-1 for x in size):
            raise ValueError(f'Scene.size must contain integral positive PNG/native dimensions; got {size!r}')
        try:
            scale = float(scale)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('scale must be finite and positive') from error
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError('scale must be finite and positive')
        if stage == 'preview':
            scale = 1.0
        delivery = tuple(map(int, size))
        for label, dims in [('delivery', delivery)]:
            area = dims[0]*dims[1]
            if area > b.level.pixels:
                raise limits.LimitExceeded(f'preflight ({label})', 'pixels', b.level.pixels,
                                           requested=area, level=b.level.name,
                                           detail=f'{dims[0]} x {dims[1]}')
        # Bound the product before conversion; int(inf) and huge native dimensions never occur.
        products = [x*scale for x in delivery]
        if any(not math.isfinite(x) or x > 2**31-1 for x in products):
            raise ValueError('working dimensions exceed PNG/native representation')
        working = tuple(int(x) for x in products)
        if min(working) < 16:
            raise ValueError(f'working dimensions must each be at least 16; got {working}')
        area = working[0]*working[1]
        if area > b.level.pixels:
            raise limits.LimitExceeded('preflight (working canvas)', 'pixels', b.level.pixels,
                                       requested=area, level=b.level.name,
                                       detail=f'{working[0]} x {working[1]}')
        return cls(delivery, working, working[0]/1600.0, area, scale, stage)

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MapPlan:
    name: str
    requested_width: int
    requested_height: int
    width: int
    height: int
    pixel_limit: int
    max_dimension: int
    reduced: bool

    def as_dict(self):
        return asdict(self)


def fit_map(name, width, height, *, max_pixels, max_dimension=8192):
    width, height = max(1, int(width)), max(1, int(height))
    max_pixels = max(1, int(max_pixels)); max_dimension = max(1, int(max_dimension))
    factor = min(1.0, max_dimension/width, max_dimension/height,
                 math.sqrt(max_pixels/(width*height)))
    w, h = max(1, int(width*factor)), max(1, int(height*factor))
    # Clamping one axis to one can defeat uniform scaling for extremely long maps.
    if w*h > max_pixels:
        if w >= h:
            w = max(1, max_pixels//h)
        else:
            h = max(1, max_pixels//w)
    return MapPlan(name, width, height, w, h, max_pixels, max_dimension,
                   (w, h) != (width, height))


@dataclass
class ResourcePlan:
    job: JobPlan
    baseline_bytes: int
    ceiling_bytes: int
    output_reserve_bytes: int
    estimated_peak_bytes: int
    map_pixel_budget: int
    sun_map: MapPlan
    point_maps: list
    lifetime_groups: dict
    shore_map: object = None

    @classmethod
    def for_scene(cls, scene, job, vertex_count, triangle_count, budget=None,
                  sun_size=(4096,4096), shore_size=(4096,4096), used_materials=None):
        b = budget or limits.active()
        A = job.pixel_area; delivery = math.prod(job.delivery_size)
        baseline = int(limits.process_memory().get('address_space_kb', 0))*1024
        ceiling = int(b.level.memory_mb)*1024**2
        # Use the effective inherited ceiling even for direct library callers.
        try:
            import resource
            outside = resource.getrlimit(resource.RLIMIT_AS)[0]
            if outside != resource.RLIM_INFINITY:
                ceiling = min(ceiling, int(outside))
        except (ImportError, ValueError, OSError):
            pass
        output = max(16*1024**2, 20*A+8*delivery)
        regions={0}
        if scene.sea:
            regions.add(1)
        if used_materials is None:
            # Library callers without an assembled mesh retain a conservative fallback.
            regions.update({int(scene.default_region),*map(int,scene.regions.values())})
        else:
            regions.update(int(scene.regions.get(int(m),scene.default_region)) for m in used_materials)
        K=len(regions)
        mesh_bytes = int(vertex_count)*48 + int(triangle_count)*32
        # Account for lifetime groups rather than summing nonoverlapping render/paint peaks.
        # Mesh assembly/clipping uses bounded extra vertices/faces; shading includes RGB/vector
        # temporaries, rays, barycentrics, reflection/water/lens and target/output copies.
        groups = dict(mesh=mesh_bytes*3, render=mesh_bytes+(204 if scene.sea else 168)*A,
                      paint=mesh_bytes+(160+8*K)*A)
        available = ceiling-baseline-output-max(groups.values())
        if available < 16:
            raise limits.LimitExceeded('preflight (live buffers and output reserve)', 'memory',
                                       b.level.memory_mb, requested=math.ceil((ceiling-available)/1024**2),
                                       level=b.level.name,
                                       detail='conservative admission estimate, including imported address space')
        # During raster construction four 32-bit buffers exist; completed maps retain only depth.
        # Reserving 16 bytes per map pixel bounds aggregate as well as individual allocations.
        count = len(scene.lights)
        maps = count+1+int(scene.sea)
        # Each raster obeys the profile's pixel cap. Several simultaneously live
        # maps may share a larger, explicitly byte-bounded envelope; equating
        # aggregate map pixels with one camera canvas would unnecessarily change
        # ordinary legacy lighting that already fits the process ceiling.
        map_pixels=min(maps*int(b.level.pixels),max(1,int(available//32)))
        if maps > map_pixels:
            raise limits.LimitExceeded('preflight (shadow map count)', 'pixels', map_pixels,
                                       requested=maps, level=b.level.name)
        # Preserve preferred maps when they fit. Otherwise share the envelope deterministically.
        preferred_sun=min(math.prod(sun_size),int(b.level.pixels))
        preferred_shore=min(math.prod(shore_size),int(b.level.pixels)) if scene.sea else 0
        preferred_point=min(2048**2,int(b.level.pixels))
        preferred_total=preferred_sun+count*preferred_point+preferred_shore
        sun_pixels = max(1,min(preferred_sun,map_pixels*preferred_sun//preferred_total))
        shore_pixels = max(1,min(preferred_shore,map_pixels*preferred_shore//preferred_total)) if scene.sea else 0
        point_pixels=max(1,min(preferred_point,(map_pixels-sun_pixels-shore_pixels)//count)) if count else 0
        if sun_pixels+shore_pixels+count*point_pixels>map_pixels:
            sun_pixels=map_pixels-shore_pixels-count*point_pixels
        sun = fit_map('sun',*sun_size,max_pixels=sun_pixels)
        points = [fit_map(f'point-{i}',2048,2048,max_pixels=point_pixels) for i in range(count)]
        shore = fit_map('shore',*shore_size,max_pixels=shore_pixels) if scene.sea else None
        peak = baseline+output+max(groups.values())+32*(sun.pixel_limit+sum(x.pixel_limit for x in points)+shore_pixels)
        return cls(job,baseline,ceiling,output,peak,map_pixels,sun,points,groups,shore)

    def as_dict(self):
        d = asdict(self)
        d['estimate_kind'] = 'conservative address-space admission; not physical reservation'
        return d
