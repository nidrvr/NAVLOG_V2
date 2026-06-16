import xml.etree.ElementTree as ET
import math
import json
import datetime
from geopy.distance import geodesic
import airportsdata
import geomag
from weather_api import get_live_upper_winds, get_forecast_altimeter
from checkpoint_math import process_checkpoints
# ---------------------------------------------------------
# GLOBAL DATABASES
# ---------------------------------------------------------
AIRPORT_DB = airportsdata.load('icao')

try:
    with open('compass_cards.json', 'r') as f:
        COMPASS_DB = json.load(f)
except FileNotFoundError:
    print("[!] compass_cards.json not found. Deviation engine disabled.")
    COMPASS_DB = {}


def save_flight_config(config_data, filename="flight_cache.json"):
    with open(filename, 'w') as f:
        json.dump(config_data, f, indent=4)

def load_flight_config(filename="flight_cache.json"):
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return None

# ---------------------------------------------------------
# MATH & AIRSPEED HELPERS
# ---------------------------------------------------------
def safe_interpolate(target, x1, y1, x2, y2):
    if x1 == x2:
        return float(y1)
    return float(y1) + (target - x1) * (float(y2) - float(y1)) / (x2 - x1)

def generate_circuit_leg(destination_id):
    return {
        'to': f"{destination_id} (CCT)",
        'alt': 'Circuit',
        'oat': '', 'rpm': '', 'ias': '', 'tas': '',
        'track_true': '', 'wind_true': '', 'heading_true': '',
        'variation': '', 'heading_mag': '', 'ground_speed': '',
        'dist_formatted': '0',       # No distance credited
        'time_formatted': '6',       # 6 minutes
        'gph': 10.0,
        'fuel_req': 1.0,             # 10 gph for 6 mins = 1.0 gal
        'fuel_formatted': '1.0'
    }

def get_magnetic_variation(lat, lon, altitude_ft=0):
    declination = geomag.declination(lat, lon, h=altitude_ft)
    direction = "E" if declination > 0 else "W"
    return f"{abs(int(round(declination)))}{direction}"
def calculate_pressure_altitude(elevation, altimeter_setting):
    if not altimeter_setting: return elevation
    return elevation + (29.92 - altimeter_setting) * 1000


def get_kcas_from_kias(kias_target):
    try:
        with open('c172_airspeed_calibration.json', 'r') as f:
            cal = json.load(f)

        table = cal["airspeed_calibration_normal_static"]["flaps_up"]

        lower, upper = None, None
        for row in table:
            if row["KIAS"] <= kias_target:
                lower = row
            if row["KIAS"] >= kias_target and upper is None:
                upper = row
                break

        if lower and upper:
            if lower["KIAS"] == upper["KIAS"]:
                return lower["KCAS"]
            ratio = (kias_target - lower["KIAS"]) / (upper["KIAS"] - lower["KIAS"])
            return lower["KCAS"] + ratio * (upper["KCAS"] - lower["KCAS"])

        return kias_target
    except Exception:
        return kias_target


def get_poh_ias_from_cas(cas):
    try:
        with open('c172_airspeed_calibration.json', 'r') as f:
            data = json.load(f)
        cal_table = data["airspeed_calibration_normal_static"]["flaps_up"]

        for i in range(len(cal_table) - 1):
            lower = cal_table[i]
            upper = cal_table[i + 1]
            if lower["KCAS"] <= cas <= upper["KCAS"]:
                if lower["KCAS"] == upper["KCAS"]:
                    return lower["KIAS"]
                fraction = (cas - lower["KCAS"]) / (upper["KCAS"] - lower["KCAS"])
                ias = lower["KIAS"] + (fraction * (upper["KIAS"] - lower["KIAS"]))
                return int(round(ias))

        if cas < cal_table[0]["KCAS"]: return cal_table[0]["KIAS"]
        if cas > cal_table[-1]["KCAS"]: return cal_table[-1]["KIAS"]
    except Exception:
        return cas
    return cas


def calculate_cas_from_tas(tas, altitude, oat_c):
    delta = (1 - (0.00000687559 * altitude)) ** 5.25588
    theta = (oat_c + 273.15) / 288.15
    sigma = delta / theta
    if sigma <= 0: return tas
    return tas * math.sqrt(sigma)


def calculate_tas_from_ias(kias, altitude, oat_c):
    kcas = get_kcas_from_kias(kias)
    delta = (1 - (0.00000687559 * altitude)) ** 5.25588
    theta = (oat_c + 273.15) / 288.15
    sigma = delta / theta
    if sigma <= 0: return kcas
    tas = kcas / math.sqrt(sigma)
    return int(round(tas))


