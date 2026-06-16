import os
import PyPDF2
import datetime
import textwrap
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

from navlog_engine import parse_skyvector_fpl, get_leg_configs_interactively, process_flight_plan, get_airport_elevation

# ---------------------------------------------------------
# THE MODULAR REGISTRY
# ---------------------------------------------------------
LAYOUT_REGISTRY = {
    "VFR_LOG": {
        "FRONT_PAGE": {
            "header": {
                "from_elev_date_y": 756,
                "aircraft_pilot_y": 756,
                "aircraft_x": 404,
                "pilot_x": 455
            },
            "cols": {
                "to": 70, "alt": 115, "oat": 150, "rpm": 178,
                "ias": 205, "tas": 235, "track": 265, "wind": 295,
                "hdg_true": 326, "var": 354, "hdg_mag": 385,
                "gs": 418, "dist": 448, "time": 476, "gph": 512, "fuel": 556
            },
            "row_start_y": 708,
            "row_height": -16
        },
        "BACK_ROTATED": {
            "header": {
                "from_box_x": 160, "from_box_y": 70,
                "dest_ete_x": 135, "dest_ete_y": 100
            },
            "cols": {
                "to": 52, "alt": 98, "ias": 158, "track_mag": 188,
                "hdg_mag": 220, "gs": 252, "dist": 282, "time": 314
            },
            "row_start_x": 205,
            "row_height": 17
        },
        "PAGE_2": {
            "ramp_x": 510,
            "toc_x": 455,
            "row_start_x": 400,
            "row_step": -55,
            "box_offset": 6,
            "cols": {
                "checkpoint": 730,
                "ete": 672,
                "dist_flown": 628,
                "dist_to_go": 563,
                "pfob": 498,
                "mfob": 463
            }
        }
    }
}


def generate_unique_filename(route_data):
    if len(route_data) >= 2:
        base_name = f"NAVLOG_{route_data[0]['id']}_to_{route_data[-1]['id']}"
    else:
        base_name = "stamped_navlog"

    filename = f"{base_name}.pdf"
    counter = 2
    while os.path.exists(filename):
        filename = f"{base_name}_v{counter}.pdf"
        counter += 1
    return filename


def safe_str(val):
    return "-" if val == "-" else str(val)


# ---------------------------------------------------------
# FORMATTING HELPERS
# ---------------------------------------------------------
def parse_leg_time(time_val):
    t_str = str(time_val).strip()
    if '/' in t_str: t_str = t_str.split('/')[-1]
    try:
        return float(t_str)
    except ValueError:
        return 0.0


def parse_leg_fuel(fuel_val):
    f_str = str(fuel_val).strip()
    if f_str == '-' or not f_str: return 0.0
    if '+' in f_str: return sum(float(part) for part in f_str.split('+'))
    try:
        return float(f_str)
    except ValueError:
        return 0.0


def format_checkpoint_ete(minutes_val):
    total_seconds = int(round(float(minutes_val) * 60))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0: return f"{hours}h{minutes}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s"


def format_checkpoint_endurance(fuel_gallons, gph):
    if not gph or float(gph) == 0: return "-"
    total_hours = float(fuel_gallons) / float(gph)
    hours = int(total_hours)
    minutes = int(round((total_hours - hours) * 60))
    if minutes == 60:
        hours += 1
        minutes = 0
    return f"{hours}h{minutes:02d}"


