"""Named looks. Kept free of heavy imports so the command line can list them at once."""

# sun_offset is degrees from the camera's view direction (negative = left of frame).
LOOKS = {
    "sunset": dict(sun_offset=-20.0, sun_elev=0.9),        # sun in frame on the horizon, glitter path, backlit subject
    "golden_hour": dict(sun_offset=-104.0, sun_elev=7.0),  # sun out of frame to the side, raking light, visible shadows
}
