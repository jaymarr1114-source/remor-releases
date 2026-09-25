"""
swarm_engine/forge/primitives.py

Composable, parametric mesh primitives. These are the "vocabulary" the
CapabilitySynthesizer draws on when it writes a new generator function.
Nothing here is prompt-specific — that's the point. The synthesizer decides
*how* to combine these based on the parsed spec; these functions just know
how to build one well-formed piece of geometry from parameters.

Every function returns a trimesh.Trimesh translated to the given origin.
"""
import trimesh
import numpy as np


def torso(width=0.5, height=0.8, depth=0.3, origin=(0, 0, 0)):
    m = trimesh.creation.box(extents=[width, height, depth])
    return m.apply_translation(origin)


def head(radius=0.25, origin=(0, 0, 0)):
    m = trimesh.creation.uv_sphere(radius=radius)
    return m.apply_translation(origin)


def limb(radius=0.07, length=0.7, origin=(0, 0, 0), rotation=None):
    m = trimesh.creation.cylinder(radius=radius, height=length)
    if rotation is not None:
        m.apply_transform(rotation)
    return m.apply_translation(origin)


def wheel(radius=0.15, thickness=0.08, origin=(0, 0, 0)):
    m = trimesh.creation.cylinder(radius=radius, height=thickness)
    rot = trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0])
    m.apply_transform(rot)
    return m.apply_translation(origin)


def wing(span=0.6, chord=0.25, thickness=0.03, origin=(0, 0, 0), sweep_deg=0.0):
    m = trimesh.creation.box(extents=[span, chord, thickness])
    if sweep_deg:
        rot = trimesh.transformations.rotation_matrix(np.radians(sweep_deg), [0, 0, 1])
        m.apply_transform(rot)
    return m.apply_translation(origin)


def shell(radius=0.4, origin=(0, 0, 0), squash=1.0):
    m = trimesh.creation.icosphere(radius=radius, subdivisions=2)
    m.apply_scale([1.0, squash, 1.0])
    return m.apply_translation(origin)


def radial_legs(count, radius, length, attach_radius, z=0.0, thickness=0.05):
    """Arranges `count` legs evenly around a circle of `attach_radius`."""
    parts = []
    for i in range(count):
        angle = 2 * np.pi * i / count
        x = attach_radius * np.cos(angle)
        y = attach_radius * np.sin(angle)
        rot = trimesh.transformations.rotation_matrix(np.pi / 2, [np.cos(angle + np.pi / 2), np.sin(angle + np.pi / 2), 0])
        leg = limb(radius=thickness, length=length, origin=(x, y, z), rotation=rot)
        parts.append(leg)
    return parts
