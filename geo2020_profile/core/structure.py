# -*- coding: utf-8 -*-
"""
Structural line construction inside a geological section (pure Python, no QGIS).

Coordinates are section coordinates: x = chainage (m), z = elevation (m), 1:1.

Construction order follows the law of cross-cutting relationships used in
professional section drawing (younger features truncate older ones):

    1. topography                       (given)
    2. borehole logs                    (given)
    3. soil / rock interface            (given; erosional base of the overburden)
    4. faults        -> stop at the soil/rock interface, topography, section frame
    5. rock contacts -> correlated between adjacent boreholes; unmatched or end
                        segments are extended with the apparent dip until they meet
                        a fault, the soil/rock interface, topography, the frame, or
                        a neighbouring borehole whose log does not show the contact
    6. apparent-dip form lines (bedding / cleavage ...) -> same terminators
"""
import math

EPS = 1e-6

# Geo2020 discontinuity type codes (kind = 06)
FAULT_TYPES = ("00", "01")                     # 斷層帶, 斷層
FABRIC_TYPES = ("03", "04", "08", "09", "10")  # 劈理, 片理, 葉理, 層理, 紋層  -> parallel to the rock attitude
SHORT_TYPES = ("02", "05", "06", "07", "11")   # 節理, 剪力面, 裂隙, 張裂隙, 岩脈 -> short ticks (core not oriented)


# ---------------------------------------------------------------- attitude
def azimuth(p0, p1):
    """Azimuth (deg from north, clockwise) of the map segment p0 -> p1 (x=E, y=N)."""
    return math.degrees(math.atan2(p1[0] - p0[0], p1[1] - p0[1])) % 360.0


def apparent_dip(dip_dir, dip, section_az):
    """Signed apparent dip (deg) seen in a section with azimuth ``section_az``.

    Positive = the plane goes DOWN towards increasing chainage (towards A'),
    negative = it goes down towards the section start (A).
    tan(a) = tan(dip) * cos(dip_dir - section_az)
    """
    if dip >= 89.999:
        c = math.cos(math.radians(dip_dir - section_az))
        if abs(c) < 1e-9:
            return 0.0
        return 90.0 if c > 0 else -90.0
    t = math.tan(math.radians(dip)) * math.cos(math.radians(dip_dir - section_az))
    return math.degrees(math.atan(t))


def direction(app_dip_deg):
    """Unit vector (dx, dz) pointing towards +x along a line with the given signed apparent dip."""
    a = math.radians(app_dip_deg)
    return (math.cos(a), -math.sin(a))


# ---------------------------------------------------------------- geometry
def _ray_seg(p, v, q1, q2):
    """Parameter t >= 0 where ray p + t v hits segment q1-q2, or None."""
    rx, rz = v
    sx, sz = q2[0] - q1[0], q2[1] - q1[1]
    den = rx * sz - rz * sx
    if abs(den) < 1e-12:
        return None
    qpx, qpz = q1[0] - p[0], q1[1] - p[1]
    t = (qpx * sz - qpz * sx) / den
    u = (qpx * rz - qpz * rx) / den
    if t > 1e-4 and -1e-9 <= u <= 1 + 1e-9:
        return t
    return None


def _ray_box(p, v, box):
    """Parameter where the ray leaves the box (xmin, zmin, xmax, zmax)."""
    xmin, zmin, xmax, zmax = box
    ts = []
    if abs(v[0]) > 1e-12:
        ts.append(((xmax if v[0] > 0 else xmin) - p[0]) / v[0])
    if abs(v[1]) > 1e-12:
        ts.append(((zmax if v[1] > 0 else zmin) - p[1]) / v[1])
    ts = [t for t in ts if t >= 0]
    return min(ts) if ts else 0.0


