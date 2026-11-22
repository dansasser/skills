"""What a scene file hands to the engine."""
from dataclasses import dataclass, field
from numbers import Integral
from typing import Callable, Optional


def validate_regions(scene):
    """Validate region IDs before the renderer allocates its signed 8-bit buffers."""
    def check(value, field):
        if isinstance(value, bool) or not isinstance(value, Integral) or not 2 <= value <= 127:
            raise ValueError(f"{field} must be an integer from 2 through 127 "
                             f"(0 and 1 are reserved for sky and sea); got {value!r}")

    check(scene.default_region, "Scene.default_region")
    for material, value in scene.regions.items():
        check(value, f"Scene.regions[{material!r}]")


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
