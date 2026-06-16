import math
from geopy.distance import geodesic


def get_true_bearing(lat1, lon1, lat2, lon2):
    """Calculates the true bearing between two coordinate pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    initial_bearing = math.atan2(x, y)
    return (math.degrees(initial_bearing) + 360) % 360


def calculate_abeam_projection(leg_start, leg_end, checkpoint):
    """Projects the checkpoint onto the leg vector."""
    dist_leg = geodesic((leg_start['lat'], leg_start['lon']), (leg_end['lat'], leg_end['lon'])).nautical
    if dist_leg == 0:
        return 0.0, 0.0, 0.0

    dist_checkpoint = geodesic((leg_start['lat'], leg_start['lon']), (checkpoint['lat'], checkpoint['lon'])).nautical
    bearing_leg = get_true_bearing(leg_start['lat'], leg_start['lon'], leg_end['lat'], leg_end['lon'])
    bearing_checkpoint = get_true_bearing(leg_start['lat'], leg_start['lon'], checkpoint['lat'], checkpoint['lon'])

    angle_diff = math.radians(bearing_checkpoint - bearing_leg)
    along_track = dist_checkpoint * math.cos(angle_diff)
    cross_track = dist_checkpoint * math.sin(angle_diff)

    fraction = along_track / dist_leg
    return fraction, along_track, abs(cross_track)


def parse_num(val):
    """Extracts the total numeric value from formatted strings like '8+1/9'."""
    val_str = str(val)
    if '/' in val_str:
        return float(val_str.split('/')[-1])
    try:
        return float(val_str)
    except ValueError:
        return 0.0


def process_checkpoints(route_data, flight_legs, all_points, reset_ids):
    mapped_checkpoints = []

    # =========================================================================
    # 1. THE GATEKEEPER (Logic-based filtering)
    # =========================================================================
    # We create a set of "Hard" IDs from your route_data.
    # If a checkpoint ID is in this list, it's a turning point—we IGNORE it.
    main_route_ids = {node['id'] for node in route_data}

    # We create a set to ensure we don't process duplicate points
    processed_ids = set()
    observation_checkpoints = []

    for checkpoint in all_points:
        checkpoint_id = checkpoint['id']

        # Skip if we already processed this ID
        if checkpoint_id in processed_ids:
            continue

        # THE FIX: If the ID exists in our route path, it's a turning point.
        # We do NOT perform abeam math on turning points.
        if checkpoint_id in main_route_ids:
            continue

        observation_checkpoints.append(checkpoint)
        processed_ids.add(checkpoint_id)


    # =========================================================================
    # 2. CHRONOLOGICAL LEG ASSIGNMENT
    # =========================================================================
    current_leg_idx = 0
    temp_mapped = []

    for checkpoint in observation_checkpoints:
        best_leg = current_leg_idx
        min_score = float('inf')
        best_fraction = 0.0
        best_cross = 0.0

        for i in range(current_leg_idx, len(route_data) - 1):
            fraction, along, cross = calculate_abeam_projection(route_data[i], route_data[i + 1], checkpoint)

            penalty = 0
            if fraction < -0.1: penalty = abs(fraction) * 1000
            if fraction > 1.1: penalty = abs(fraction - 1) * 1000

            score = cross + penalty
            if score < min_score:
                min_score = score
                best_leg = i
                best_fraction = max(0.0, min(1.0, fraction))
                best_cross = cross

        current_leg_idx = best_leg

        temp_mapped.append({
            "checkpoint": checkpoint,
            "leg_idx": best_leg,
            "fraction": best_fraction,
            "cross_track": best_cross
        })

    # =========================================================================
    # 3. 1D TIMELINE UNROLLING & AUTO-ANCHORS
    # =========================================================================
    cumulative_dist = [0.0]
    cumulative_time = [0.0]
    cumulative_fuel = [0.0]

    for i in range(len(flight_legs)):
        cumulative_dist.append(cumulative_dist[-1] + parse_num(flight_legs[i].get('distance_nm', 0)))
        cumulative_time.append(cumulative_time[-1] + parse_num(flight_legs[i].get('total_time', 0)))
        cumulative_fuel.append(cumulative_fuel[-1] + float(flight_legs[i].get('fuel_req', 0)))

    reset_anchors = set([0, len(route_data) - 1])

    for i in range(len(route_data)):
        node_id = route_data[i]['id']
        # Auto-detect SHPs: Only if it's explicitly in your TURNING_POINTS prompt
        if node_id in reset_ids:
            reset_anchors.add(i)

    for i in range(len(flight_legs)):
        if flight_legs[i].get('is_climb_leg', False):
            reset_anchors.add(i)

    reset_anchors = sorted(list(reset_anchors))

    # --- ADDED: Translator to skip injected circuit legs ---
    spatial_to_flight = []
    f_idx = 0
    for i in range(len(route_data) - 1):
        while f_idx < len(flight_legs) and flight_legs[f_idx].get('is_circuit', False):
            f_idx += 1
        spatial_to_flight.append(f_idx)
        f_idx += 1
    # -------------------------------------------------------

    for item in temp_mapped:
        checkpoint = item['checkpoint']
        spatial_idx = item['leg_idx']
        f = item['fraction']

        # Translate spatial segment to actual flight leg
        leg_idx = spatial_to_flight[spatial_idx]

        # Extract the performance metrics for this specific leg
        leg_gs = float(flight_legs[leg_idx].get('gs', 1))  # Prevent div by 0
        if leg_gs <= 0: leg_gs = 1  # Fallback

        leg_gph = float(flight_legs[leg_idx].get('gph', 10.0))

        # 1. Calculate exact distance flown in THIS leg
        dist_flown_in_leg = f * parse_num(flight_legs[leg_idx].get('distance_nm', 0))

        # 2. Calculate time based on Ground Speed (not a fraction of total leg time)
        time_flown_in_leg = (dist_flown_in_leg / leg_gs) * 60  # in minutes

        # 3. Calculate fuel based strictly on GPH and time flown
        fuel_burned_in_leg = (time_flown_in_leg / 60) * leg_gph

        # 4. Add isolated leg burn to the cumulative totals
        checkpoint_abs_dist = cumulative_dist[leg_idx] + dist_flown_in_leg
        checkpoint_abs_time = cumulative_time[leg_idx] + time_flown_in_leg
        checkpoint_abs_fuel = cumulative_fuel[leg_idx] + fuel_burned_in_leg

        prev_anchor_idx = max([r for r in reset_anchors if r <= leg_idx], default=0)
        next_anchor_idx = min([r for r in reset_anchors if r > leg_idx], default=len(route_data) - 1)

        anchor_start_dist = cumulative_dist[prev_anchor_idx]
        anchor_start_time = cumulative_time[prev_anchor_idx]
        anchor_start_fuel = cumulative_fuel[prev_anchor_idx]
        anchor_end_dist = cumulative_dist[next_anchor_idx]

        mapped_checkpoints.append({
            "type": "CHECKPOINT",
            "id": checkpoint["id"],
            "leg_index": leg_idx,
            "ete_minutes": round(checkpoint_abs_time - anchor_start_time, 2),
            "dist_flown": int(round(checkpoint_abs_dist - anchor_start_dist)),
            "dist_to_go": int(round(anchor_end_dist - checkpoint_abs_dist)),
            "fuel_burned": round(checkpoint_abs_fuel - anchor_start_fuel, 1),
            "absolute_fuel_burned": round(checkpoint_abs_fuel, 1),
            "cross_track_error": round(item['cross_track'], 1)
        })

    mapped_checkpoints.sort(key=lambda x: (x['leg_index'], x['absolute_fuel_burned']))
    return mapped_checkpoints