def cast(p, v, terminators, box):
    """Walk from p along v until the first terminator / frame.  Returns (end_point, tag)."""
    best, tag = _ray_box(p, v, box), "剖面邊界"
    for name, line in terminators:
        for i in range(len(line) - 1):
            t = _ray_seg(p, v, line[i], line[i + 1])
            if t is not None and t < best:
                best, tag = t, name
    return (p[0] + best * v[0], p[1] + best * v[1]), tag


def segment_hits(a, b, line):
    """True if segment a-b crosses polyline ``line`` (strictly inside a-b)."""
    v = (b[0] - a[0], b[1] - a[1])
    for i in range(len(line) - 1):
        t = _ray_seg(a, v, line[i], line[i + 1])
        if t is not None and t < 1 - 1e-4:
            return True
    return False


def interp(xs, zs, x):
    if not xs:
        return None
    if x <= xs[0]:
        return zs[0]
    if x >= xs[-1]:
        return zs[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        m = (lo + hi) // 2
        if xs[m] <= x:
            lo = m
        else:
            hi = m
    t = (x - xs[lo]) / ((xs[hi] - xs[lo]) or 1.0)
    return zs[lo] + t * (zs[hi] - zs[lo])


# ---------------------------------------------------------------- builders
def is_rock(code):
    c = (code or "").strip()
    return c == "010" or (c[:1].isdigit() and c[:1] in "345678")


def rock_contacts(layers, z_collar):
    """Contacts between DIFFERENT rock units inside one borehole: [(depth, z, upper, lower)]."""
    out = []
    prev = None
    for lay in sorted(layers, key=lambda x: x["top"]):
        code = (lay["code"] or "").strip()
        if prev is not None and is_rock(prev["code"]) and is_rock(code) and code != prev["code"].strip() \
                and abs(prev["bottom"] - lay["top"]) < 0.05:
            out.append((lay["top"], z_collar - lay["top"], prev["code"].strip(), code))
        prev = lay
    return out


def _match(ca, cb):
    """Order-preserving match of contacts with the same (upper, lower) pair (LCS)."""
    n, m = len(ca), len(cb)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if ca[i][2:] == cb[j][2:]:
                dp[i][j] = 1 + dp[i + 1][j + 1]
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if ca[i][2:] == cb[j][2:]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


class StructureBuilder(object):
    """
    holes: list of dict(x, z (collar), bottom_z, contacts=[(depth,z,upper,lower)],
                        discon=[(depth, code, name, angle)], name)
    terrain: (xs, zs);  soil_rock: list of (x, z) or None;  box: (xmin, zmin, xmax, zmax)
    rock_att: (dip_dir, dip) or None;  fault_att: (dip_dir, dip) or None
    az_at(x): section azimuth at chainage x;  col_w: borehole column width (m)
    """

    def __init__(self, holes, terrain, soil_rock, box, az_at, rock_att=None, fault_att=None,
                 use_core_angle=False, col_w=2.0, correlate=False):
        self.holes = sorted(holes, key=lambda h: h["x"])
        self.tx, self.tz = terrain
        self.soil_rock = soil_rock
        self.box = box
        self.az_at = az_at
        self.rock_att = rock_att
        self.fault_att = fault_att
        self.use_core_angle = use_core_angle
        self.correlate = correlate
        self.col_w = col_w
        self.lines = []      # (type, [(x,z),...], attrs)
        self.notes = []
        self.faults = []

    # ------------------------------------------------------------ helpers
    def _terrain_line(self):
        return list(zip(self.tx, self.tz))

    def _base_terminators(self):
        t = [("地形", self._terrain_line())]
        if self.soil_rock:
            t.append(("土岩界面", self.soil_rock))
        return t

    def rock_app(self, x, dip=None):
        if not self.rock_att:
            return 0.0
        return apparent_dip(self.rock_att[0], self.rock_att[1] if dip is None else dip, self.az_at(x))

    # ------------------------------------------------------------ 4. faults
    def build_faults(self):
        pts = [(h, d) for h in self.holes for d in h["discon"] if d[1] in FAULT_TYPES]
        if not pts:
            return
        if not self.fault_att:
            self.notes.append("有斷層紀錄但未輸入斷層代表位態，斷層只以不連續面符號標示，未延伸繪製")
            return
        terms = self._base_terminators()
        for h, (depth, code, name, angle) in pts:
            p = (h["x"], h["z"] - depth)
            a = apparent_dip(self.fault_att[0], self.fault_att[1], self.az_at(h["x"]))
            v = direction(a)
            # skip if this fault point already lies on a fault drawn from another borehole
            if any(_dist_point_line(p, f) < 0.5 for f in self.faults):
                continue
            e1, _t1 = cast(p, v, terms, self.box)
            e2, _t2 = cast(p, (-v[0], -v[1]), terms, self.box)
            line = [e2, p, e1]
            self.faults.append(line)
            self.lines.append(("斷層", line, {"孔號": h["name"], "深度": depth, "類別": name,
                                             "視傾角": round(a, 1)}))

    def _structure_terminators(self):
        return self._base_terminators() + [("斷層", f) for f in self.faults]

    # ------------------------------------------------------------ 5. rock contacts
    def build_contacts(self):
        H = self.holes
        if not any(h["contacts"] for h in H):
            return
        if not self.rock_att:
            self.notes.append("未輸入岩層代表位態：岩層界線以水平延伸")
        # links[i][k] = (j, k2) matched contact in the right neighbour
        right = [dict() for _ in H]
        left = [dict() for _ in H]
        for i in range(len(H) - 1):
            if not self.correlate:
                break  # default: every contact is drawn at the apparent dip from its own borehole
            if H[i + 1]["x"] - H[i]["x"] < self.col_w:
                continue  # overlapping columns: correlating them would draw a vertical zig-zag
            for a, b in _match(H[i]["contacts"], H[i + 1]["contacts"]):
                pa = (H[i]["x"], H[i]["contacts"][a][1])
                pb = (H[i + 1]["x"], H[i + 1]["contacts"][b][1])
                if any(segment_hits(pa, pb, f) for f in self.faults):
                    continue  # do not correlate across a fault
                right[i][a] = b
                left[i + 1][b] = a
        terms = self._structure_terminators()
        done = set()
        for i, h in enumerate(H):
            for k in range(len(h["contacts"])):
                if (i, k) in done or k in left[i]:
                    continue
                chain = [(i, k)]
                while chain[-1][1] in right[chain[-1][0]]:
                    ii, kk = chain[-1]
                    chain.append((ii + 1, right[ii][kk]))
                for c in chain:
                    done.add(c)
                pts = [(H[ii]["x"], H[ii]["contacts"][kk][1]) for ii, kk in chain]
                i0, i1 = chain[0][0], chain[-1][0]
                left_end, tl = self._extend(pts[0], -1, i0, H[i0]["contacts"][chain[0][1]], terms)
                right_end, tr = self._extend(pts[-1], +1, i1, H[i1]["contacts"][chain[-1][1]], terms)
                up, lo = H[i]["contacts"][k][2], H[i]["contacts"][k][3]
                self.lines.append(("地層界線", [left_end] + pts + [right_end], {
                    "孔號": "、".join(H[ii]["name"] for ii, _ in chain), "上層": up, "下層": lo,
                    "類別": "岩層界線", "終止": "%s / %s" % (tl, tr)}))

    def _extend(self, p, side, hole_idx, contact, terms):
        a = self.rock_app(p[0])
        v = direction(a)
        v = (v[0] * side, v[1] * side)
        t = list(terms)
        # any borehole further along whose logged interval would be crossed: the contact cannot pass
        # through a log that does not show it at that depth (overlapping columns are ignored)
        x0 = self.holes[hole_idx]["x"]
        for n in self.holes:
            if (n["x"] - x0) * side >= self.col_w:
                xe = n["x"] - side * self.col_w / 2.0
                t.append(("鄰孔", [(xe, n["z"]), (xe, n["bottom_z"])]))
        return cast(p, v, t, self.box)

    # ------------------------------------------------------------ 6. apparent-dip form lines
    def build_fabric(self):
        pts = [(h, d) for h in self.holes for d in h["discon"] if d[1] in FABRIC_TYPES]
        if not pts:
            return
        if not self.rock_att:
            self.notes.append("有層理/劈理紀錄但未輸入岩層代表位態，無法判定視傾角方向，未繪製地層視傾角線")
            return
        terms = self._structure_terminators()
        seen = []
        for h, (depth, code, name, angle) in pts:
            p = (h["x"], h["z"] - depth)
            dip = angle if (self.use_core_angle and angle is not None) else None
            a = self.rock_app(p[0], dip)
            v = direction(a)
            # skip near-duplicates (parallel lines closer than 0.3 m)
            nrm = (-v[1], v[0])
            off = p[0] * nrm[0] + p[1] * nrm[1]
            if any(abs(a - a2) < 0.5 and abs(off - o2) < 0.3 for a2, o2 in seen):
                continue
            seen.append((a, off))
            e1, t1 = cast(p, v, terms, self.box)
            e2, t2 = cast(p, (-v[0], -v[1]), terms, self.box)
            self.lines.append(("地層視傾角", [e2, p, e1], {"孔號": h["name"], "深度": depth, "類別": name,
                                                       "視傾角": round(a, 1), "終止": "%s / %s" % (t2, t1)}))

    # ------------------------------------------------------------ other discontinuities
    def discon_app(self, x, code, angle):
        """Apparent dip (signed) of one discontinuity record, or None if no direction is known.

        Direction: fault types use the fault attitude, everything else the rock attitude.
        Dip: bedding-type fabric uses the representative dip (or the MDB angle when
        use_core_angle is set); the other types use their own MDB angle.
        """
        az = self.az_at(x)
        if code in FAULT_TYPES:
            if not self.fault_att:
                return None
            return apparent_dip(self.fault_att[0], self.fault_att[1] if angle is None else angle, az)
        if not self.rock_att:
            return None
        if code in FABRIC_TYPES and not (self.use_core_angle and angle is not None):
            dip = self.rock_att[1]
        else:
            dip = angle if angle is not None else self.rock_att[1]
        return apparent_dip(self.rock_att[0], dip, az)

    def build_ticks(self, half_len=2.5):
        """Joints, shear planes, fissures, tension cracks, veins: short segments centred on the record.

        With a rock attitude the segment is drawn at its apparent dip (MDB angle, representative
        dip direction).  Without one only the core angle is known; the segment then rises towards +x
        (same convention as the 不連續面 symbols).
        """
        for h in self.holes:
            for depth, code, name, angle in h["discon"]:
                if code not in SHORT_TYPES:
                    continue
                a = self.discon_app(h["x"], code, angle)
                if a is None:
                    ang = math.radians(angle if angle is not None else 45.0)
                    dx, dz = math.cos(ang) * half_len, math.sin(ang) * half_len
                else:
                    v = direction(a)
                    dx, dz = v[0] * half_len, v[1] * half_len
                p = (h["x"], h["z"] - depth)
                self.lines.append((name, [(p[0] - dx, p[1] - dz), (p[0] + dx, p[1] + dz)],
                                   {"孔號": h["name"], "深度": depth, "類別": name,
                                    "視傾角": None if a is None else round(a, 1)}))

    def build(self):
        self.build_faults()
        self.build_contacts()
        self.build_fabric()
        self.build_ticks()
        return self


def _dist_point_line(p, line):
    best = 1e18
    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        vx, vz = b[0] - a[0], b[1] - a[1]
        L2 = vx * vx + vz * vz
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vz) / L2))
        dx, dz = p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vz)
        best = min(best, math.hypot(dx, dz))
    return best