def calculate_isa_deviation(altitude, oat, fd_alt=None):
    standard_temp = 15 - (2 * (altitude / 1000))
    return oat - standard_temp


def get_steer_heading(target_mag_heading, aircraft_id, compass_db):
    if aircraft_id not in compass_db: return target_mag_heading
    table = compass_db[aircraft_id]
    headings = sorted([int(k) for k in table.keys()])

    h1, h2 = headings[-1], headings[0]
    for i in range(len(headings) - 1):
        if headings[i] <= target_mag_heading < headings[i + 1]:
            h1, h2 = headings[i], headings[i + 1]
            break

    s1 = table[str(h1).zfill(3)]
    s2 = table[str(h2).zfill(3)]

    if h1 == 330 and h2 == 0:
        h2 = 360
        s2 = s2 + 360 if s2 < 180 else s2

    if h1 == h2: return s1
    steer = s1 + (s2 - s1) * (target_mag_heading - h1) / (h2 - h1)
    return round(steer) % 360


def get_airport_elevation(station_id):
    station_id = station_id.strip().upper()
    try:
        return int(AIRPORT_DB[station_id]['elevation'])
    except KeyError:
        return None


# ---------------------------------------------------------
# THE EXTRACTOR (Single-File Architecture)
# ---------------------------------------------------------
def parse_skyvector_fpl(filepath):
    import xml.etree.ElementTree as ET

    tree = ET.parse(filepath)
    root = tree.getroot()
    ns = {'g': 'http://www8.garmin.com/xmlschemas/FlightPlan/v1'}

    all_points_dict = {}
    all_points = []

    # 1. Extract the "Universe" from the waypoint table
    for waypoint in root.findall('.//g:waypoint-table/g:waypoint', ns):
        ident = waypoint.find('g:identifier', ns).text
        lat = float(waypoint.find('g:lat', ns).text)
        lon = float(waypoint.find('g:lon', ns).text)
        wp_data = {"id": ident, "lat": lat, "lon": lon}
        all_points_dict[ident] = wp_data
        all_points.append(wp_data)

    # 2. Extract the "Spine" from the route sequence
    route = []
    for rp in root.findall('.//g:route/g:route-point', ns):
        ident = rp.find('g:waypoint-identifier', ns).text
        if ident in all_points_dict:
            route.append(all_points_dict[ident])

    # Return both datasets for the new architecture
    return route, all_points


# ---------------------------------------------------------
# BILINEAR POH PERFORMANCE LOOKUPS
# ---------------------------------------------------------
def get_poh_cruise_performance(leg_altitude, isa_dev, leg_rpm):
    try:
        with open('c172_poh.json', 'r') as f:
            poh_data = json.load(f)
            poh_cruise_data = poh_data.get("cruise", poh_data)
    except Exception:
        return 110.0, 8.5

    alt_keys = sorted([int(k) for k in poh_cruise_data.keys()])
    alt_low = next((a for a in reversed(alt_keys) if a <= leg_altitude), alt_keys[0])
    alt_high = next((a for a in alt_keys if a >= leg_altitude), alt_keys[-1])

    def get_perf_at_alt(alt_level):
        temp_data = poh_cruise_data[str(alt_level)]

        # Map the float deviation to the exact string keys and numeric bounds
        if isa_dev <= -20:
            t_low_str, t_high_str, dev_low, dev_high = "ISA_minus_20", "ISA_minus_20", -20.0, -20.0
        elif isa_dev <= 0:
            t_low_str, t_high_str, dev_low, dev_high = "ISA_minus_20", "ISA", -20.0, 0.0
        elif isa_dev >= 20:
            t_low_str, t_high_str, dev_low, dev_high = "ISA_plus_20", "ISA_plus_20", 20.0, 20.0
        else:
            t_low_str, t_high_str, dev_low, dev_high = "ISA", "ISA_plus_20", 0.0, 20.0

        rpm_str = str(leg_rpm)
        rpm_block_low = temp_data.get(t_low_str, {}).get(rpm_str, {})
        rpm_block_high = temp_data.get(t_high_str, {}).get(rpm_str, {})

        tas_low_t = float(rpm_block_low.get("tas", rpm_block_low.get("TAS", 110)))
        gph_low_t = float(rpm_block_low.get("gph", rpm_block_low.get("GPH", 8.0)))

        tas_high_t = float(rpm_block_high.get("tas", rpm_block_high.get("TAS", 110)))
        gph_high_t = float(rpm_block_high.get("gph", rpm_block_high.get("GPH", 8.0)))

        # Feed the numeric bounds into your safe_interpolate helper
        tas_interp = safe_interpolate(isa_dev, dev_low, tas_low_t, dev_high, tas_high_t)
        gph_interp = safe_interpolate(isa_dev, dev_low, gph_low_t, dev_high, gph_high_t)
        return tas_interp, gph_interp

    tas_a_low, gph_a_low = get_perf_at_alt(alt_low)
    tas_a_high, gph_a_high = get_perf_at_alt(alt_high)

    final_tas = safe_interpolate(leg_altitude, alt_low, tas_a_low, alt_high, tas_a_high)
    final_gph = safe_interpolate(leg_altitude, alt_low, gph_a_low, alt_high, gph_a_high)

    return round(final_tas), round(final_gph, 1)