# ---------------------------------------------------------
# DRAWING FUNCTIONS
# ---------------------------------------------------------
def draw_leg_row(c, leg, y_pos, layout_config):
    cols = layout_config["cols"]
    c.setFont("Helvetica", 7)

    c.drawCentredString(cols["to"], y_pos, safe_str(leg.get('to', '-')))
    c.setFont("Helvetica", 9)
    c.drawCentredString(cols["alt"], y_pos, safe_str(leg.get('altitude', '-')))
    c.drawCentredString(cols["oat"], y_pos, safe_str(leg.get('oat', '-')))
    c.drawCentredString(cols["rpm"], y_pos, safe_str(leg.get('rpm_formatted', leg.get('rpm', '-'))))
    c.drawCentredString(cols["ias"], y_pos, safe_str(leg.get('ias', '-')))
    c.drawCentredString(cols["tas"], y_pos, safe_str(leg.get('tas', '-')))
    c.drawCentredString(cols["track"], y_pos, safe_str(leg.get('track_true', '-')))

    c.setFont("Helvetica", 7)
    c.drawCentredString(cols["wind"], y_pos, safe_str(leg.get('wind_true', '-')))
    c.setFont("Helvetica", 9)

    c.drawCentredString(cols["hdg_true"], y_pos, safe_str(leg.get('heading_true', '-')))
    c.drawCentredString(cols["var"], y_pos, safe_str(leg.get('variation', '-')))

    hdg_mag_val = leg.get('heading_mag_formatted', leg.get('heading_mag', '-'))
    c.drawCentredString(cols["hdg_mag"], y_pos, safe_str(hdg_mag_val))

    gs_val = safe_str(leg.get('ground_speed', '-'))
    if gs_val != "-":
        try:
            gs_display = str(int(round(float(gs_val))))
        except ValueError:
            gs_display = gs_val
        c.drawCentredString(cols["gs"], y_pos, gs_display)

    dist_val = safe_str(leg.get('dist_formatted', '-'))
    if "/" in dist_val:
        c.setFont("Helvetica", 7)
        c.drawCentredString(cols["dist"], y_pos, dist_val)
        c.setFont("Helvetica", 9)
    else:
        c.drawCentredString(cols["dist"], y_pos, dist_val)

    time_val = safe_str(leg.get('time_formatted', '-'))
    if "/" in time_val:
        c.setFont("Helvetica", 7)
        c.drawCentredString(cols["time"], y_pos, time_val)
        c.setFont("Helvetica", 9)
    else:
        c.drawCentredString(cols["time"], y_pos, time_val)

    c.drawCentredString(cols["gph"], y_pos, safe_str(leg.get('gph', '-')))

    fuel_val = leg.get('fuel_formatted', leg.get('fuel_req', '-'))
    c.drawCentredString(cols["fuel"], y_pos, f"{fuel_val:.1f}" if isinstance(fuel_val, float) else safe_str(fuel_val))


def draw_rotated_text(c, x, y, text, angle, max_chars=None, font_size=None):
    c.saveState()
    current_font = font_size if font_size else 9
    c.setFont("Helvetica", current_font)
    c.translate(x, y)
    c.rotate(angle)

    text_str = str(text)
    if max_chars and len(text_str) > max_chars:
        lines = textwrap.wrap(text_str, width=max_chars)
    else:
        lines = [text_str]

    line_spacing = current_font * 1.1
    start_y = (len(lines) - 1) * (line_spacing / 2.0)
    for i, line in enumerate(lines):
        c.drawCentredString(0, start_y - (i * line_spacing), line)
    c.restoreState()


