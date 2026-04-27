import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import os

# --- Constants ---
EARTH_RADIUS_M = 6371000

# --- Geometry Utilities ---

def haversine_distance(p1, p2):
    """Distance between two points in meters."""
    lat1, lon1 = math.radians(p1['lat']), math.radians(p1['lon'])
    lat2, lon2 = math.radians(p2['lat']), math.radians(p2['lon'])
    
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_M * c

def calculate_bearing(p1, p2):
    """Initial bearing from p1 to p2 in degrees (0-360)."""
    lat1, lon1 = math.radians(p1['lat']), math.radians(p1['lon'])
    lat2, lon2 = math.radians(p2['lat']), math.radians(p2['lon'])
    
    dlon = lon2 - lon1
    
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    
    initial_bearing = math.atan2(x, y)
    initial_bearing = math.degrees(initial_bearing)
    return (initial_bearing + 360) % 360

def offset_point(p, distance_m, bearing_deg):
    """Offset a point by distance and bearing."""
    lat1 = math.radians(p['lat'])
    lon1 = math.radians(p['lon'])
    bearing = math.radians(bearing_deg)
    
    lat2 = math.asin(math.sin(lat1) * math.cos(distance_m / EARTH_RADIUS_M) +
                     math.cos(lat1) * math.sin(distance_m / EARTH_RADIUS_M) * math.cos(bearing))
    
    lon2 = lon1 + math.atan2(math.sin(bearing) * math.sin(distance_m / EARTH_RADIUS_M) * math.cos(lat1),
                             math.cos(distance_m / EARTH_RADIUS_M) - math.sin(lat1) * math.sin(lat2))
    
    return {'lat': math.degrees(lat2), 'lon': math.degrees(lon2)}

# --- Crossing Detection (Reference: track_analysis_utils.dart) ---

def get_side(lat, lon, line_start, line_end):
    """Helper: Calculate which side of line segment AB point P is on."""
    # (xB - xA)(yP - yA) - (yB - yA)(xP - xA)
    # Using lon as x, lat as y
    return (line_end['lon'] - line_start['lon']) * (lat - line_start['lat']) - \
           (line_end['lat'] - line_start['lat']) * (lon - line_start['lon'])

def is_forward_crossing(prev, curr, line_start, line_end, direction='CCW'):
    """Check if trajectory segment crossings start line in forward direction."""
    side_prev = get_side(prev['lat'], prev['lon'], line_start, line_end)
    side_curr = get_side(curr['lat'], curr['lon'], line_start, line_end)
    
    # Check if a numeric bearing was passed as direction, if so default to CCW
    is_cw = False
    if isinstance(direction, str) and direction.upper() == 'CW':
        is_cw = True

    if side_prev * side_curr < 0:
        # Potential crossing, now check if it's within the line segment bounds
        intersect = calculate_intersection(prev, curr, line_start, line_end)
        if intersect:
            if is_cw:
                # CW: Left -> Right (sidePrev > 0 && sideCurr < 0)
                return side_prev > 0 and side_curr < 0
            else:
                # CCW: Right -> Left (sidePrev < 0 && sideCurr > 0)
                return side_prev < 0 and side_curr > 0
    return False

def calculate_intersection(p1, p2, s1, s2):
    """Calculate intersection point and time between two line segments."""
    x1, y1 = p1['lon'], p1['lat']
    x2, y2 = p2['lon'], p2['lat']
    x3, y3 = s1['lon'], s1['lat']
    x4, y4 = s2['lon'], s2['lat']
    
    denom = (y4 - y3) * (x2 - x1) - (x4 - x3) * (y2 - y1)
    if denom == 0:
        return None
    
    ua = ((x4 - x3) * (y1 - y3) - (y4 - y3) * (x1 - x3)) / denom
    ub = ((x2 - x1) * (y1 - y3) - (y2 - y1) * (x1 - x3)) / denom
    
    if 0 <= ub <= 1:
        x = x1 + ua * (x2 - x1)
        y = y1 + ua * (y2 - y1)
        
        # Interpolate time if available
        it_time = None
        if p1.get('time') and p2.get('time'):
            t1 = p1['time'].timestamp()
            t2 = p2['time'].timestamp()
            t = t1 + ua * (t2 - t1)
            it_time = datetime.fromtimestamp(t, tz=timezone.utc)
            
        return {'lat': y, 'lon': x, 'time': it_time}
    return None

# --- GPX Implementation ---

def load_gpx(path):
    """Load GPX file into a list of points."""
    tree = ET.parse(path)
    root = tree.getroot()
    
    # Detect namespace
    ns = ""
    if '}' in root.tag:
        ns = root.tag.split('}')[0] + '}'
    
    points = []
    
    # Find points regardless of exact hierarchy if possible, or use detected ns
    for trk in root.findall(f'.//{ns}trk'):
        for seg in trk.findall(f'.//{ns}trkseg'):
            for pt in seg.findall(f'.//{ns}trkpt'):
                lat = float(pt.get('lat'))
                lon = float(pt.get('lon'))
                time_el = pt.find(f'{ns}time')
                time = None
                if time_el is not None:
                    time_str = time_el.text
                    try:
                        time = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                    except:
                        pass
                
                # Extract speed from extensions if present
                speed = 0.0
                ext = pt.find(f'{ns}extensions')
                if ext is not None:
                    for child in ext.iter():
                        if 'speed' in child.tag.lower():
                            try:
                                speed = float(child.text)
                            except:
                                pass
                
                points.append({
                    'lat': lat,
                    'lon': lon,
                    'time': time,
                    'speed': speed,
                    'lap': 1,
                    'xml_element': pt 
                })
    return points, tree, ns