def get_poh_climb_performance(target_altitude, airport_elevation, isa_dev):
    # 1. Load the data
    try:
        with open('c172_climb.json', 'r') as f:
            data = json.load(f)
            climb_data = data.get("climb_performance", {})
            adj = data.get("constants_and_adjustments", {})
    except Exception as e:
        print(f"Error loading c172_climb.json: {e}")
        return 0, 0, 0

    # 2. Get keys and ensure they are sorted integers
    alt_keys = sorted([int(k) for k in climb_data.keys()])

    # Helper function for linear interpolation
    def interp(x, x0, y0, x1, y1):
        if x1 == x0: return y0
        return y0 + (x - x0) * (y1 - y0) / (x1 - x0)

    # 3. Interpolation function for the flat structure
    def get_data_at_alt(alt_val):
        # Clamp altitude to available range
        alt_val = max(min(alt_val, alt_keys[-1]), alt_keys[0])

        # Find bracket
        alt_low = next((a for a in reversed(alt_keys) if a <= alt_val), alt_keys[0])
        alt_high = next((a for a in alt_keys if a >= alt_val), alt_keys[-1])

        d_low = climb_data[str(alt_low)]
        d_high = climb_data[str(alt_high)]

        t = interp(alt_val, alt_low, d_low['MIN'], alt_high, d_high['MIN'])
        f = interp(alt_val, alt_low, d_low['GAL'], alt_high, d_high['GAL'])
        d = interp(alt_val, alt_low, d_low['NM'], alt_high, d_high['NM'])
        return t, f, d

    # 4. Calculate performance from airport to target
    t1, f1, d1 = get_data_at_alt(target_altitude)
    t0, f0, d0 = get_data_at_alt(airport_elevation)

    time_req = max(0, t1 - t0)
    fuel_req = max(0, f1 - f0)
    dist_req = max(0, d1 - d0)

    # 5. Apply Temperature Penalty (if ISA > 0)
    if isa_dev > 0:
        pct = adj.get("temp_adjustment_percentage", 0.10)
        thresh = adj.get("temp_adjustment_threshold_c", 10)
        multiplier = 1.0 + (isa_dev / thresh) * pct
        time_req *= multiplier
        fuel_req *= multiplier
        dist_req *= multiplier


    return time_req, fuel_req, dist_req