def draw_rotated_leg_row(c, leg, current_x_pos, layout_config):
    cols = layout_config["cols"]

    def get_clean(key):
        val = str(leg.get(key, ""))
        return "" if val == "-" else val

    gs_raw = get_clean('ground_speed')
    if gs_raw:
        try:
            gs_clean = str(int(round(float(gs_raw))))
        except ValueError:
            gs_clean = gs_raw
    else:
        gs_clean = ""

    data = {
        "to": get_clean('to'),
        "alt": get_clean('altitude'),
        "ias": get_clean('ias'),
        "gs": gs_clean,
        "dist": get_clean('dist_formatted').split('/')[-1].strip(),
        "time": get_clean('time_formatted').split('/')[-1].strip()
    }

    hdg_val = get_clean('heading_mag')
    hsi_val = get_clean('heading_compass')  # Grab the new engine output
    data["hdg_mag"] = f"{int(float(hdg_val)):03d}" if hdg_val else ""
    data["hsi_hdg"] = f"{int(float(hsi_val)):03d}" if hsi_val else data["hdg_mag"]
    track_true = get_clean('track_true')
    hdg_true = get_clean('heading_true')

    if track_true and hdg_val and hdg_true:
        var = int(float(hdg_val)) - int(float(hdg_true))
        track_mag = (int(float(track_true)) + var) % 360
        data["track_mag"] = f"{360 if track_mag == 0 else track_mag:03d}"
    else:
        data["track_mag"] = track_true

    draw_rotated_text(c, current_x_pos, cols["to"], data["to"], angle=90)
    draw_rotated_text(c, current_x_pos, cols["alt"], data["alt"], angle=90)
    draw_rotated_text(c, current_x_pos, cols["ias"], data["ias"], angle=90)
    draw_rotated_text(c, current_x_pos, cols["track_mag"], data["track_mag"], angle=90)

    # FIX: Correctly pull "hdg_mag" and use angle=90 instead of angle=-90
    draw_rotated_text(c, current_x_pos, cols["hdg_mag"], data["hsi_hdg"], angle=90)

    draw_rotated_text(c, current_x_pos, cols["gs"], data["gs"], angle=90)
    draw_rotated_text(c, current_x_pos, cols["dist"], data["dist"], angle=90)
    draw_rotated_text(c, current_x_pos, cols["time"], data["time"], angle=90)


