"""Bounded convex-polygon measurements. No raster sampling, geometry repair or external engine.

Clipping/areas use exact rational representations of input lon/lat doubles. Ratios
are exact in that planar coordinate model; metre fields explicitly use an approximate
local affine projection. Concave, unsupported and over-budget polygons get no score.
"""
from fractions import Fraction as Q
import math

MAX_VERTICES = 32


def _cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _area2(p):
    return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(p, p[1:]+p[:1]))


def _convex(p):
    if _area2(p) < 0:
        p = tuple(reversed(p))
    return p if all(_cross(a,b,c) >= 0 for a,b,c in zip(p, p[1:]+p[:1], p[2:]+p[:2])) else None


def _intersection(a, b):
    out = a
    for p, q in zip(b, b[1:]+b[:1]):
        previous, clipped = out, []
        if not previous:
            break
        for s, t in zip(previous, previous[1:]+previous[:1]):
            ds, dt = _cross(p,q,s), _cross(p,q,t)
            if ds >= 0:
                clipped.append(s)
            if (ds < 0 < dt) or (dt < 0 < ds):
                f = ds/(ds-dt)
                clipped.append((s[0]+f*(t[0]-s[0]), s[1]+f*(t[1]-s[1])))
        out = tuple(clipped)
    return out


def _centroid(p):
    terms = [(a,b,a[0]*b[1]-b[0]*a[1]) for a,b in zip(p,p[1:]+p[:1])]
    denominator = 3*_area2(p)
    return tuple(sum((a[i]+b[i])*c for a,b,c in terms)/denominator for i in (0,1))


def _vertex_distance(a, b):
    def distance(p, s, t):
        dx,dy = t[0]-s[0], t[1]-s[1]
        f = max(0.0,min(1.0,((p[0]-s[0])*dx+(p[1]-s[1])*dy)/(dx*dx+dy*dy))) if dx or dy else 0.0
        return math.hypot(p[0]-s[0]-f*dx,p[1]-s[1]-f*dy)
    return max(min(distance(p,s,t) for s,t in zip(b,b[1:]+b[:1])) for p in a)


def measurements(a, b):
    if a is None or b is None:
        return {"available": False, "reason": "unsupported_geometry"}
    if max(len(a),len(b)) > MAX_VERTICES:
        return {"available": False, "reason": "measurement_vertex_limit"}
    aa,bb = (_convex(tuple(tuple(Q(v) for v in p) for p in ring)) for ring in (a,b))
    if aa is None or bb is None:
        return {"available": False, "reason": "convex_measurements_only"}
    area_a,area_b = _area2(aa)/2, _area2(bb)/2
    if not area_a or not area_b:
        return {"available": False, "reason": "degenerate_geometry"}
    cut = _intersection(aa,bb)
    intersection = abs(_area2(cut))/2 if len(cut) >= 3 else Q(0)
    iou = intersection/(area_a+area_b-intersection)
    ratio = min(area_a,area_b)/max(area_a,area_b)
    lat = (min(p[1] for p in a+b)+max(p[1] for p in a+b))/2
    sx,sy = 111320*math.cos(math.radians(lat)),110540.0
    origin = a[0]
    project = lambda ring: tuple(((p[0]-origin[0])*sx,(p[1]-origin[1])*sy) for p in ring)
    xy_a,xy_b = project(a),project(b)
    ca,cb = _centroid(aa),_centroid(bb)
    return dict(available=True, model="exact_rational_convex_lonlat_plane",
                metre_model="approximate_local_affine_projection", iou=float(iou), iou_fraction=str(iou),
                relative_area=float(ratio), relative_area_fraction=str(ratio),
                intersection_degrees2_fraction=str(intersection),
                intersection_m2_estimate=float(intersection)*sx*sy,
                a_contains_b=intersection==area_b, b_contains_a=intersection==area_a,
                centroid_distance_m_estimate=math.hypot(float(ca[0]-cb[0])*sx,float(ca[1]-cb[1])*sy),
                symmetric_vertex_boundary_m_estimate=max(_vertex_distance(xy_a,xy_b),_vertex_distance(xy_b,xy_a)))


def equivalent(a, b, tolerance):
    # Cheap rejection before rational clipping. The existing graph still enforces
    # unique one-to-one candidates and known-OSM-identity protections.
    if a is None or b is None or max(len(a),len(b)) > MAX_VERTICES:
        return False
    if max(abs(min(p[k] for p in a)-min(p[k] for p in b)) for k in (0,1)) > 0.0001:
        return False
    m = measurements(a,b)
    return (m["available"] and Q(m["iou_fraction"]) >= Q(49,50) and
            Q(m["relative_area_fraction"]) >= Q(49,50) and
            m["centroid_distance_m_estimate"] <= tolerance-1e-6 and
            m["symmetric_vertex_boundary_m_estimate"] <= tolerance-1e-6)