# ---------------------------------------------------------
# ENGINE CORE
# ---------------------------------------------------------
def process_flight_plan(route_data, leg_configs):
    legs = []

    for i in range(len(route_data) - 1):
        start_point = route_data[i]
        end_point = route_data[i + 1]

        current_config = leg_configs.get(i, leg_configs.get("DEFAULT", {}))

        is_climb_leg = current_config.get("is_climb_leg", False)
        leg_altitude = current_config.get("altitude", 0)
        leg_rpm = current_config.get("rpm", 2300)
        leg_wind_dir = current_config.get("wind_dir", 0)
        leg_wind_spd = current_config.get("wind_spd", 0)
        leg_temp = current_config.get("leg_temp", 15)

        distance_nm = geodesic((start_point["lat"], start_point["lon"]),
                               (end_point["lat"], end_point["lon"])).nautical

        track_true = int(round(math.degrees(math.atan2(
            math.sin(math.radians(end_point["lon"] - start_point["lon"])) * math.cos(math.radians(end_point["lat"])),
            math.cos(math.radians(start_point["lat"])) * math.sin(math.radians(end_point["lat"])) -
            math.sin(math.radians(start_point["lat"])) * math.cos(math.radians(end_point["lat"])) * math.cos(
                math.radians(end_point["lon"] - start_point["lon"]))
        )) % 360))
        if track_true == 0: track_true = 360

        isa_deviation = calculate_isa_deviation(leg_altitude, leg_temp)

        wind_angle = math.radians(leg_wind_dir - track_true)
        wind_cross = leg_wind_spd * math.sin(wind_angle)
        wind_head = leg_wind_spd * math.cos(wind_angle)

        if is_climb_leg:
            tas_for_wind = calculate_tas_from_ias(73, leg_altitude, leg_temp)
        else:
            tas_for_wind, _ = get_poh_cruise_performance(leg_altitude, isa_deviation, leg_rpm)

        wca = math.degrees(math.asin(wind_cross / tas_for_wind)) if tas_for_wind > 0 else 0
        heading_true = int(round((track_true + wca) % 360))
        if heading_true == 0: heading_true = 360

        ground_speed = tas_for_wind - wind_head

        # Get the exact variation for this leg
        variation_str = get_magnetic_variation(start_point["lat"], start_point["lon"], leg_altitude)

        # We need the numeric value for the math, so extract it from the string
        avg_var = -int(variation_str[:-1]) if "E" in variation_str else int(variation_str[:-1])

        heading_mag = int(round((heading_true + avg_var) % 360))
        if heading_mag == 0: heading_mag = 360


        # NEW: Calculate compass/steer heading (HSI)
        aircraft_id = leg_configs.get("AIRCRAFT_ID", "")
        hsi_heading = get_steer_heading(heading_mag, aircraft_id, COMPASS_DB)

        if is_climb_leg:
            if i == 0:
                current_dep_elev = leg_configs.get("DEP_ELEVATION", 0)
            else:
                current_dep_elev = leg_configs.get(i - 1, {}).get("DEST_ELEVATION", 0)

            climb_time, climb_fuel, climb_dist = get_poh_climb_performance(
                target_altitude=leg_altitude,
                airport_elevation=current_dep_elev,
                isa_dev=isa_deviation
            )

            climb_ias = 73
            climb_tas = calculate_tas_from_ias(climb_ias, leg_altitude, leg_temp)

            cruise_dist = max(0, distance_nm - climb_dist)
            _, cruise_gph = get_poh_cruise_performance(leg_altitude, isa_deviation, leg_rpm)
            cruise_time = (cruise_dist / ground_speed) * 60 if ground_speed > 0 else 0
            cruise_fuel = (cruise_time / 60) * cruise_gph

            # --- DISTANCE SYNCHRONIZATION (Climb Leg Only) ---
            total_dist_rounded = int(round(distance_nm))
            climb_dist_rounded = int(round(climb_dist))
            cruise_dist_rounded = total_dist_rounded - climb_dist_rounded
            dist_str = f"{climb_dist_rounded}+{cruise_dist_rounded}/{total_dist_rounded}"

            # --- TIME SYNCHRONIZATION (Climb Leg Only) ---
            leg_time = climb_time + cruise_time
            total_time_rounded = int(round(leg_time))
            climb_time_rounded = int(round(climb_time))
            cruise_time_rounded = total_time_rounded - climb_time_rounded
            time_str = f"{climb_time_rounded}+{cruise_time_rounded}/{total_time_rounded}"

            total_fuel = climb_fuel + cruise_fuel
            fuel_formatted = f"{climb_fuel:.1f}+{cruise_fuel:.1f}"

            rpm_formatted = f"({leg_rpm})"
            ias_formatted = str(climb_ias)
            tas_formatted = str(int(climb_tas))

        else:
            tas, cruise_gph = get_poh_cruise_performance(leg_altitude, isa_deviation, leg_rpm)

            cruise_cas = calculate_cas_from_tas(tas, leg_altitude, leg_temp)
            cruise_ias = get_poh_ias_from_cas(cruise_cas)

            total_time = (distance_nm / ground_speed) * 60 if ground_speed > 0 else 0
            total_fuel = (total_time / 60) * cruise_gph

            rpm_formatted = str(leg_rpm)
            fuel_formatted = f"{total_fuel:.1f}"

            ias_formatted = str(cruise_ias)
            tas_formatted = str(int(tas))

            # STANDARD ROUNDING (Cruise Leg Only)
            dist_str = str(int(round(distance_nm)))
            time_str = str(int(round(total_time)))

            # --- CALCULATE COMPASS HEADING ---
        aircraft_id = leg_configs.get("AIRCRAFT_ID", "UNKNOWN")
        compass_heading = get_steer_heading(heading_mag, aircraft_id, COMPASS_DB)

        # Append Dual Keys to support BOTH the console loop and the pdf_stamper
        legs.append({
            "to": end_point["id"],
            "altitude": leg_altitude,
            "oat": int(leg_temp),
            "rpm_formatted": rpm_formatted,
            "ias": ias_formatted,
            "tas": tas_formatted,
            "track_true": f"{track_true:03d}",
            "wind_true": f"{int(leg_wind_dir):03d}/{int(leg_wind_spd):02d}",
            "heading_true": f"{heading_true:03d}",
            "variation": f"{variation_str}",
            "heading_mag": f"{heading_mag:03d}",
            "heading_compass": f"{compass_heading:03d}",
            "ground_speed": ground_speed,
            "dist_formatted": dist_str,
            "time_formatted": time_str,
            "gph": cruise_gph,
            "fuel_formatted": fuel_formatted,
            "fuel_req": total_fuel,
            "rpm": rpm_formatted,
            "track": f"{track_true:03d}",
            "wind": f"{int(leg_wind_dir):03d}/{int(leg_wind_spd):02d}",
            "hdg_true": f"{heading_true:03d}",
            "var": f"{variation_str}",
            "gs": str(int(round(ground_speed))),
            "distance_nm": dist_str,
            "total_time": time_str,
            "fuel": fuel_formatted
        })

        # Inject the circuit leg if this waypoint is marked as a full-stop landing
        if current_config.get("is_landing", False):
            elev = get_airport_elevation(end_point["id"])
            if elev is not None:
                cct_alt = str(int(round((elev + 1000) / 100.0)) * 100)
            else:
                cct_alt = "Circuit"
            legs.append({
                "to": f"{end_point['id']} (CCT)",
                "altitude": cct_alt,
                "is_circuit": True,
                "oat": "-",
                "rpm_formatted": "-",
                "ias": "-",
                "tas": "-",
                "track_true": "-",
                "wind_true": "-",
                "heading_true": "-",
                "variation": "-",
                "heading_mag": "-",
                "heading_compass": "-",
                "ground_speed": "-",
                "dist_formatted": "0",
                "time_formatted": "6",
                "gph": 10.0,
                "fuel_formatted": "1.0",
                "fuel_req": 1.0,
                "rpm": "-",
                "track": "-",
                "wind": "-",
                "hdg_true": "-",
                "var": "-",
                "gs": "1",  # Prevents division by zero in checkpoint_math
                "distance_nm": "0",
                "total_time": "6",
                "fuel": "1.0",
                "is_climb_leg": False
            })

    return legs


