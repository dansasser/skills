"""What a scene file hands to the engine."""
from dataclasses import dataclass, field
from numbers import Integral
from typing import Callable, Optional
import numpy as np
from .validation import finite, integer, indices


def validate_regions(scene):
    """Validate region IDs before the renderer allocates its signed 8-bit buffers."""
    def check(value, field):
        if isinstance(value, bool) or not isinstance(value, Integral) or not 2 <= value <= 127:
            raise ValueError(f"{field} must be an integer from 2 through 127 "
                             f"(0 and 1 are reserved for sky and sea); got {value!r}")

    check(scene.default_region, "Scene.default_region")
    if not isinstance(scene.regions, dict):
        raise ValueError('Scene.regions must map material IDs to paint region IDs')
    for material, value in scene.regions.items():
        integer(material, 'Scene.regions material ID')
        check(value, f"Scene.regions[{material!r}]")


def validate_shading(result, n):
    """Return finite float32 callback arrays; explicitly allow scalar broadcasting."""
    if not isinstance(result, (tuple, list)) or len(result) != 3:
        raise ValueError('Scene.shade must return (albedo, gloss, emission)')
    out = []
    for field, value, shape in zip(('albedo','gloss','emission'),result,((n,3),(n,),(n,3))):
        a = finite(value, 'Scene.shade.'+field, nonnegative=True)
        if a.shape not in ((),shape,(3,) if len(shape)==2 else shape):
            raise ValueError(f'Scene.shade.{field} must be scalar or shape {shape}; got {a.shape}')
        a = np.broadcast_to(a,shape).astype(np.float32,copy=False)
        if not np.isfinite(a).all():
            raise ValueError(f'Scene.shade.{field} overflows float32')
        out.append(a)
    return tuple(out)


def validate_scene(scene):
    if not isinstance(scene, Scene):
        raise ValueError('build() must return a meshpaint.scene.Scene')
    validate_regions(scene)
    if not hasattr(scene.meshes,'done') or not callable(scene.meshes.done):
        raise ValueError('Scene.meshes must expose done()')
    if not callable(scene.shade) or (scene.bump is not None and not callable(scene.bump)):
        raise ValueError('Scene.shade and optional Scene.bump must be callable')
    from .geometry import Camera
    if not isinstance(scene.camera,Camera):
        raise ValueError('Scene.camera must be a Camera')
    scene.camera.validate()
    if not isinstance(scene.size,(tuple,list)) or len(scene.size)!=2:
        raise ValueError('Scene.size must be (width, height)')
    for x in scene.size:
        integer(x,'Scene.size',1)
    if scene.sea and scene.camera.eye[2]<=0:
        raise ValueError('Scene.camera.eye must be above the sea plane when Scene.sea is enabled')
    if isinstance(scene.look,str):
        from .looks import LOOKS
        if scene.look not in LOOKS:
            raise ValueError(f'Scene.look is unknown: {scene.look!r}')
    elif isinstance(scene.look,dict) and {'sun_offset','sun_elev'}<=scene.look.keys():
        finite(scene.look['sun_offset'],'Scene.look.sun_offset',())
        finite(scene.look['sun_elev'],'Scene.look.sun_elev',())
    else:
        raise ValueError('Scene.look must be a known name or finite sun_offset/sun_elev mapping')
    for field in ('sea_state','foam','clouds'):
        finite(getattr(scene,field),'Scene.'+field,(),nonnegative=True)
    if scene.exposure is not None:
        finite(scene.exposure,'Scene.exposure',(),positive=True)
    if not isinstance(scene.stroke_angle,dict):
        raise ValueError('Scene.stroke_angle must map region IDs to finite angles')
    for region,angle in scene.stroke_angle.items():
        integer(region,'Scene.stroke_angle region',0,127)
        finite(angle,f'Scene.stroke_angle[{region}]',())
    for material in scene.no_flip:
        integer(material,'Scene.no_flip material')
    for light in scene.lights:
        if not isinstance(light,Light):
            raise ValueError('Scene.lights entries must be Light values')
        finite(light.pos,'Light.pos',(3,));finite(light.colour,'Light.colour',(3,),nonnegative=True)
        finite(light.power,'Light.power',(),nonnegative=True)
        for material in (*light.skip_mats,*light.transparent_mats):
            integer(material,'Light material')
    for glow in scene.glows:
        if not isinstance(glow,Glow):
            raise ValueError('Scene.glows entries must be Glow values')
        finite(glow.pos,'Glow.pos',(3,));finite(glow.colour,'Glow.colour',(3,),nonnegative=True)
        for amp,radius in glow.halo:
            finite(amp,'Glow.halo strength',(),nonnegative=True);finite(radius,'Glow.halo radius',(),positive=True)
        for angle,strength in glow.beams:
            finite(angle,'Glow.beams angle',());finite(strength,'Glow.beams strength',(),nonnegative=True)
        if glow.starburst is not None:
            finite(glow.starburst,'Glow.starburst',(2,),nonnegative=True)
    return scene


@dataclass
class Light:
    """A point light. Its shadow map looks straight down, so it suits lamps above the ground."""
    pos: tuple
    colour: tuple = (1.0, 0.72, 0.36)
    power: float = 55.0
    skip_mats: tuple = ()          # materials this light never brightens (e.g. the lamp's own housing)
    transparent_mats: tuple = ()   # materials that do not block it (e.g. lantern glass)


@dataclass
class Glow:
    """Screen-space glare around a bright source. halo: [(strength, radius px at 1600 wide)].
    beams: [(angle radians on screen, strength)]. starburst: (strength, length px) or None."""
    pos: tuple
    colour: tuple = (1.0, 0.78, 0.42)
    halo: tuple = ((0.9, 9), (0.22, 50), (0.05, 230))
    beams: tuple = ()
    starburst: Optional[tuple] = (0.55, 16.0)


@dataclass
class Scene:
    meshes: object                          # mesh.MeshSet
    camera: object                          # geometry.Camera
    shade: Callable                         # shade(g) -> (albedo Nx3, gloss N, emission Nx3)
    bump: Optional[Callable] = None         # bump(g): may adjust g.N in place for fine relief
    look: str = "golden_hour"               # a name from sky.LOOKS, or a dict(sun_offset=, sun_elev=)
    sea: bool = True                        # water plane at z = 0
    sea_state: float = 1.0                  # wave height: 0 = mirror calm, 0.3 = calm bay, 1 = breezy open sea, 1.5 = rough
    foam: float = 1.0                       # shore foam: 0 = none, 1 = surf on an exposed coast
    reflections: bool = True                # the sea mirrors land and structures as well as sky
    clouds: float = 1.0                     # cloud cover: 0 = clear sky, 1 = default broken cloud, 1.5 = heavy
    lens_ghosts: bool = True                # faint ghost discs opposite an in-frame sun; turn off if they land on the subject
    regions: dict = field(default_factory=dict)       # material id -> paint region id (2 through 127)
    default_region: int = 2
    stroke_angle: dict = field(default_factory=dict)  # region id -> default stroke direction (radians)
    no_flip: tuple = ()                     # materials whose normals are never flipped toward the camera (terrain)
    lights: list = field(default_factory=list)
    glows: list = field(default_factory=list)
    exposure: Optional[float] = None
    size: tuple = (1600, 1000)              # delivered image size