# ---------------------------------------------------------
# OVERLAY GENERATOR
# ---------------------------------------------------------
def create_overlay(legs, route_data, processed_checkpoints, flight_configs, total_ramp_fuel, starting_pfob,
                   output_filename="stamped_navlog.pdf"):
    c = canvas.Canvas("overlay.pdf", pagesize=letter)

    # =========================================================================
    # PAGE 1 STAMPING
    # =========================================================================
    c.setFont("Helvetica", 9)
    front_config = LAYOUT_REGISTRY["VFR_LOG"]["FRONT_PAGE"]
    back_config = LAYOUT_REGISTRY["VFR_LOG"]["BACK_ROTATED"]
    p2_config = LAYOUT_REGISTRY["VFR_LOG"]["PAGE_2"]

    h_y_fed = front_config["header"]["from_elev_date_y"]
    h_y_ap = front_config["header"]["aircraft_pilot_y"]

    c.drawCentredString(70, h_y_fed, route_data[0]['id'])
    c.drawCentredString(115, h_y_fed, str(get_airport_elevation(route_data[0]['id'])))
    flight_date = flight_configs.get("FLIGHT_DATE", datetime.datetime.now().strftime("%Y-%m-%d"))
    c.drawCentredString(533, h_y_fed, flight_date)
    c.drawCentredString(front_config["header"]["aircraft_x"], h_y_ap, flight_configs.get("AIRCRAFT_ID", ""))

    c.setFont("Helvetica", 6)
    c.drawCentredString(front_config["header"]["pilot_x"], h_y_ap, flight_configs.get("PILOT_NAME", ""))
    c.setFont("Helvetica", 9)

    import json
    try:
        with open('c172_climb.json', 'r') as f:
            startup_fuel = float(json.load(f).get("constants_and_adjustments", {}).get("startup_taxi_takeoff_gal", 1.4))
    except Exception:
        startup_fuel = 1.4

    c.drawCentredString(556, 726, f"{startup_fuel:.1f}")
    current_y = front_config["row_start_y"]
    row_step = front_config["row_height"]

    total_dist = 0.0
    total_time = 0.0

    for leg in legs:
        draw_leg_row(c, leg, current_y, front_config)
        d_str = str(leg.get('dist_formatted', '0')).strip()
        if '/' in d_str: d_str = d_str.split('/')[-1]
        if d_str and d_str != '-':
            try:
                total_dist += float(d_str)
            except ValueError:
                pass

        t_str = str(leg.get('time_formatted', '0')).strip()
        if '/' in t_str: t_str = t_str.split('/')[-1]
        if t_str and t_str != '-':
            try:
                total_time += float(t_str)
            except ValueError:
                pass

        current_y += row_step

    total_enroute = sum(parse_leg_fuel(leg.get('fuel_formatted', leg.get('fuel_req', 0))) for leg in legs)
    reserves = flight_configs.get("FUEL_RESERVES", {"cont_min": 10, "cont_gph": 8.5, "omit_reserve": False})
    cont_min = int(reserves.get("cont_min", 10))
    cont_gph = float(reserves.get("cont_gph", 8.5))
    cont_fuel = (cont_min / 60.0) * cont_gph
    subtotal_fuel = startup_fuel + total_enroute + cont_fuel

    if reserves.get("omit_reserve", False):
        res_min = 0;
        res_gph = 0.0;
        res_fuel = 0.0
    else:
        res_min = 30
        max_leg_gph = max([leg.get('gph', 8.5) for leg in legs if isinstance(leg.get('gph'), (int, float))] or [8.5])
        res_gph = float(max_leg_gph)
        res_fuel = (res_min / 60.0) * res_gph

    total_fuel = subtotal_fuel + res_fuel

    y_cont, y_subt, y_resv, y_totl = 494, 478, 462, 446
    col_dist, col_time, col_gph, col_fuel = 446, 476, 512, 556

    c.drawCentredString(col_time, y_cont, str(cont_min))
    c.drawCentredString(col_gph, y_cont, f"{cont_gph:.1f}")
    c.drawCentredString(col_fuel, y_cont, f"{cont_fuel:.1f}")
    c.drawCentredString(col_dist, y_subt, str(int(round(total_dist))))
    c.drawCentredString(col_time, y_subt, str(int(round(total_time))))
    c.drawCentredString(col_fuel, y_subt, f"{subtotal_fuel:.1f}")
    c.drawCentredString(col_time, y_resv, str(res_min))
    c.drawCentredString(col_gph, y_resv, f"{res_gph:.1f}")
    c.drawCentredString(col_fuel, y_resv, f"{res_fuel:.1f}")
    c.drawCentredString(col_dist, y_totl, str(int(round(total_dist))))
    c.drawCentredString(col_time, y_totl, str(int(round(total_time))))
    c.drawCentredString(col_fuel, y_totl, f"{total_fuel:.1f}")

    current_x_pos = back_config["row_start_x"]
    row_height_back = back_config["row_height"]
    draw_rotated_text(c, back_config["header"]["from_box_x"], back_config["header"]["from_box_y"], route_data[0]['id'],
                      angle=90)
    ete_hours = int(total_time // 60)
    ete_minutes = int(total_time % 60)
    dest_ete_str = f"{ete_hours}h{ete_minutes:02d}"
    draw_rotated_text(c, back_config["header"]["dest_ete_x"], back_config["header"]["dest_ete_y"], dest_ete_str,
                      angle=90)

    for leg in legs:
        if "to" in leg:
            draw_rotated_leg_row(c, leg, current_x_pos, back_config)
            current_x_pos += row_height_back

    # =========================================================================
    # PAGE 2 STAMPING (Sequential Chronological Filtering)
    # =========================================================================
    c.showPage()
    c.setFont("Helvetica", 9)

    ramp_x = p2_config["ramp_x"]
    toc_x = p2_config["toc_x"]
    p2_x = p2_config["row_start_x"]
    p2_step = p2_config["row_step"]
    p2_offset = p2_config["box_offset"]
    p2_cols = p2_config["cols"]
    aliases = flight_configs.get("ALIASES", {})

    max_leg_gph = 8.5
    for leg in legs:
        try:
            val = float(leg.get('gph', 0))
            if val > max_leg_gph: max_leg_gph = val
        except ValueError:
            pass

    # 1. GENERATE CLEAN ROWS
    display_rows = []

    max_leg_gph = max([float(leg.get('gph', 8.5)) for leg in legs if leg.get('gph') is not None] or [8.5])

    # Push Ramp Row
    display_rows.append({
        "type": "RAMP",
        "pfob": f"{starting_pfob:.1f}",
        "mfob": f"{total_ramp_fuel:.1f}",
        "endurance_pfob": format_checkpoint_endurance(starting_pfob, max_leg_gph),
        "endurance_mfob": format_checkpoint_endurance(total_ramp_fuel, max_leg_gph)
    })

    accum_fuel_burned = startup_fuel
    segment_start_fuel_burned = 0.0
    reset_ids = flight_configs.get("TURNING_POINTS", [])
    last_printed_raw = None

    # ADDED: A separate counter to track geographical waypoints
    route_idx = 0

    for i, leg in enumerate(legs):
        leg_gph = float(leg.get('gph', 8.5))

        # 1. FIX: Since we changed the altitude to a number (e.g., "1600") in Bug 1,
        # we MUST update this filter to use the boolean flag so it still skips circuits on Page 2.
        if leg.get('is_circuit', False) or leg.get('altitude') == 'Circuit':
            accum_fuel_burned += parse_leg_fuel(leg.get('fuel_formatted', leg.get('fuel_req', 0)))
            continue

        if route_idx < len(route_data):
            shp_id = route_data[route_idx]['id']
        else:
            shp_id = leg.get('to', 'UNKNOWN')

        raw_shp = aliases.get(shp_id, shp_id)

        if shp_id in reset_ids or route_idx == 0:
            segment_start_fuel_burned = accum_fuel_burned

        leg_pfob = starting_pfob - accum_fuel_burned
        leg_mfob = total_ramp_fuel - accum_fuel_burned

        # 2. NEW LOGIC: Check if this waypoint is acting as a departure after a full stop
        is_departure_after_full_stop = False
        if i > 0 and (legs[i - 1].get('is_circuit', False) or legs[i - 1].get('altitude') == 'Circuit'):
            is_departure_after_full_stop = True

        # SHP Parsing
        if route_idx > 0 and raw_shp != last_printed_raw:
            # 3. FIX: Only print if it is a Turning Point AND it's not a new departure
            if shp_id in reset_ids and not is_departure_after_full_stop:
                display_name = f"** {raw_shp} **"
                if len(raw_shp) > 11: display_name = f"** {raw_shp[:10]}. **"

                display_rows.append({
                    "type": "SHP",
                    "name": display_name,
                    "pfob": f"{leg_pfob:.1f}",
                    "mfob": f"{leg_mfob:.1f}",
                    "endurance_pfob": format_checkpoint_endurance(leg_pfob, leg_gph),
                    "endurance_mfob": format_checkpoint_endurance(leg_mfob, leg_gph)
                })
                last_printed_raw = raw_shp

        # Checkpoints Parsing
        leg_checkpoints = [checkpoint for checkpoint in processed_checkpoints if checkpoint['leg_index'] == i]
        leg_checkpoints.sort(key=lambda x: x.get('dist_flown', 0))

        for checkpoint in leg_checkpoints:
            checkpoint_id = checkpoint['id']
            raw_checkpoint = aliases.get(checkpoint_id, checkpoint_id)

            # Skip if it is an exact duplicate of the SHP, Leg Destination, or the immediate preceding point
            if raw_checkpoint == raw_shp or raw_checkpoint == aliases.get(leg['to'], leg['to']) or raw_checkpoint == last_printed_raw:
                continue

            display_name = raw_checkpoint
            if len(raw_checkpoint) > 11: display_name = raw_checkpoint[:10] + "."

            checkpoint_total_burned = startup_fuel + float(checkpoint.get('absolute_fuel_burned', 0))

            checkpoint_pfob = starting_pfob - checkpoint_total_burned
            checkpoint_mfob = total_ramp_fuel - checkpoint_total_burned

            display_rows.append({
                "type": "Checkpoint",
                "name": display_name,
                "ete": format_checkpoint_ete(checkpoint['ete_minutes']),
                "dist_flown": str(checkpoint['dist_flown']),
                "dist_to_go": str(checkpoint['dist_to_go']),
                "pfob": f"{max(0, checkpoint_pfob):.1f}",  # Ensure we don't display negative fuel
                "mfob": f"{max(0, checkpoint_mfob):.1f}",
                "endurance_pfob": format_checkpoint_endurance(checkpoint_pfob, leg_gph),
                "endurance_mfob": format_checkpoint_endurance(checkpoint_mfob, leg_gph)
            })
            last_printed_raw = raw_checkpoint

        accum_fuel_burned += parse_leg_fuel(leg.get('fuel_formatted', leg.get('fuel_req', 0)))

        # Advance the geographical waypoint counter
        route_idx += 1

    # 2. DRAW CLEAN ROWS
    first_shp_drawn = False

    for row in display_rows:
        if row["type"] == "RAMP":
            draw_rotated_text(c, ramp_x + p2_offset, p2_cols["pfob"], row["pfob"], angle=-90, font_size=6)
            draw_rotated_text(c, ramp_x - p2_offset, p2_cols["pfob"], row["endurance_pfob"], angle=-90, font_size=6)
            draw_rotated_text(c, ramp_x + p2_offset, p2_cols["mfob"], row["mfob"], angle=-90, font_size=6)
            draw_rotated_text(c, ramp_x - p2_offset, p2_cols["mfob"], row["endurance_mfob"], angle=-90, font_size=6)

        elif row["type"] == "SHP":
            if not first_shp_drawn:
                use_x = toc_x
                first_shp_drawn = True
            else:
                use_x = p2_x
                p2_x += p2_step

            c.setFont("Helvetica", 9 if len(row["name"]) <= 11 else 7)
            draw_rotated_text(c, use_x, p2_cols["checkpoint"], row["name"], angle=-90)

            c.setFont("Helvetica", 9)
            draw_rotated_text(c, use_x + p2_offset, p2_cols["ete"], "-", angle=-90)
            draw_rotated_text(c, use_x, p2_cols["dist_flown"], "-", angle=-90)
            draw_rotated_text(c, use_x, p2_cols["dist_to_go"], "-", angle=-90)

            draw_rotated_text(c, use_x + p2_offset, p2_cols["pfob"], row["pfob"], angle=-90, font_size=6)
            draw_rotated_text(c, use_x - p2_offset, p2_cols["pfob"], row["endurance_pfob"], angle=-90, font_size=6)
            draw_rotated_text(c, use_x + p2_offset, p2_cols["mfob"], row["mfob"], angle=-90, font_size=6)
            draw_rotated_text(c, use_x - p2_offset, p2_cols["mfob"], row["endurance_mfob"], angle=-90, font_size=6)

        elif row["type"] == "Checkpoint":
            use_x = p2_x
            p2_x += p2_step

            c.setFont("Helvetica", 9 if len(row["name"]) <= 10 else 7)
            draw_rotated_text(c, use_x, p2_cols["checkpoint"], row["name"], angle=-90)

            c.setFont("Helvetica", 9)
            draw_rotated_text(c, use_x + p2_offset, p2_cols["ete"], row["ete"], angle=-90)
            draw_rotated_text(c, use_x, p2_cols["dist_flown"], row["dist_flown"], angle=-90)
            draw_rotated_text(c, use_x, p2_cols["dist_to_go"], row["dist_to_go"], angle=-90)

            draw_rotated_text(c, use_x + p2_offset, p2_cols["pfob"], row["pfob"], angle=-90, font_size=6)
            draw_rotated_text(c, use_x - p2_offset, p2_cols["pfob"], row["endurance_pfob"], angle=-90, font_size=6)
            draw_rotated_text(c, use_x + p2_offset, p2_cols["mfob"], row["mfob"], angle=-90, font_size=6)
            draw_rotated_text(c, use_x - p2_offset, p2_cols["mfob"], row["endurance_mfob"], angle=-90, font_size=6)

    c.save()

    # =========================================================================
    # MULTI-PAGE PDF MERGE PROCESSING
    # =========================================================================
    base_pdf_path = "304 - VFR NAV Log 2024-1 (2).pdf"
    with open(base_pdf_path, "rb") as f_orig, open("overlay.pdf", "rb") as f_overlay:
        original_pdf = PyPDF2.PdfReader(f_orig)
        overlay_pdf = PyPDF2.PdfReader(f_overlay)
        output = PyPDF2.PdfWriter()

        page1 = original_pdf.pages[0]
        page1.merge_page(overlay_pdf.pages[0])
        output.add_page(page1)

        if len(original_pdf.pages) > 1 and len(overlay_pdf.pages) > 1:
            page2 = original_pdf.pages[1]
            page2.merge_page(overlay_pdf.pages[1])
            output.add_page(page2)

        with open(output_filename, "wb") as f_out:
            output.write(f_out)

    print(f"\n[+] Navigation Log stamped successfully! Saved as: {output_filename}")
    os.remove("overlay.pdf")


if __name__ == "__main__":
    my_route, raw_checkpoints = parse_skyvector_fpl('route.fpl')

    # Pull the final dynamically filtered route
    FLIGHT_PROFILES = get_leg_configs_interactively(my_route, raw_checkpoints)
    final_route = FLIGHT_PROFILES.get("FINAL_ROUTE", my_route)
    calculated_legs = process_flight_plan(final_route, leg_configs=FLIGHT_PROFILES)

    total_enroute_fuel = sum(
        parse_leg_fuel(leg.get('fuel_formatted', leg.get('fuel_req', 0))) for leg in calculated_legs)
    reserves = FLIGHT_PROFILES.get("FUEL_RESERVES", {"cont_min": 10, "cont_gph": 8.5, "omit_reserve": False})

    cont_fuel = (reserves["cont_min"] / 60.0) * reserves["cont_gph"]
    res_min = 0 if reserves.get("omit_reserve", False) else 30
    max_leg_gph = max(
        [leg.get('gph', 8.5) for leg in calculated_legs if isinstance(leg.get('gph'), (int, float))] or [8.5])
    res_gph = 0.0 if reserves.get("omit_reserve", False) else max_leg_gph
    res_fuel = (res_min / 60.0) * res_gph

    import json

    try:
        with open('c172_climb.json', 'r') as f:
            startup_fuel = float(json.load(f).get("constants_and_adjustments", {}).get("startup_taxi_takeoff_gal", 1.4))
    except Exception:
        startup_fuel = 1.4

    total_ramp_fuel = startup_fuel + total_enroute_fuel + cont_fuel + res_fuel

    planned_input = reserves.get("planned_ramp_fuel", "")
    if planned_input == "":
        print(f"\n[?] Engine reported no Planned Ramp Fuel (Minimum required is {total_ramp_fuel:.1f} GAL).")
        override = input("    Enter Planned Ramp Fuel to override (or press Enter to use minimum): ").strip()
        if override: planned_input = override

    try:
        starting_pfob = float(planned_input) if planned_input != "" else total_ramp_fuel
    except ValueError:
        starting_pfob = total_ramp_fuel

    processed_checkpoints = []
    if raw_checkpoints:
        try:
            from checkpoint_math import process_checkpoints

            reset_points = FLIGHT_PROFILES.get("TURNING_POINTS", ["OMEMEE"])
            # Pass final_route to ensure math aligns perfectly
            processed_checkpoints = process_checkpoints(final_route, calculated_legs, raw_checkpoints, reset_points)
        except Exception as e:
            print(f"[!] Checkpoint processing bypassed: {e}")

    safe_output_name = generate_unique_filename(final_route)

    create_overlay(
        legs=calculated_legs,
        route_data=final_route,  # IMPORTANT: Base the PDF UI on final_route
        processed_checkpoints=processed_checkpoints,
        flight_configs=FLIGHT_PROFILES,
        total_ramp_fuel=total_ramp_fuel,
        starting_pfob=starting_pfob,
        output_filename=safe_output_name
    )