# ---------------------------------------------------------
# INTERACTIVE CONFIGURATOR
# ---------------------------------------------------------
def get_leg_configs_interactively(route_data, checkpoints_data=None):
    if checkpoints_data is None: checkpoints_data = []

    cached_data = load_flight_config()
    use_cache = False

    if cached_data:
        ans = input("Found a previous flight config. Use as defaults? (Y/n): ").strip().lower()
        if ans != 'n':
            use_cache = True
            print("--- Using Cached Defaults (Press Enter to accept each) ---")

    print("\n==========================================")
    print("      VFR NAV LOG - FLIGHT CONFIGURATOR     ")
    print("==========================================")

    def_ac = cached_data.get("AIRCRAFT_ID", "") if use_cache else ""
    aircraft_id = input(f"Aircraft Callsign [{def_ac}]: " if def_ac else "Aircraft Callsign (e.g., C-GSEQ): ").strip().upper() or def_ac
    def_pilot = cached_data.get("PILOT_NAME", "") if use_cache else ""
    pilot_name = input(f"Pilot Name [{def_pilot}]: " if def_pilot else "Pilot Name: ").strip().upper() or def_pilot

    leg_configs = {"AIRCRAFT_ID": aircraft_id, "PILOT_NAME": pilot_name}

    default_zulu_time = datetime.datetime.utcnow() + datetime.timedelta(hours=12)
    def_zulu_str = cached_data.get("FLIGHT_DATE_TIME", default_zulu_time.strftime("%Y-%m-%d %H%M")) if use_cache else default_zulu_time.strftime("%Y-%m-%d %H%M")
    while True:
        zulu_input = input(f"1. Enter Zulu Departure Time (YYYY-MM-DD HHMM) [{def_zulu_str}]: ").strip()
        final_zulu_str = zulu_input or def_zulu_str
        try:
            departure_zulu = datetime.datetime.strptime(final_zulu_str, "%Y-%m-%d %H%M")
            leg_configs["FLIGHT_DATE_TIME"] = final_zulu_str
            break
        except ValueError:
            print("   [!] Invalid format. Use: YYYY-MM-DD HHMM")

    departure_airport_id = route_data[0]["id"]
    print(f"\n[DB] Looking up elevation for departure airport: {departure_airport_id}...")
    auto_elevation = get_airport_elevation(departure_airport_id)
    if auto_elevation is not None:
        print(f"     -> Found Elevation: {auto_elevation} ft")
        departure_elevation = auto_elevation
    else:
        def_dep_elev = cached_data.get("DEP_ELEVATION", 0) if use_cache else 0
        dep_elev_input = input(f"   Enter Departure Airport Elevation (ft) [{def_dep_elev}]: ").strip()
        departure_elevation = int(dep_elev_input) if dep_elev_input.isdigit() else def_dep_elev

    leg_configs["DEP_ELEVATION"] = departure_elevation

    print(f"\n[API] Fetching HRDPS Altimeter forecast for {departure_zulu.strftime('%H:00')}Z...")
    altimeter_setting = get_forecast_altimeter(departure_airport_id, departure_zulu)
    print(f"     -> Forecasted Altimeter: {altimeter_setting} inHg")
    alt_override = input("2. Press [Enter] to accept, or type your own (inHg): ").strip()
    if alt_override:
        try: altimeter_setting = float(alt_override)
        except ValueError: pass

    departure_pa = calculate_pressure_altitude(departure_elevation, altimeter_setting)

    # -----------------------------------------------------
    # DYNAMIC ROUTE RECONSTRUCTION (Filters Soft Points)
    # -----------------------------------------------------
    print("\n--> 3. ALIASES & TURNING POINTS")
    aliases = cached_data.get("ALIASES", {}) if use_cache else {}

    all_ids = []
    for pt in route_data:
        if pt['id'] not in all_ids: all_ids.append(pt['id'])
    for checkpoint in checkpoints_data:
        if checkpoint['id'] not in all_ids: all_ids.append(checkpoint['id'])

    dep_id = route_data[0]['id']
    dest_id = route_data[-1]['id']
    renameable_ids = [wid for wid in all_ids if wid not in (dep_id, dest_id)]

    if renameable_ids:
        ans = input("   Do you want to rename your waypoints? [Y/n]: ").strip().lower()
        if ans != 'n':
            for wid in renameable_ids:
                default_alias = aliases.get(wid, "")
                new_name = input(f"   Rename {wid} to [{default_alias}]: " if default_alias else f"   Rename {wid} to: ").strip().upper()
                if new_name: aliases[wid] = new_name
                elif default_alias: aliases[wid] = default_alias
    leg_configs["ALIASES"] = aliases

    tps = cached_data.get("TURNING_POINTS", []) if use_cache else []
    if tps: print(f"   Cached Turning Points: {tps}")

    if input("   Update Turning Points (Leg Boundaries)? (y/N): ").strip().lower() == 'y':
        tp_str = input("   Enter IDs or Aliases separated by space (e.g., OMEMEE): ").strip().upper()
        resolved_tps = []
        reverse_aliases = {v: k for k, v in aliases.items()}

        # ADDED: Map the uppercase version of every known ID back to its original case
        for wid in all_ids:
            if wid.upper() not in reverse_aliases:
                reverse_aliases[wid.upper()] = wid

        for tp in tp_str.split():
            resolved_tps.append(reverse_aliases.get(tp, tp))
        leg_configs["TURNING_POINTS"] = resolved_tps
    else:
        leg_configs["TURNING_POINTS"] = tps
        resolved_tps = tps

    # Filter the Route (Only keep Dep, Dest, and User-defined Hard Points)
    final_route = [route_data[0]]
    for pt in route_data[1:-1]:
        if pt['id'] in resolved_tps:
            final_route.append(pt)
    if len(route_data) > 1:
        final_route.append(route_data[-1])

    # Remove consecutive duplicates
    dedup_route = []
    for pt in final_route:
        if not dedup_route or dedup_route[-1]['id'] != pt['id']:
            dedup_route.append(pt)
    final_route = dedup_route
    leg_configs["FINAL_ROUTE"] = final_route

    print(f"\n   [+] Route collapsed from {len(route_data)-1} segments down to {len(final_route)-1} main legs.")
    print("       (Excluded points will be processed as Observation Checkpoints)")

    # -----------------------------------------------------
    # LEG CONFIGURATION (Loops over filtered route)
    # -----------------------------------------------------
    current_flight_time = departure_zulu
    previous_leg_was_landing = False

    for i in range(len(final_route) - 1):
        start_point = final_route[i]
        end_point = final_route[i + 1]
        start_id = start_point["id"]
        end_id = end_point["id"]

        print(f"\n--> CONFIGURING LEG {i + 1}: {start_id} -> {end_id}")
        str_i = str(i)
        leg_cache = cached_data.get(str_i, {}) if use_cache else {}

        def_alt = leg_cache.get("altitude", 4500)
        alt_input = input(f"   Enter Cruise Altitude (ft) [{def_alt}]: ").strip()
        altitude = int(alt_input) if alt_input.isdigit() else def_alt

        def_rpm = leg_cache.get("rpm", 2300)
        rpm_input = input(f"   Enter Cruise RPM Setting   [{def_rpm}]: ").strip()
        rpm = int(rpm_input) if rpm_input.isdigit() else def_rpm

        coords_1 = (start_point["lat"], start_point["lon"])
        coords_2 = (end_point["lat"], end_point["lon"])
        est_distance_nm = geodesic(coords_1, coords_2).nautical

        waypoint_eta = current_flight_time
        current_flight_time += datetime.timedelta(hours=(est_distance_nm / 110.0))

        leg_weather = get_live_upper_winds(start_id, altitude, start_point["lat"], start_point["lon"], waypoint_eta)
        print(f"     -> Forecast over {start_id} at {waypoint_eta.strftime('%H:%M')}Z: {leg_weather['dir']:03d}° at {leg_weather['spd']} kts, {leg_weather['temp']}°C")

        use_manual = input("   Press [Enter] to use forecast, or type 'm' to override: ").strip().lower()
        if use_manual == 'm':
            leg_weather = {
                "dir": int(input("     Wind Direction (True): ")),
                "spd": int(input("     Wind Speed (kts): ")),
                "temp": float(input("     Temperature (C): "))
            }

        if i < len(final_route) - 2:
            def_land = "y" if leg_cache.get("is_landing", False) else "N"
            land_in = input(f"   Full stop / Landing at {end_id}? (y/N) [{def_land}]: ").strip().lower()
            is_landing = True if land_in == 'y' else (False if land_in == 'n' else leg_cache.get("is_landing", False))
        else:
            is_landing = True

        cruise_pa = calculate_pressure_altitude(altitude, altimeter_setting)
        is_climb_leg = (i == 0) or previous_leg_was_landing

        leg_configs[i] = {
            "altitude": altitude,
            "rpm": rpm,
            "wind_dir": leg_weather["dir"],
            "wind_spd": leg_weather["spd"],
            "leg_temp": leg_weather["temp"],
            "is_climb_leg": is_climb_leg,
            "is_landing": is_landing,
            "departure_pa": departure_pa if is_climb_leg else 0,
            "cruise_pa": cruise_pa if is_climb_leg else altitude
        }

        if is_landing:
            dest_elev = get_airport_elevation(end_id)
            if dest_elev is None:
                def_dest_elev = leg_cache.get("DEST_ELEVATION", 0)
                dep_elev_in = input(f"   Enter {end_id} Elevation for next departure (ft) [{def_dest_elev}]: ").strip()
                dest_elev = int(dep_elev_in) if dep_elev_in.isdigit() else def_dest_elev
            departure_pa = calculate_pressure_altitude(dest_elev, altimeter_setting)
            leg_configs[i]["DEST_ELEVATION"] = dest_elev

        previous_leg_was_landing = is_landing

    print("\n--> 4. CONTINGENCY & RESERVE FUEL")
    res_cache = cached_data.get("FUEL_RESERVES", {}) if use_cache else {}
    def_c_min = res_cache.get("cont_min", 10)
    c_min_in = input(f"   Enter Contingency time (minutes) [{def_c_min}]: ").strip()

    def_c_gph = res_cache.get("cont_gph", 8.5)
    c_gph_in = input(f"   Enter Contingency Fuel Flow (GPH) [{def_c_gph}]: ").strip()

    def_omit = "Y" if not res_cache.get("omit_reserve", False) else "n"
    omit_res_in = input(f"   Include standard 30-min automatic reserve? (Y/n) [{def_omit}]: ").strip().lower()

    def_planned = res_cache.get("planned_ramp_fuel", "")
    planned_in = input(f"   Enter Planned Ramp Fuel in GAL (Leave blank for minimum req) [{def_planned}]: ").strip()

    leg_configs["FUEL_RESERVES"] = {
        "cont_min": int(c_min_in) if c_min_in.isdigit() else def_c_min,
        "cont_gph": float(c_gph_in) if c_gph_in else def_c_gph,
        "omit_reserve": True if omit_res_in == 'n' else False,
        "planned_ramp_fuel": float(planned_in) if planned_in else ""
    }

    leg_configs["FLIGHT_DATE"] = departure_zulu.strftime("%Y-%m-%d")
    save_flight_config(leg_configs)
    print("\n[!] Config saved to flight_cache.json")
    print("==========================================\n")
    return leg_configs