def save_gpx(path, tree, points, ns):
    """Save GPX file with updated lap information in extensions."""
    for pt_data in points:
        pt = pt_data['xml_element']
        ext = pt.find(f'{ns}extensions')
        if ext is None:
            ext = ET.SubElement(pt, f'{ns}extensions')
        
        # Add lap as a sub-element of extensions
        # We'll use the same namespace as extensions for the lap tag as well
        lap_tag = f'{ns}lap'
        lap_el = ext.find(lap_tag)
        if lap_el is None:
            lap_el = ET.SubElement(ext, lap_tag)
        lap_el.text = str(pt_data['lap'])
        
    tree.write(path, encoding='utf-8', xml_declaration=True)

# --- VBO Implementation ---

def load_vbo(path):
    """Load VBO file."""
    with open(path, 'r', encoding='latin-1') as f:
        lines = f.readlines()
    
    header_lines = []
    column_names = []
    data_rows = []
    in_data = False
    in_column_names = False
    
    for line in lines:
        stripped = line.strip()
        if stripped == '[column names]':
            in_column_names = True
            header_lines.append(line)
            continue
        if stripped == '[data]':
            in_data = True
            header_lines.append(line)
            continue
        
        if in_data:
            if stripped:
                data_rows.append(stripped.split())
        elif in_column_names:
            column_names = stripped.split()
            in_column_names = False
            header_lines.append(line)
        else:
            header_lines.append(line)
            
    # Map row to dictionary for easier processing
    points = []
    lat_idx = -1
    lon_idx = -1
    time_idx = -1
    heading_idx = -1
    speed_idx = -1
    
    for i, name in enumerate(column_names):
        name_lower = name.lower()
        if 'lat' in name_lower and 'acc' not in name_lower:
            lat_idx = i
        if ('long' in name_lower or 'lon' in name_lower) and 'acc' not in name_lower:
            lon_idx = i
        if 'time' in name_lower: 
            time_idx = i
        if 'heading' in name_lower: 
            heading_idx = i
        if ('velocity' in name_lower or 'speed' in name_lower): 
            speed_idx = i

    for row in data_rows:
        # VBO Lat/Long are often in minutes or decimal degrees. 
        # Racelogic standard is minutes: DDDMM.MMMMM
        def parse_vbo_coord(val):
            try:
                return float(val) / 60.0
            except:
                return 0.0

        lat = parse_vbo_coord(row[lat_idx]) if lat_idx != -1 else 0.0
        # Reference project (merge_demo) convention: standard longitude = - VBO_lon / 60.0
        lon = -parse_vbo_coord(row[lon_idx]) if lon_idx != -1 else 0.0
        heading = float(row[heading_idx]) if heading_idx != -1 else 0.0
        speed = float(row[speed_idx]) if speed_idx != -1 else 0.0
        
        # VBO Time is HHMMSS.SS
        time_obj = None
        if time_idx != -1:
            try:
                t_str = row[time_idx]
                h = int(t_str[0:2])
                m = int(t_str[2:4])
                s = float(t_str[4:])
                time_obj = datetime(2026, 1, 1, h, m, int(s), int((s - int(s)) * 1000000), tzinfo=timezone.utc)
            except:
                pass
        
        points.append({
            'lat': lat,
            'lon': lon,
            'time': time_obj,
            'heading': heading,
            'speed': speed,
            'lap': 1,
            'raw_row': row
        })
        
    return points, column_names, header_lines

def save_vbo(path, points, column_names, header_lines):
    """Save VBO file with updated Lap channel."""
    # Check if Lap is already in columns
    lap_idx = -1
    for i, name in enumerate(column_names):
        if name.lower() == 'lap':
            lap_idx = i
            break
    
    # Update header lines if Lap is new
    if lap_idx == -1:
        new_header = []
        in_col_names = False
        in_col_units = False
        for line in header_lines:
            stripped = line.strip()
            if stripped == '[column names]':
                in_col_names = True
                new_header.append(line)
            elif in_col_names:
                new_header.append(" ".join(column_names) + " lap\n")
                in_col_names = False
            elif stripped == '[column units]':
                in_col_units = True
                new_header.append(line)
            elif in_col_units:
                new_header.append(line.strip() + " #\n")
                in_col_units = False
            else:
                new_header.append(line)
        header_lines = new_header
    
    with open(path, 'w', encoding='latin-1') as f:
        in_data = False
        for line in header_lines:
            f.write(line)
            if line.strip() == '[data]':
                in_data = True
        
        if in_data:
            for pt in points:
                row = pt['raw_row']
                if lap_idx == -1:
                    # Append new column
                    f.write(" ".join(row) + f" {pt['lap']:03d}\n")
                else:
                    # Update existing column
                    row[lap_idx] = f"{pt['lap']:03d}"
                    f.write(" ".join(row) + "\n")

# --- Splitting Logic ---

def split_into_laps(points, line_start, line_end, direction='CCW'):
    """Split point list into laps by marking 'lap' attribute."""
    if len(points) < 2:
        return points
    
    current_lap = 1
    for i in range(1, len(points)):
        prev = points[i-1]
        curr = points[i]
        
        # Check crossing
        if is_forward_crossing(prev, curr, line_start, line_end, direction):
            current_lap += 1
            
        points[i]['lap'] = current_lap
    
    # First point inherits from second point or stays 1
    points[0]['lap'] = points[1]['lap'] if len(points) > 1 else 1
    return points