# ---------------------------------------------------------
# SCRIPT EXECUTION
# ---------------------------------------------------------
if __name__ == "__main__":
    import os
    route_file = '../../../Downloads/route.fpl'
    if not os.path.exists(route_file):
        route_file = input(f"[!] '{route_file}' not found. Enter filename: ").strip()

    try:
        my_route, raw_checkpoints = parse_skyvector_fpl(route_file)

        # 1. Get configs and retrieve the dynamically filtered route
        FLIGHT_PROFILES = get_leg_configs_interactively(my_route, raw_checkpoints)
        final_route = FLIGHT_PROFILES["FINAL_ROUTE"]

        # 2. Process math strictly using the collapsed spine
        flight_legs = process_flight_plan(final_route, leg_configs=FLIGHT_PROFILES)

        # 3. Process checkpoints (Gatekeeper projects everything else dynamically)
        processed_checkpoints = []
        if raw_checkpoints:
            from checkpoint_math import process_checkpoints
            reset_points = FLIGHT_PROFILES.get("TURNING_POINTS", ["OMEMEE"])
            processed_checkpoints = process_checkpoints(final_route, flight_legs, raw_checkpoints, reset_points)

        # 4. Compile Fuel Totals
        total_enroute_fuel = sum(leg.get('fuel_req', 0) for leg in flight_legs)
        max_leg_gph = max([leg['gph'] for leg in flight_legs if isinstance(leg['gph'], (int, float))] or [8.5])
        reserves = FLIGHT_PROFILES.get("FUEL_RESERVES", {"cont_min": 10, "cont_gph": 8.5, "omit_reserve": False})

        try:
            with open('c172_climb.json', 'r') as f:
                startup_fuel = json.load(f).get("constants_and_adjustments", {}).get("startup_taxi_takeoff_gal", 1.4)
        except Exception:
            startup_fuel = 1.4

        cont_fuel = (reserves["cont_min"] / 60.0) * reserves["cont_gph"]
        res_min = 0 if reserves.get("omit_reserve", False) else 30
        res_gph = 0.0 if reserves.get("omit_reserve", False) else max_leg_gph
        res_fuel = (res_min / 60.0) * res_gph

        total_ramp_fuel = startup_fuel + total_enroute_fuel + cont_fuel + res_fuel
        planned_input = reserves.get("planned_ramp_fuel", "")
        if planned_input != "":
            starting_pfob = float(planned_input)
            pfob_label = "PLANNED RAMP FUEL (PILOT OVERRIDE):"
        else:
            starting_pfob = total_ramp_fuel
            pfob_label = "TOTAL MINIMUM FLIGHT FUEL:"

        # 5. Output Console
        print("\n" + "=" * 123)
        print("                                              VFR NAV LOG ENGINE                                             ")
        print("=" * 123)
        print(f"{'TO':<6} | {'ALT':<5} | {'OAT':<4} | {'RPM':<6} | {'IAS':<3} | {'TAS':<3} | {'TRK°T':<5} | {'WIND':<7} | {'HDG°T':<5} | {'VAR':<4} | {'HDG°M':<8} | {'GS':<3} | {'DIST':<7} | {'TIME':<7} | {'GPH':<4} | {'FUEL'}")
        print("-" * 123)

        def safe_int(val):
            return "-" if val == "-" else int(float(val))

        aliases = FLIGHT_PROFILES.get("ALIASES", {})

        for i, leg in enumerate(flight_legs):
            oat_str = "-" if leg['oat'] == "-" else f"{leg['oat']}°C"
            display_to = aliases.get(leg['to'], leg['to'])

            print(f"{display_to:<6} | {leg['altitude']:<5} | {oat_str:<4} | {leg['rpm_formatted']:<6} | "
                  f"{safe_int(leg['ias']):<3} | {safe_int(leg['tas']):<3} | {leg['track_true']:<5} | "
                  f"{leg['wind_true']:<7} | {leg['heading_true']:<5} | {leg['variation']:<4} | "
                  f"{leg['heading_mag']:<8} | {safe_int(leg['ground_speed']):<3} | "
                  f"{leg['dist_formatted']:<7} | {leg['time_formatted']:<7} | {leg['gph']:<4.1f} | {leg['fuel_formatted']:<9}")

            leg_checkpoints = [checkpoint for checkpoint in processed_checkpoints if checkpoint['leg_index'] == i]
            for checkpoint in leg_checkpoints:
                display_checkpoint = aliases.get(checkpoint['id'], checkpoint['id'])
                current_pfob = starting_pfob - startup_fuel - checkpoint.get('absolute_fuel_burned', 0)
                print(
                    f"  --> [checkpoint] {display_checkpoint:<6} | ETE: {checkpoint['ete_minutes']:>2}m | FLOWN: {checkpoint['dist_flown']:>2}nm | TO GO: {checkpoint['dist_to_go']:>2}nm | PFOB: {current_pfob:>4.1f} GAL")

            # Compile labels first to avoid Python 3.10 f-string quote collisions
        cont_label = f"CONTINGENCY ({reserves['cont_min']} MIN @ {reserves['cont_gph']} GPH):"
        res_label = f"RESERVE ({res_min} MIN @ {res_gph} GPH):"

        print("-" * 123)
        print(f"{'ENROUTE TOTAL:':>112} {total_enroute_fuel:>4.1f}")
        print(f"{cont_label:>112} {cont_fuel:>4.1f}")
        print(f"{res_label:>112} {res_fuel:>4.1f}")
        print("=" * 123)
        print(f"{pfob_label:>112} {starting_pfob:>4.1f} GAL\n")

    except Exception as e:
        print(f"\n[CRITICAL ERROR] {e}")